"""Expo push delivery — keyless, stdlib-only, with bounded urgent retry.

Sends notifications through the Expo push service
(``POST https://exp.host/--/api/v2/push/send``). The service is free and
needs no account or API key: the push token issued to the app *is* the
credential. Deployment can still override the endpoint (e.g. a self-hosted
relay or an Expo access token for private projects) via environment:

* ``OPENJARVIS_PUSH_ENDPOINT`` — full URL of a push endpoint
* ``OPENJARVIS_PUSH_ACCESS_TOKEN`` — optional ``Authorization: Bearer`` token

Reliability model:

* Normal notifications: one attempt; a network failure is reported, never
  retried (a stale reminder is noise, not an emergency).
* Urgent "call" alerts: retried by a bounded daemon worker (up to 3
  attempts, 60 s apart, queue capped at 20) so a transient network blip
  does not swallow an important contact attempt.
* ``DeviceNotRegistered`` responses prune the dead token automatically.
* Quiet hours are filtered here (urgent alerts bypass when the device
  allows it) — callers do not need to know the per-device settings.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from openjarvis.mobile.push_registry import (
    PushRegistry,
    get_shared_registry,
    in_quiet_hours,
)

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://exp.host/--/api/v2/push/send"
_URGENT_CHANNEL = "jarivs-urgent"
_NORMAL_CHANNEL = "jarivs-updates"
_BATCH_LIMIT = 100
_RETRIES_URGENT = 3
_RETRY_DELAY_SECONDS = 60.0
_RETRY_QUEUE_LIMIT = 20


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


class PushSender:
    """Delivers notifications; swallows every delivery error into reports."""

    def __init__(self, registry: Optional[PushRegistry] = None,
                 endpoint: Optional[str] = None,
                 access_token: Optional[str] = None,
                 dry_run: bool = False,
                 timeout_seconds: float = 10.0) -> None:
        self.registry = registry or get_shared_registry()
        self.endpoint = endpoint or os.environ.get("OPENJARVIS_PUSH_ENDPOINT", DEFAULT_ENDPOINT)
        self.access_token = access_token or os.environ.get("OPENJARVIS_PUSH_ACCESS_TOKEN", "")
        self.dry_run = bool(dry_run)
        self.timeout = float(timeout_seconds)
        self.sent: List[Dict[str, Any]] = []
        self._retry_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_RETRY_QUEUE_LIMIT)
        self._retry_thread: Optional[threading.Thread] = None
        self._retry_lock = threading.Lock()

    # ---------------------------------------------------------------- public
    def notify(self, *, title: str, body: str, data: Optional[Dict[str, Any]] = None,
               urgent: bool = False, sound: Optional[str] = None,
               devices: Optional[List[str]] = None,
               retry_urgent: bool = True) -> Dict[str, Any]:
        """Push one notification to matching devices.

        Returns a delivery report:
        ``{"targeted", "delivered", "failed", "skipped_quiet", "results"}``.
        """
        title = _clip(title, 120)
        body = _clip(body, 240)
        if not title and not body:
            return {"targeted": 0, "delivered": 0, "failed": 0,
                    "skipped_quiet": 0, "results": [],
                    "error": "empty notification"}

        records = self.registry.active_records()
        if devices is not None:
            wanted = {str(d) for d in devices}
            records = [r for r in records if r["device_id"] in wanted]

        now_min = None
        targeted: List[Dict[str, Any]] = []
        skipped_quiet = 0
        for record in records:
            if not urgent:
                if now_min is None:
                    from openjarvis.mobile.push_registry import minutes_of_day
                    now_min = minutes_of_day()
                if in_quiet_hours(record.get("quiet_start_minutes"),
                                  record.get("quiet_end_minutes"), now_min):
                    skipped_quiet += 1
                    continue
            targeted.append(record)

        if not targeted:
            return {"targeted": 0, "delivered": 0, "failed": 0,
                    "skipped_quiet": skipped_quiet, "results": [],
                    "error": "no active push devices"
                             + (" (quiet hours)" if skipped_quiet else "")}

        payload_data = dict(data or {})
        if urgent:
            payload_data.setdefault("jarvisUrgent", True)
            payload_data.setdefault("jarvisCall", True)
            payload_data.setdefault("speak", True)

        messages: List[Dict[str, Any]] = []
        for record in targeted:
            messages.append({
                "to": record["expo_push_token"],
                "title": title,
                "body": body,
                "data": payload_data,
                "priority": "high" if urgent else "default",
                "sound": "default" if (sound or urgent) else None,
                "channelId": _URGENT_CHANNEL if urgent else _NORMAL_CHANNEL,
                **({"badge": 1} if urgent else {}),
            })

        results = self._dispatch(messages, targeted, urgent)
        delivered = sum(1 for r in results if r.get("ok"))
        failed = len(results) - delivered
        report = {"targeted": len(targeted), "delivered": delivered,
                  "failed": failed, "skipped_quiet": skipped_quiet,
                  "results": results}
        self.sent.append({"title": title, "body": body, "urgent": urgent,
                          "at": time.time(), "delivered": delivered,
                          "failed": failed})
        self.sent = self.sent[-100:]
        if failed and urgent and retry_urgent and not self.dry_run:
            self._enqueue_retry(title, body, payload_data, sound)
        return report

    # -------------------------------------------------------------- delivery
    def _dispatch(self, messages: List[Dict[str, Any]],
                  targeted: List[Dict[str, Any]], urgent: bool) -> List[Dict[str, Any]]:
        if self.dry_run:
            results = []
            for record in targeted:
                self.registry.record_delivery(record["device_id"], True, "dry-run")
                results.append({"device_id": record["device_id"], "ok": True,
                                "status": "dry-run"})
            return results
        results: List[Dict[str, Any]] = []
        for index in range(0, len(messages), _BATCH_LIMIT):
            batch = messages[index: index + _BATCH_LIMIT]
            batch_records = targeted[index: index + _BATCH_LIMIT]
            try:
                response = self._post_json(self.endpoint, batch)
                statuses = self._parse_response(response)
            except Exception as exc:  # network/HTTP error — never raise
                logger.warning("push dispatch failed: %s", exc)
                for record in batch_records:
                    self.registry.record_delivery(record["device_id"], False, str(exc)[:64])
                    results.append({"device_id": record["device_id"], "ok": False,
                                    "status": str(exc)[:128], "network_error": True})
                continue
            for record, status in zip(batch_records, statuses):
                ok = status.get("status") == "ok"
                details = status.get("details") or {}
                reason = str(details.get("error") or status.get("message") or
                             (status.get("status") or ""))[:64]
                self.registry.record_delivery(record["device_id"], ok, reason or ("ok" if ok else "failed"))
                if details.get("error") in ("DeviceNotRegistered", "InvalidCredentials"):
                    self.registry.prune_device(record["device_id"], str(details["error"]))
                    reason = f"pruned: {details['error']}"
                results.append({"device_id": record["device_id"], "ok": ok,
                                "status": reason})
        return results

    def _post_json(self, url: str, body: List[Dict[str, Any]]) -> Dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json",
                     **({"Authorization": f"Bearer {self.access_token}"} if self.access_token else {})},
            method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse_response(response: Any) -> List[Dict[str, Any]]:
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, list):
                return [item if isinstance(item, dict) else {"status": "error",
                                                             "message": str(item)}
                        for item in data]
        return [{"status": "error", "message": "unparseable push response"}]

    # ----------------------------------------------------------- urgent retry
    def _enqueue_retry(self, title: str, body: str,
                       data: Dict[str, Any], sound: Optional[str]) -> None:
        try:
            self._retry_queue.put_nowait({"title": title, "body": body, "data": data,
                                          "sound": sound, "attempts": 0,
                                          "queued_at": time.time()})
        except queue.Full:
            return
        self._ensure_retry_worker()

    def _ensure_retry_worker(self) -> None:
        with self._retry_lock:
            if self._retry_thread is not None and self._retry_thread.is_alive():
                return
            self._retry_thread = threading.Thread(target=self._retry_loop,
                                                  name="openjarvis-push-retry",
                                                  daemon=True)
            self._retry_thread.start()

    def _retry_loop(self) -> None:
        while True:
            item = self._retry_queue.get()
            if item is None:
                return
            try:
                if item["attempts"] >= _RETRIES_URGENT:
                    continue
                time.sleep(min(_RETRY_DELAY_SECONDS, 5.0) if item["attempts"] == 0
                           else _RETRY_DELAY_SECONDS)
                item["attempts"] += 1
                self.notify(title=item["title"], body=item["body"],
                            data=item["data"], urgent=True,
                            sound=item["sound"], retry_urgent=False)
            except Exception:
                logger.warning("urgent push retry failed", exc_info=True)


# Shared singleton (same pattern as the registry).
_SHARED_SENDER: Dict[str, Any] = {}
_SENDER_LOCK = threading.Lock()


def get_shared_sender(dry_run: bool = False) -> PushSender:
    with _SENDER_LOCK:
        sender = _SHARED_SENDER.get("sender")
        if sender is None:
            # Only affects fresh creation; set_shared_sender() stays authoritative.
            sender = PushSender(get_shared_registry(), dry_run=dry_run)
            _SHARED_SENDER["sender"] = sender
        return sender


def set_shared_sender(sender: PushSender) -> None:
    with _SENDER_LOCK:
        _SHARED_SENDER["sender"] = sender


__all__ = ["PushSender", "get_shared_sender", "set_shared_sender", "DEFAULT_ENDPOINT"]

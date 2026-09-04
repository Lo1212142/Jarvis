"""Proactive watcher — Jarvis contacts the user when something matters.

Subscribes to the server's ``EventBus`` and pushes to registered phones on
important events. Nothing here blocks the bus: every notification is handed
to a bounded daemon worker (queue of 32) and delivered in the background.

Default topics (persisted, editable via ``PUT /api/mobile/settings``):

===========================  =========  =========  ===========
topic (event)                urgent     cooldown   default
===========================  =========  =========  ===========
resource_alert               yes        15 min     ON
security_alert               yes        1 min      ON
security_block               yes        1 min      ON
scheduler_failure            no         10 min     ON
channel_message              no         2 min      ON
tool_timeout                 no         5 min      off
loop_guard_triggered         no         5 min      off
capability_denied            no         5 min      off
taint_violation              yes        1 min      off
===========================  =========  =========  ===========

Per-topic cooldowns stop alert storms (a flapping monitor otherwise pushes
every few seconds). Quiet hours apply to non-urgent topics; urgent ones
bypass when the device allows.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from openjarvis.mobile.push_registry import get_shared_registry
from openjarvis.mobile.push_sender import get_shared_sender

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 32

DEFAULT_TOPICS: Dict[str, Dict[str, Any]] = {
    "resource_alert": {"enabled": True, "urgent": True, "cooldown": 900,
                       "title": "⚠ Server resource alert"},
    "security_alert": {"enabled": True, "urgent": True, "cooldown": 60,
                       "title": "🛡 Security alert"},
    "security_block": {"enabled": True, "urgent": True, "cooldown": 60,
                       "title": "🛡 Security block"},
    "scheduler_failure": {"enabled": True, "urgent": False, "cooldown": 600,
                          "title": "Scheduled task failed"},
    "channel_message": {"enabled": True, "urgent": False, "cooldown": 120,
                        "title": "New channel message"},
    "tool_timeout": {"enabled": False, "urgent": False, "cooldown": 300,
                     "title": "Tool timeout"},
    "loop_guard_triggered": {"enabled": False, "urgent": False, "cooldown": 300,
                             "title": "Loop guard triggered"},
    "capability_denied": {"enabled": False, "urgent": False, "cooldown": 300,
                          "title": "Capability denied"},
    "taint_violation": {"enabled": False, "urgent": True, "cooldown": 60,
                        "title": "🛡 Taint violation"},
}


def _deep_get(data: Dict[str, Any], *keys: str, default: str = "") -> str:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return str(value or default)


def describe_event(topic: str, data: Dict[str, Any]) -> str:
    """Human one-liner for an event payload (defensive — never raises)."""
    try:
        if topic == "resource_alert":
            level = _deep_get(data, "level") or "alert"
            message = _deep_get(data, "message") or _deep_get(data, "detail")
            return message or f"server resource {level}"
        if topic in ("security_alert", "security_block", "taint_violation"):
            return (_deep_get(data, "message") or _deep_get(data, "detail")
                    or _deep_get(data, "reason") or "security event")
        if topic == "scheduler_failure":
            name = _deep_get(data, "task") or _deep_get(data, "name") or _deep_get(data, "id")
            error = _deep_get(data, "error") or _deep_get(data, "reason")
            return (f"task {name}" if name else "scheduled task") + \
                   (f": {error}" if error else " failed")
        if topic == "channel_message":
            channel = _deep_get(data, "channel") or _deep_get(data, "source")
            sender = _deep_get(data, "sender") or _deep_get(data, "from")
            preview = _deep_get(data, "text") or _deep_get(data, "message") or _deep_get(data, "body")
            head = " via " + channel if channel else ""
            who = f" from {sender}" if sender else ""
            return f"new message{head}{who}: {preview[:120]}" if preview else f"new message{head}{who}"
        if topic == "tool_timeout":
            return f"tool {_deep_get(data, 'tool') or '?'} timed out"
        if topic == "loop_guard_triggered":
            return _deep_get(data, "message") or "loop guard triggered"
        if topic == "capability_denied":
            return f"capability denied: {_deep_get(data, 'capability') or '?'}"
    except Exception:
        logger.debug("event description failed", exc_info=True)
    return "see server logs"


class ProactiveDispatcher:
    """Bounded background delivery of event-driven notifications."""

    def __init__(self, registry: Any = None, sender: Any = None) -> None:
        self.registry = registry or get_shared_registry()
        self.sender = sender or get_shared_sender()
        self._cooldowns: Dict[str, float] = {}
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_LIMIT)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._enabled = True
        self._recent: List[Dict[str, Any]] = []
        config = self.registry.proactive_config() or {}
        self.config: Dict[str, Dict[str, Any]] = {
            name: {**settings, **(config.get(name) or {})}
            for name, settings in DEFAULT_TOPICS.items()
        }
        if not isinstance(config, dict) or not config:
            self._persist_defaults()

    def _persist_defaults(self) -> None:
        try:
            self.registry.set_proactive_config(
                {name: {k: v for k, v in settings.items() if k != "title"}
                 for name, settings in self.config.items()})
        except Exception:
            logger.debug("proactive config persist failed", exc_info=True)

    # ---------------------------------------------------------------- public
    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._loop,
                                            name="openjarvis-proactive-push",
                                            daemon=True)
            self._worker.start()

    def stop(self) -> None:
        self._enabled = False
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    def handle(self, topic: str, data: Optional[Dict[str, Any]]) -> None:
        """Event-bus callback — filters, then queues for the worker."""
        if not self._enabled or topic not in self.config:
            return
        settings = self.config[topic]
        if not settings.get("enabled"):
            return
        now = time.time()
        cooldown = float(settings.get("cooldown") or 60)
        last = self._cooldowns.get(topic, 0.0)
        if now - last < cooldown:
            return
        self._cooldowns[topic] = now
        item = {"topic": topic, "data": dict(data or {}),
                "urgent": bool(settings.get("urgent")),
                "title": str(settings.get("title") or "Jarivs")}
        try:
            self._queue.put_nowait(item)
            self.start()
        except queue.Full:
            logger.debug("proactive queue full — dropping %s", topic)

    def update_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        for name, override in (config or {}).items():
            if name not in self.config or not isinstance(override, dict):
                continue
            self.config[name].update(override)
        self._persist_defaults()
        return self.status()

    def status(self) -> Dict[str, Any]:
        return {"enabled": self._enabled,
                "queue_depth": self._queue.qsize(),
                "topics": {name: {k: v for k, v in settings.items() if k != "title"}
                           for name, settings in self.config.items()},
                "recent": self._recent}

    # --------------------------------------------------------------- private
    def _loop(self) -> None:
        while self._enabled:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._deliver(item)
            except Exception:
                logger.warning("proactive delivery failed", exc_info=True)

    def _deliver(self, item: Dict[str, Any]) -> None:
        topic = item["topic"]
        body = describe_event(topic, item.get("data") or {})
        report = self.sender.notify(title=item["title"], body=body,
                                    data={"category": "proactive",
                                          "topic": topic,
                                          "route": "operations"},
                                    urgent=bool(item.get("urgent")))
        self._recent.append({"topic": topic, "body": body[:120],
                             "delivered": report.get("delivered", 0),
                             "at": time.time()})
        self._recent = self._recent[-50:]
        if report.get("delivered"):
            logger.info("proactive push (%s) delivered: %s", topic, body[:80])
        elif report.get("targeted"):
            logger.warning("proactive push (%s) failed: %s", topic,
                           report.get("results"))


_DISPATCHER: Dict[str, Any] = {}
_DISPATCHER_LOCK = threading.Lock()

_EVENT_MAP = {
    "RESOURCE_ALERT": "resource_alert",
    "SECURITY_ALERT": "security_alert",
    "SECURITY_BLOCK": "security_block",
    "SCHEDULER_TASK_END": "scheduler_failure",
    "CHANNEL_MESSAGE_RECEIVED": "channel_message",
    "TOOL_TIMEOUT": "tool_timeout",
    "LOOP_GUARD_TRIGGERED": "loop_guard_triggered",
    "CAPABILITY_DENIED": "capability_denied",
    "TAINT_VIOLATION": "taint_violation",
}


def get_dispatcher() -> ProactiveDispatcher:
    with _DISPATCHER_LOCK:
        dispatcher = _DISPATCHER.get("dispatcher")
        if dispatcher is None:
            dispatcher = ProactiveDispatcher()
            _DISPATCHER["dispatcher"] = dispatcher
        return dispatcher


def install_proactive_listeners(bus: Any) -> Dict[str, Any]:
    """Subscribe the dispatcher to the event bus (idempotent)."""
    if bus is None:
        return {"installed": False, "reason": "no event bus"}
    dispatcher = get_dispatcher()
    already = getattr(bus, "_openjarvis_mobile_proactive", False)
    if already:
        return {"installed": True, "already": True, "topics": list(dispatcher.config)}
    from openjarvis.core.events import EventType

    def _make(topic: str):
        def _callback(event: Any) -> None:
            payload = getattr(event, "data", None)
            data = payload if isinstance(payload, dict) else {"value": str(payload)}
            if topic == "scheduler_failure":
                # Only failure ends are interesting; a completed task is not.
                text = str(data)
                lowered = text.lower()
                if not any(word in lowered for word in
                           ("fail", "error", "cancel", "exception", "crash")):
                    return
            dispatcher.handle(topic, data)
        return _callback

    mounted: List[str] = []
    for attr, topic in _EVENT_MAP.items():
        event_type = getattr(EventType, attr, None)
        if event_type is None:
            continue
        try:
            bus.subscribe(event_type, _make(topic))
            mounted.append(topic)
        except Exception:
            logger.debug("subscribe %s failed", topic, exc_info=True)
    dispatcher.start()
    bus._openjarvis_mobile_proactive = True
    return {"installed": True, "topics": mounted}


__all__ = ["ProactiveDispatcher", "install_proactive_listeners",
           "get_dispatcher", "DEFAULT_TOPICS", "describe_event"]

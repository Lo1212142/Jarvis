"""Persisted registry of push-capable paired devices.

Stores one record per paired device (keyed by the server-side ``device_id``
issued by ``PairingService``) plus the per-device notification settings:

* Expo push token (opaque string; format-validated but not network-verified)
* quiet hours (minutes-of-day window, supports overnight wrap 23:00→07:00)
* urgent-bypass flag (may urgent alerts ring during quiet hours?)
* enable/disable without unpairing
* delivery counters + last status for the status endpoint

Storage: ``~/.openjarvis/mobile_push.json`` (0600, atomic replace) next to
the existing pairing/devices.json. The proactive-watcher configuration is
persisted under the ``proactive`` key in the same file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STORAGE_NAME = "mobile_push.json"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-=\[\].:]{16,256}$")

# Ring-buffer size for recent deliveries kept in memory (diagnostics only).
_HISTORY_LIMIT = 100


def storage_path() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / _STORAGE_NAME


def minutes_of_day(timestamp: Optional[float] = None) -> int:
    """Local minutes since midnight for *timestamp* (default: now)."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(timestamp if timestamp is not None else time.time())
    return moment.hour * 60 + moment.minute


def in_quiet_hours(start_min: Optional[int], end_min: Optional[int],
                   now_min: Optional[int] = None) -> bool:
    """True when *now_min* falls inside the quiet window [start, end).

    The window supports overnight wrap (e.g. 23:00→07:00). ``None`` bounds or
    a zero-length window disable quiet hours entirely.
    """
    if start_min is None or end_min is None:
        return False
    if start_min == end_min:
        return False
    current = minutes_of_day() if now_min is None else now_min
    start_min = int(start_min) % 1440
    end_min = int(end_min) % 1440
    if start_min < end_min:
        return start_min <= current < end_min
    return current >= start_min or current < end_min


def _normalise_minutes(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number % 1440 if 0 <= number < 1440 * 2 else None


def _new_record(device_id: str, token: str, name: str) -> Dict[str, Any]:
    now = time.time()
    return {
        "device_id": str(device_id)[:128],
        "expo_push_token": str(token)[:256],
        "device_name": str(name)[:128],
        "enabled": True,
        "quiet_start_minutes": None,
        "quiet_end_minutes": None,
        "urgent_bypass": True,
        "registered_at": now,
        "last_seen_at": now,
        "last_push_at": None,
        "last_status": "registered",
        "delivered": 0,
        "failed": 0,
    }


class PushRegistry:
    """Thread-safe, file-backed registry of push devices + settings."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else storage_path()
        self._lock = threading.RLock()
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._proactive: Dict[str, Any] = {}
        self.history: List[Dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("mobile push registry unreadable: %s", self._path)
            return
        if isinstance(payload, dict):
            raw_devices = payload.get("devices")
            proactive = payload.get("proactive")
        else:  # legacy/plain list
            raw_devices = payload
            proactive = None
        if isinstance(raw_devices, list):
            for item in raw_devices:
                if not isinstance(item, dict):
                    continue
                device_id = str(item.get("device_id", ""))[:128]
                token = str(item.get("expo_push_token", ""))[:256]
                if not device_id or not token:
                    continue
                record = _new_record(device_id, token, str(item.get("device_name", ""))[:128])
                for key in ("enabled", "urgent_bypass"):
                    if key in item:
                        record[key] = bool(item[key])
                for key in ("quiet_start_minutes", "quiet_end_minutes"):
                    record[key] = _normalise_minutes(item.get(key))
                for key in ("registered_at", "last_seen_at", "last_push_at"):
                    try:
                        if item.get(key) is not None:
                            record[key] = float(item[key])
                    except (TypeError, ValueError):
                        pass
                for key in ("delivered", "failed"):
                    try:
                        record[key] = int(item.get(key, 0))
                    except (TypeError, ValueError):
                        pass
                if item.get("last_status"):
                    record["last_status"] = str(item["last_status"])[:64]
                self._devices[device_id] = record
        if isinstance(proactive, dict):
            self._proactive = dict(proactive)

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "devices": list(self._devices.values()),
                       "proactive": self._proactive}
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")),
                                 encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
            os.chmod(self._path, 0o600)
        except OSError:
            logger.warning("mobile push registry persist failed", exc_info=True)

    # --------------------------------------------------------------- devices
    def register(self, device_id: str, token: str, name: str = "") -> Dict[str, Any]:
        token = str(token or "").strip()
        if not _TOKEN_RE.match(token):
            raise ValueError("expo_push_token must be 16-256 safe characters")
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is not None:
                record["expo_push_token"] = token[:256]
                record["device_name"] = str(name or record["device_name"])[:128]
                record["enabled"] = True
                record["last_seen_at"] = time.time()
                record["last_status"] = "registered"
            else:
                record = _new_record(str(device_id), token, str(name))
                self._devices[record["device_id"]] = record
            self._persist()
            return dict(record)

    def unregister(self, device_id: str) -> bool:
        with self._lock:
            existed = self._devices.pop(str(device_id), None) is not None
            if existed:
                self._persist()
            return existed

    def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._devices.get(str(device_id))
            return dict(record) if record else None

    def touch(self, device_id: str) -> None:
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is not None:
                record["last_seen_at"] = time.time()

    def active_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._devices.values()
                    if r["enabled"] and r.get("expo_push_token")]

    def all_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._devices.values()]

    def set_quiet_hours(self, device_id: str, start_min: Optional[int],
                        end_min: Optional[int], urgent_bypass: bool = True) -> Optional[Dict[str, Any]]:
        start = _normalise_minutes(start_min)
        end = _normalise_minutes(end_min)
        if (start is None) != (end is None):
            raise ValueError("quiet hours need both start and end (or neither)")
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is None:
                return None
            record["quiet_start_minutes"] = start
            record["quiet_end_minutes"] = end
            record["urgent_bypass"] = bool(urgent_bypass)
            self._persist()
            return dict(record)

    def set_enabled(self, device_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is None:
                return None
            record["enabled"] = bool(enabled)
            self._persist()
            return dict(record)

    def record_delivery(self, device_id: str, ok: bool, status: str = "") -> None:
        now = time.time()
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is not None:
                record["last_push_at"] = now
                record["last_status"] = str(status or ("ok" if ok else "failed"))[:64]
                if ok:
                    record["delivered"] = int(record.get("delivered", 0)) + 1
                else:
                    record["failed"] = int(record.get("failed", 0)) + 1
            self.history.append({"device_id": str(device_id), "ok": bool(ok),
                                 "status": str(status)[:64], "at": now})
            self.history = self.history[-_HISTORY_LIMIT:]
            if record is not None:
                self._persist()

    def prune_device(self, device_id: str, reason: str = "DeviceNotRegistered") -> None:
        """Disable a token the push service says is dead (keep the record)."""
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is not None:
                record["enabled"] = False
                record["last_status"] = str(reason)[:64]
                self._persist()
                logger.info("push token for %s pruned: %s", device_id, reason)

    # ------------------------------------------------------------- proactive
    def proactive_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._proactive)

    def set_proactive_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._proactive = dict(config)
            self._persist()
            return dict(self._proactive)

    # ---------------------------------------------------------------- status
    def stats(self) -> Dict[str, Any]:
        records = self.active_records()
        now_min = minutes_of_day()
        quiet_now = [r["device_id"] for r in records
                     if in_quiet_hours(r["quiet_start_minutes"], r["quiet_end_minutes"], now_min)]
        return {
            "devices_total": len(self._devices),
            "devices_active": len(records),
            "devices_quiet_now": quiet_now,
            "recent_deliveries": len(self.history),
            "storage": str(self._path),
        }


# Shared singletons (bound to app.state by install_mobile_routes; usable
# standalone by tools even before the app installs the routes).
_SHARED: Dict[str, Any] = {}
_SHARED_LOCK = threading.Lock()


def get_shared_registry() -> PushRegistry:
    with _SHARED_LOCK:
        registry = _SHARED.get("registry")
        if registry is None:
            registry = PushRegistry()
            _SHARED["registry"] = registry
        return registry


def set_shared_registry(registry: PushRegistry) -> None:
    with _SHARED_LOCK:
        _SHARED["registry"] = registry


__all__ = ["PushRegistry", "get_shared_registry", "set_shared_registry",
           "in_quiet_hours", "minutes_of_day", "storage_path"]

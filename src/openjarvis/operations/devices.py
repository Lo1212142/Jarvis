"""Allowlist and approval gate for owned-device commands.

This module intentionally does not open sockets. A transport adapter such as
Home Assistant or MQTT must be injected by deployment code after approval.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class OwnedDevice:
    device_id: str
    name: str
    transport: str
    capabilities: set[str] = field(default_factory=set)
    paired: bool = False
    last_state: dict[str, Any] = field(default_factory=dict)


class DeviceCommandGate:
    def __init__(self, *, sender: Callable[[OwnedDevice, str, dict[str, Any]], bool] | None = None) -> None:
        self.sender = sender
        self.devices: dict[str, OwnedDevice] = {}
        self._pending: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self.audit: list[dict[str, Any]] = []

    def pair(self, name: str, transport: str, capabilities: set[str], *, device_id: str | None = None) -> OwnedDevice:
        device = OwnedDevice(device_id or uuid.uuid4().hex, name[:128], transport[:32], {str(item)[:64] for item in capabilities}, paired=True)
        with self._lock:
            self.devices[device.device_id] = device
            self._record("paired", {"device_id": device.device_id, "transport": device.transport})
        return device

    def request(self, device_id: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if device is None or not device.paired:
                return {"allowed": False, "reason": "device is not paired"}
            if command not in device.capabilities:
                return {"allowed": False, "reason": "command is not in device allowlist"}
            approval_id = uuid.uuid4().hex
            self._pending[approval_id] = (device_id, command, dict(params or {}))
            self._record("approval_required", {"approval_id": approval_id, "device_id": device_id, "command": command})
            return {"allowed": False, "approval_required": True, "approval_id": approval_id}

    def approve(self, approval_id: str) -> bool:
        with self._lock:
            pending = self._pending.pop(approval_id, None)
            if pending is None:
                return False
            device_id, command, params = pending
            device = self.devices.get(device_id)
            if device is None or self.sender is None:
                self._record("approval_failed", {"approval_id": approval_id})
                return False
            ok = bool(self.sender(device, command, params))
            self._record("command_sent" if ok else "command_failed", {"approval_id": approval_id, "device_id": device_id, "command": command})
            return ok

    def update_state(self, device_id: str, state: dict[str, Any]) -> bool:
        with self._lock:
            device = self.devices.get(device_id)
            if device is None or not device.paired:
                return False
            device.last_state = {str(key)[:64]: value for key, value in list(state.items())[:100]}
            self._record("state_updated", {"device_id": device_id, "keys": list(device.last_state)})
            return True

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        self.audit.append({"event": event, **payload})
        self.audit = self.audit[-500:]


__all__ = ["DeviceCommandGate", "OwnedDevice"]

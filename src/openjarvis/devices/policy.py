"""Allowlist and approval policy for user-owned device control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActionRisk = Literal["read", "low", "high"]
_HIGH_RISK_ACTIONS = {
    "unlock",
    "lock",
    "open",
    "close",
    "turn_on",
    "turn_off",
    "set_temperature",
    "set_alarm",
    "start",
    "stop",
}


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    device_id: str
    action: str
    value: str = ""
    requested_by: str = "user"


@dataclass(frozen=True, slots=True)
class DeviceDecision:
    allowed: bool
    requires_approval: bool
    reason: str


def authorize_device_command(
    command: DeviceCommand,
    *,
    allowlisted_device_ids: set[str],
    approved: bool = False,
) -> DeviceDecision:
    if command.device_id not in allowlisted_device_ids:
        return DeviceDecision(False, True, "device is not on the explicit allowlist")
    if not command.action.strip():
        return DeviceDecision(False, True, "action is empty")
    if command.action in _HIGH_RISK_ACTIONS and not approved:
        return DeviceDecision(False, True, "high-impact device action requires confirmation")
    return DeviceDecision(True, False, "device command is allowed by policy")


__all__ = ["DeviceCommand", "DeviceDecision", "authorize_device_command"]

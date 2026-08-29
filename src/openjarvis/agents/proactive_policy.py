"""Policy helpers for bounded proactive check-ins and voice notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    allowed: bool
    reason: str
    channel: str


def _parse_clock(value: str) -> time:
    hour, minute = (int(part) for part in value.strip().split(":", 1))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("clock value must be HH:MM")
    return time(hour, minute)


def _in_quiet_hours(now: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def should_proactively_notify(
    *,
    now: datetime,
    timezone: str,
    voice_enabled: bool,
    quiet_hours_start: str,
    quiet_hours_end: str,
    cooldown_minutes: int,
    minutes_since_last: float | None,
    messages_today: int,
    max_messages_per_day: int,
    urgent: bool = False,
    client_online: bool = True,
    only_when_client_online: bool = True,
    channel: str = "text",
) -> ProactiveDecision:
    """Return an explainable decision for a proactive notification.

    Urgent notifications bypass quiet hours but still respect client/channel
    availability. All other messages obey the configured cooldown and daily cap.
    """
    if channel == "voice" and not voice_enabled:
        return ProactiveDecision(False, "voice proactive mode is disabled", channel)
    if only_when_client_online and not client_online:
        return ProactiveDecision(False, "client is offline", channel)
    if max_messages_per_day >= 0 and messages_today >= max_messages_per_day and not urgent:
        return ProactiveDecision(False, "daily proactive message cap reached", channel)

    local_now = now.astimezone(ZoneInfo(timezone))
    if not urgent and _in_quiet_hours(
        local_now.time(), _parse_clock(quiet_hours_start), _parse_clock(quiet_hours_end)
    ):
        return ProactiveDecision(False, "inside quiet hours", channel)
    if (
        not urgent
        and minutes_since_last is not None
        and minutes_since_last < max(0, cooldown_minutes)
    ):
        return ProactiveDecision(False, "cooldown has not elapsed", channel)
    return ProactiveDecision(True, "allowed by proactive policy", channel)


__all__ = ["ProactiveDecision", "should_proactively_notify"]

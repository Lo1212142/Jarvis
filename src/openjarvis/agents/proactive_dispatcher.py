"""Bounded proactive notification dispatcher.

The dispatcher is deliberately transport-agnostic. A caller must provide an
allowlisted notifier, while this layer enforces the proactive policy and keeps
an explainable audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .proactive_policy import ProactiveDecision, should_proactively_notify


@dataclass(frozen=True, slots=True)
class ProactiveDelivery:
    delivered: bool
    decision: ProactiveDecision
    event_type: str
    message: str
    created_at: str


class ProactiveDispatcher:
    def __init__(
        self,
        *,
        notifier: Callable[[str, str], bool],
        voice_notifier: Callable[[str, str], bool] | None = None,
        timezone: str = "Africa/Cairo",
        voice_enabled: bool = False,
        quiet_hours_start: str = "23:00",
        quiet_hours_end: str = "07:00",
        cooldown_minutes: int = 30,
        max_messages_per_day: int = 10,
        only_when_client_online: bool = True,
    ) -> None:
        self.notifier = notifier
        self.voice_notifier = voice_notifier
        self.timezone = timezone
        self.voice_enabled = voice_enabled
        self.quiet_hours_start = quiet_hours_start
        self.quiet_hours_end = quiet_hours_end
        self.cooldown_minutes = max(0, cooldown_minutes)
        self.max_messages_per_day = max(0, max_messages_per_day)
        self.only_when_client_online = only_when_client_online
        self.messages_today = 0
        self.last_delivery_at: datetime | None = None
        self.audit: list[dict[str, Any]] = []

    def dispatch(
        self,
        *,
        event_type: str,
        message: str,
        now: datetime | None = None,
        channel: str = "text",
        urgent: bool = False,
        client_online: bool = True,
    ) -> ProactiveDelivery:
        current = now or datetime.now(timezone.utc)
        minutes_since_last = None
        if self.last_delivery_at is not None:
            minutes_since_last = max(0.0, (current - self.last_delivery_at).total_seconds() / 60.0)
        decision = should_proactively_notify(
            now=current,
            timezone=self.timezone,
            voice_enabled=self.voice_enabled,
            quiet_hours_start=self.quiet_hours_start,
            quiet_hours_end=self.quiet_hours_end,
            cooldown_minutes=self.cooldown_minutes,
            minutes_since_last=minutes_since_last,
            messages_today=self.messages_today,
            max_messages_per_day=self.max_messages_per_day,
            urgent=urgent,
            client_online=client_online,
            only_when_client_online=self.only_when_client_online,
            channel=channel,
        )
        delivered = False
        if decision.allowed:
            sender = self.voice_notifier if channel == "voice" else self.notifier
            if sender is not None:
                delivered = bool(sender(event_type, message[:10_000]))
            if delivered:
                self.messages_today += 1
                self.last_delivery_at = current
        record = {
            "event_type": event_type,
            "channel": channel,
            "delivered": delivered,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "created_at": current.isoformat(),
        }
        self.audit.append(record)
        self.audit = self.audit[-500:]
        return ProactiveDelivery(delivered, decision, event_type, message, current.isoformat())

    def reset_daily_counter(self) -> None:
        self.messages_today = 0


__all__ = ["ProactiveDelivery", "ProactiveDispatcher"]

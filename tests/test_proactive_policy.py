from datetime import datetime, timezone

from openjarvis.agents.proactive_policy import should_proactively_notify


def _now(hour: int) -> datetime:
    return datetime(2026, 8, 27, hour, 0, tzinfo=timezone.utc)


def test_voice_requires_opt_in() -> None:
    decision = should_proactively_notify(
        now=_now(12),
        timezone="UTC",
        voice_enabled=False,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        cooldown_minutes=0,
        minutes_since_last=None,
        messages_today=0,
        max_messages_per_day=5,
        channel="voice",
    )
    assert not decision.allowed
    assert "disabled" in decision.reason


def test_quiet_hours_block_non_urgent() -> None:
    decision = should_proactively_notify(
        now=_now(23),
        timezone="UTC",
        voice_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        cooldown_minutes=0,
        minutes_since_last=None,
        messages_today=0,
        max_messages_per_day=5,
        channel="voice",
    )
    assert not decision.allowed
    assert "quiet" in decision.reason


def test_urgent_can_bypass_quiet_hours() -> None:
    decision = should_proactively_notify(
        now=_now(23),
        timezone="UTC",
        voice_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        cooldown_minutes=120,
        minutes_since_last=1,
        messages_today=5,
        max_messages_per_day=5,
        urgent=True,
        channel="voice",
    )
    assert decision.allowed


def test_cooldown_blocks_regular_message() -> None:
    decision = should_proactively_notify(
        now=_now(12),
        timezone="UTC",
        voice_enabled=True,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
        cooldown_minutes=120,
        minutes_since_last=10,
        messages_today=0,
        max_messages_per_day=5,
        channel="text",
    )
    assert not decision.allowed
    assert "cooldown" in decision.reason

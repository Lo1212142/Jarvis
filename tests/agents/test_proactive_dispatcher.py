from datetime import datetime

from openjarvis.agents.proactive_dispatcher import ProactiveDispatcher


def test_proactive_dispatcher_delivers_and_audits():
    sent = []
    dispatcher = ProactiveDispatcher(
        notifier=lambda event_type, message: sent.append((event_type, message)) or True,
        timezone="Africa/Cairo",
        cooldown_minutes=0,
        max_messages_per_day=2,
        only_when_client_online=True,
    )
    result = dispatcher.dispatch(event_type="job.completed", message="Build finished", now=datetime(2026, 8, 27, 8, 0))
    assert result.delivered is True
    assert sent == [("job.completed", "Build finished")]
    assert dispatcher.audit[-1]["reason"] == "allowed by proactive policy"


def test_proactive_dispatcher_respects_quiet_cooldown_cap_and_voice():
    sent = []
    voice = []
    dispatcher = ProactiveDispatcher(
        notifier=lambda event_type, message: sent.append(message) or True,
        voice_notifier=lambda event_type, message: voice.append(message) or True,
        timezone="Africa/Cairo",
        voice_enabled=False,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        cooldown_minutes=30,
        max_messages_per_day=1,
    )
    quiet = dispatcher.dispatch(event_type="news", message="quiet", now=datetime(2026, 8, 27, 23, 0))
    assert quiet.delivered is False
    first = dispatcher.dispatch(event_type="news", message="first", now=datetime(2026, 8, 27, 8, 0))
    assert first.delivered is True
    capped = dispatcher.dispatch(event_type="news", message="second", now=datetime(2026, 8, 27, 9, 0))
    assert capped.delivered is False
    disabled_voice = dispatcher.dispatch(event_type="alert", message="voice", channel="voice", urgent=True, now=datetime(2026, 8, 27, 9, 0))
    assert disabled_voice.delivered is False
    assert sent == ["first"]
    assert voice == []

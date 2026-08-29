from pathlib import Path

import pytest

from openjarvis.agents.reminder_service import ReminderStore


def test_reminders_persist_and_change_status(tmp_path: Path) -> None:
    path = tmp_path / "reminders.db"
    store = ReminderStore(path)
    created = store.create(
        prompt="ابدأ مشروع Orion",
        schedule_type="interval",
        schedule_value="60",
        timezone="Africa/Cairo",
    )
    assert created.status == "active"
    assert store.list()[0].prompt == "ابدأ مشروع Orion"
    paused = store.set_status(created.id, "paused")
    assert paused.status == "paused"
    store.close()

    reopened = ReminderStore(path)
    assert reopened.list(status="paused")[0].id == created.id
    reopened.close()


def test_reminder_rejects_too_fast_interval(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "reminders.db")
    with pytest.raises(ValueError):
        store.create(prompt="too fast", schedule_type="interval", schedule_value="1")
    store.close()

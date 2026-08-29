"""Durable reminder/job API backed by the server-side SQLite store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.agents.reminder_service import Reminder, ReminderStore, ReminderWorker
from openjarvis.core.events import EventType
from openjarvis.core.paths import get_config_dir


class ReminderCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    schedule_type: str = Field(default="once", pattern="^(once|interval|cron)$")
    schedule_value: str = Field(min_length=1, max_length=256)
    timezone: str = Field(default="Africa/Cairo", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReminderStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|paused|cancelled)$")


router = APIRouter(prefix="/api/reminders", tags=["reminders"])


def _store(request: Request) -> ReminderStore:
    current = getattr(request.app.state, "reminder_store", None)
    if current is None:
        current = ReminderStore(get_config_dir() / "reminders.db")
        request.app.state.reminder_store = current
    return current


def _json(reminder: Reminder) -> dict[str, Any]:
    return {
        "id": reminder.id,
        "prompt": reminder.prompt,
        "schedule_type": reminder.schedule_type,
        "schedule_value": reminder.schedule_value,
        "timezone": reminder.timezone,
        "status": reminder.status,
        "next_run_at": reminder.next_run_at,
        "metadata": reminder.metadata,
    }


@router.get("")
async def list_reminders(request: Request, status: str | None = None) -> dict[str, Any]:
    if status is not None and status not in {"active", "paused", "cancelled", "completed"}:
        raise HTTPException(status_code=422, detail="invalid status")
    return {"reminders": [_json(item) for item in _store(request).list(status=status)]}


@router.post("")
async def create_reminder(payload: ReminderCreateRequest, request: Request) -> dict[str, Any]:
    try:
        reminder = _store(request).create(
            prompt=payload.prompt,
            schedule_type=payload.schedule_type,
            schedule_value=payload.schedule_value,
            timezone=payload.timezone,
            metadata=payload.metadata,
        )
        return {"reminder": _json(reminder), "persistent": True}
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{reminder_id}")
async def update_reminder(reminder_id: str, payload: ReminderStatusRequest, request: Request) -> dict[str, Any]:
    try:
        return {"reminder": _json(_store(request).set_status(reminder_id, payload.status))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="reminder not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def install_reminder_service(app: Any) -> None:
    """Install one durable worker per server process."""
    if getattr(app.state, "reminder_worker", None) is not None:
        return
    store = ReminderStore(get_config_dir() / "reminders.db")

    def on_due(reminder: Reminder) -> None:
        bus = getattr(app.state, "event_bus", None)
        if bus is not None:
            bus.publish(
                EventType.SCHEDULER_TASK_START,
                {"reminder_id": reminder.id, "prompt": reminder.prompt, "metadata": reminder.metadata},
            )

    worker = ReminderWorker(store, on_due, poll_seconds=5.0)
    app.state.reminder_store = store
    app.state.reminder_worker = worker
    worker.start()

    @app.on_event("shutdown")
    async def _shutdown_reminders() -> None:
        worker.stop()
        store.close()


__all__ = ["install_reminder_service", "router"]

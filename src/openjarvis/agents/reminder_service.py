"""Durable reminder and long-running job scheduler primitives."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    prompt: str
    schedule_type: str
    schedule_value: str
    timezone: str
    status: str
    next_run_at: float
    metadata: dict[str, Any]


class ReminderStore:
    """SQLite-backed store for durable user reminders and jobs."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                timezone TEXT NOT NULL,
                status TEXT NOT NULL,
                next_run_at REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        self._conn.commit()

    def create(
        self,
        *,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        timezone: str = "Africa/Cairo",
        metadata: dict[str, Any] | None = None,
        next_run_at: float | None = None,
    ) -> Reminder:
        if not prompt.strip() or len(prompt) > 4000:
            raise ValueError("prompt must be 1-4000 characters")
        if schedule_type not in {"once", "interval", "cron"}:
            raise ValueError("schedule_type must be once, interval, or cron")
        if schedule_type == "interval" and float(schedule_value) < 60:
            raise ValueError("interval must be at least 60 seconds")
        if schedule_type == "cron":
            try:
                import croniter  # noqa: F401
            except ImportError as exc:
                raise ValueError("cron schedules require croniter") from exc
        ZoneInfo(timezone)
        now = time.time()
        run_at = next_run_at if next_run_at is not None else self._initial_run(schedule_type, schedule_value, timezone, now)
        reminder = Reminder(
            id=uuid.uuid4().hex[:16],
            prompt=prompt.strip(),
            schedule_type=schedule_type,
            schedule_value=str(schedule_value),
            timezone=timezone,
            status="active",
            next_run_at=run_at,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO reminders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reminder.id, reminder.prompt, reminder.schedule_type, reminder.schedule_value, reminder.timezone, reminder.status, reminder.next_run_at, json.dumps(reminder.metadata), now, now),
            )
            self._conn.commit()
        return reminder

    @staticmethod
    def _initial_run(schedule_type: str, schedule_value: str, timezone: str, now: float) -> float:
        if schedule_type == "interval":
            return now + float(schedule_value)
        if schedule_type == "once":
            parsed = datetime.fromisoformat(schedule_value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
            return parsed.timestamp()
        from croniter import croniter
        local = datetime.fromtimestamp(now, tz=ZoneInfo(timezone))
        return croniter(schedule_value, local).get_next(datetime).timestamp()

    def list(self, *, status: str | None = None) -> list[Reminder]:
        query = "SELECT * FROM reminders"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY next_run_at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def due(self, now: float | None = None) -> list[Reminder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'active' AND next_run_at <= ? ORDER BY next_run_at ASC",
                (now or time.time(),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_fired(self, reminder_id: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            if row is None:
                return
            if row["schedule_type"] == "once":
                self._conn.execute("UPDATE reminders SET status = 'completed', updated_at = ? WHERE id = ?", (time.time(), reminder_id))
            else:
                next_run = self._initial_run(row["schedule_type"], row["schedule_value"], row["timezone"], time.time())
                self._conn.execute("UPDATE reminders SET next_run_at = ?, updated_at = ? WHERE id = ?", (next_run, time.time(), reminder_id))
            self._conn.commit()

    def set_status(self, reminder_id: str, status: str) -> Reminder:
        if status not in {"active", "paused", "cancelled"}:
            raise ValueError("invalid reminder status")
        with self._lock:
            self._conn.execute("UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), reminder_id))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        if row is None:
            raise KeyError(reminder_id)
        return self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> Reminder:
        return Reminder(
            id=row["id"], prompt=row["prompt"], schedule_type=row["schedule_type"], schedule_value=row["schedule_value"], timezone=row["timezone"], status=row["status"], next_run_at=float(row["next_run_at"]), metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ReminderWorker:
    """Bounded daemon worker that dispatches due reminders to a callback."""

    def __init__(self, store: ReminderStore, callback: Callable[[Reminder], None], poll_seconds: float = 5.0) -> None:
        self.store = store
        self.callback = callback
        self.poll_seconds = max(1.0, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-reminder-worker")
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            for reminder in self.store.due():
                try:
                    self.callback(reminder)
                finally:
                    self.store.mark_fired(reminder.id)


__all__ = ["Reminder", "ReminderStore", "ReminderWorker"]

"""Durable bounded job state for long-running server work."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_ALLOWED_STATUS = {"queued", "running", "paused", "completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: int
    job_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: str
    prompt: str
    status: str
    progress: float
    checkpoint: dict[str, Any]
    artifacts: list[str]
    error: str = ""


class JobStore:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                checkpoint TEXT NOT NULL DEFAULT '{}',
                artifacts TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self._conn.commit()

    def create(self, *, kind: str, prompt: str) -> Job:
        if not kind.strip() or not prompt.strip():
            raise ValueError("kind and prompt are required")
        job = Job(str(uuid.uuid4()), kind.strip()[:128], prompt.strip()[:4000], "queued", 0.0, {}, [])
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, prompt, status, progress, checkpoint, artifacts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.kind, job.prompt, job.status, job.progress, "{}", "[]"),
            )
            self._record_event_locked(job.id, "queued", {"kind": job.kind})
            self._conn.commit()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._from_row(row)

    def list(self, status: str | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: tuple[Any, ...] = ()
        if status is not None:
            if status not in _ALLOWED_STATUS:
                raise ValueError("invalid job status")
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def record_event(self, job_id: str, event_type: str, payload: dict[str, Any] | None = None) -> JobEvent:
        self.get(job_id)
        with self._lock:
            return self._record_event_locked(job_id, event_type, payload or {})

    def list_events(self, job_id: str, limit: int = 100) -> list[JobEvent]:
        self.get(job_id)
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY id ASC LIMIT ?", (job_id, limit)
            ).fetchall()
        return [JobEvent(int(row["id"]), row["job_id"], row["event_type"], json.loads(row["payload"] or "{}"), float(row["created_at"])) for row in rows]

    def _record_event_locked(self, job_id: str, event_type: str, payload: dict[str, Any]) -> JobEvent:
        if not event_type or len(event_type) > 64:
            raise ValueError("event_type must be 1-64 characters")
        now = __import__("time").time()
        cur = self._conn.execute(
            "INSERT INTO job_events (job_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, json.dumps(payload), now),
        )
        self._conn.commit()
        return JobEvent(int(cur.lastrowid), job_id, event_type, dict(payload), now)

    def claim_next(self, allowed_kinds: set[str]) -> Job | None:
        """Atomically move one queued allowlisted job to running."""
        if not allowed_kinds:
            return None
        with self._lock:
            placeholders = ",".join("?" for _ in allowed_kinds)
            row = self._conn.execute(
                f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN ({placeholders}) ORDER BY created_at ASC LIMIT 1",
                tuple(sorted(allowed_kinds)),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='running', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",
                (row["id"],),
            )
            self._conn.commit()
            job = self._from_row(self._conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            self._record_event_locked(job.id, "running", {})
            return job

    def recover_running(self) -> int:
        """Requeue interrupted work after a process restart; never resumes arbitrary code."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='queued', updated_at=CURRENT_TIMESTAMP WHERE status='running'"
            )
            self._conn.commit()
            return cur.rowcount

    def update(self, job_id: str, *, status: str | None = None, progress: float | None = None,
               checkpoint: dict[str, Any] | None = None, artifacts: list[str] | None = None,
               error: str | None = None) -> Job:
        if status is not None and status not in _ALLOWED_STATUS:
            raise ValueError("invalid job status")
        if progress is not None and not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        current = self.get(job_id)
        next_job = Job(
            current.id, current.kind, current.prompt, status or current.status,
            progress if progress is not None else current.progress,
            checkpoint if checkpoint is not None else current.checkpoint,
            artifacts if artifacts is not None else current.artifacts,
            error if error is not None else current.error,
        )
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, progress=?, checkpoint=?, artifacts=?, error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (next_job.status, next_job.progress, json.dumps(next_job.checkpoint), json.dumps(next_job.artifacts), next_job.error, job_id),
            )
            self._conn.commit()
            if status is not None:
                self._record_event_locked(job_id, status, {"progress": next_job.progress, "error": next_job.error})
            elif progress is not None or checkpoint is not None:
                self._record_event_locked(job_id, "progress", {"progress": next_job.progress, "checkpoint": next_job.checkpoint})
        return next_job

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Job:
        return Job(
            row["id"], row["kind"], row["prompt"], row["status"], float(row["progress"]),
            json.loads(row["checkpoint"] or "{}"), json.loads(row["artifacts"] or "[]"), row["error"] or "",
        )


__all__ = ["Job", "JobEvent", "JobStore"]

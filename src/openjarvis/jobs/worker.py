"""CPU-light, allowlist-only worker for durable jobs.

Handlers are registered by trusted application code. Job prompts are data and
are never passed to a shell, Python evaluator, browser, or connector here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .store import Job, JobStore


ProgressCallback = Callable[[float, dict], None]
JobHandler = Callable[[Job, ProgressCallback], list[str]]


@dataclass(frozen=True, slots=True)
class JobWorkerConfig:
    poll_seconds: float = 1.0
    max_runtime_seconds: float = 900.0


class JobWorker:
    def __init__(self, store: JobStore, handlers: dict[str, JobHandler], config: JobWorkerConfig | None = None) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.config = config or JobWorkerConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_id: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.recover_running()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-job-worker")
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._active_id = None

    def _loop(self) -> None:
        wait = max(0.2, float(self.config.poll_seconds))
        while not self._stop.is_set():
            job = self.store.claim_next(set(self.handlers))
            if job is None:
                self._stop.wait(wait)
                continue
            self._active_id = job.id
            try:
                self._run_one(job)
            finally:
                self._active_id = None

    def _run_one(self, job: Job) -> None:
        started = time.monotonic()
        handler = self.handlers.get(job.kind)
        if handler is None:
            self.store.update(job.id, status="failed", error=f"unsupported job kind: {job.kind}")
            return

        def progress(value: float, checkpoint: dict) -> None:
            current = self.store.get(job.id)
            if current.status in {"paused", "cancelled"}:
                return
            if time.monotonic() - started > self.config.max_runtime_seconds:
                raise TimeoutError("job exceeded runtime budget")
            self.store.update(job.id, progress=max(0.0, min(1.0, value)), checkpoint=checkpoint)

        try:
            current = self.store.get(job.id)
            if current.status in {"paused", "cancelled"}:
                return
            progress(0.0, {"phase": "started"})
            artifacts = handler(job, progress)
            current = self.store.get(job.id)
            if current.status == "cancelled":
                return
            if current.status == "paused":
                return
            self.store.update(job.id, status="completed", progress=1.0, artifacts=list(artifacts or []), checkpoint={"phase": "completed"})
        except TimeoutError as exc:
            self.store.update(job.id, status="failed", error=str(exc))
        except Exception as exc:  # worker isolation: one job cannot stop the daemon
            self.store.update(job.id, status="failed", error=f"handler failure: {type(exc).__name__}: {exc}")


__all__ = ["JobHandler", "JobWorker", "JobWorkerConfig", "ProgressCallback"]

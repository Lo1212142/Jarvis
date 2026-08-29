"""Authenticated API for durable long-running jobs.

This layer persists intent and state. Execution workers consume queued jobs in a
separate controlled runtime; the API never executes user code inline.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.core.paths import get_config_dir
from openjarvis.jobs import Job, JobStore
from openjarvis.jobs.worker import JobWorker, JobWorkerConfig
from openjarvis.jobs.builtin_handlers import file_index_handler, video_transcript_search_handler


class JobCreateRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)


class JobUpdateRequest(BaseModel):
    status: str | None = Field(default=None, pattern="^(queued|running|paused|completed|failed|cancelled)$")
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    checkpoint: dict[str, Any] | None = None
    artifacts: list[str] | None = Field(default=None, max_length=100)
    error: str | None = Field(default=None, max_length=2000)


router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _store(request: Request) -> JobStore:
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        store = JobStore(get_config_dir() / "jobs.db")
        request.app.state.job_store = store
    return store


def _json(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "prompt": job.prompt,
        "status": job.status,
        "progress": job.progress,
        "checkpoint": job.checkpoint,
        "artifacts": job.artifacts,
        "error": job.error,
    }


@router.get("")
async def list_jobs(request: Request, status: str | None = None) -> dict[str, Any]:
    try:
        return {"jobs": [_json(job) for job in _store(request).list(status=status)]}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("")
async def create_job(payload: JobCreateRequest, request: Request) -> dict[str, Any]:
    return {"job": _json(_store(request).create(kind=payload.kind, prompt=payload.prompt)), "persistent": True}


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        return {"job": _json(_store(request).get(job_id))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.get("/{job_id}/events")
async def get_job_events(job_id: str, request: Request, limit: int = 100) -> dict[str, Any]:
    try:
        events = _store(request).list_events(job_id, limit=limit)
        return {
            "events": [
                {"id": event.id, "job_id": event.job_id, "event_type": event.event_type, "payload": event.payload, "created_at": event.created_at}
                for event in events
            ]
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{job_id}")
async def update_job(job_id: str, payload: JobUpdateRequest, request: Request) -> dict[str, Any]:
    try:
        job = _store(request).update(**payload.model_dump(exclude_none=True), job_id=job_id)
        return {"job": _json(job)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _health_check_handler(job: Job, progress: Any) -> list[str]:
    progress(0.5, {"phase": "health-check", "prompt_length": len(job.prompt)})
    progress(1.0, {"phase": "completed"})
    return []


def install_job_store(app: Any) -> None:
    if getattr(app.state, "job_store", None) is not None:
        return
    store = JobStore(get_config_dir() / "jobs.db")
    app.state.job_store = store
    worker = JobWorker(
        store,
        {
            "health_check": _health_check_handler,
            "file_index": file_index_handler,
            "video_transcript_search": video_transcript_search_handler,
        },
        JobWorkerConfig(poll_seconds=1.0, max_runtime_seconds=60.0),
    )
    app.state.job_worker = worker
    worker.start()

    @app.on_event("shutdown")
    async def _shutdown_jobs() -> None:
        worker.stop()
        store.close()


__all__ = ["install_job_store", "router"]

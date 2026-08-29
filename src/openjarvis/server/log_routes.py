"""Authenticated read-only Log Center API."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from openjarvis.monitoring.log_center import LogCenter

router = APIRouter(prefix="/api/logs", tags=["logs"])


class LogSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    relative_path: str = Field(min_length=1, max_length=512)


def _root() -> Path:
    root = Path(os.environ.get("OPENJARVIS_LOG_ROOT", tempfile.gettempdir())) / "openjarvis-logs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _center(request: Request) -> LogCenter:
    center = getattr(request.app.state, "log_center", None)
    if center is None:
        center = LogCenter(_root())
        request.app.state.log_center = center
    return center


@router.get("/sources")
async def list_sources(request: Request) -> dict[str, Any]:
    sources = _center(request).sources()
    # A paired phone does not need filesystem layout.  Return names only so a
    # device-token response cannot disclose server paths.
    if getattr(request.state, "device_id", None):
        return {"sources": [{"name": item.name} for item in sources]}
    return {"sources": [{"name": item.name, "path": item.path} for item in sources]}


@router.post("/sources")
async def register_source(payload: LogSourceRequest, request: Request) -> dict[str, Any]:
    if getattr(request.state, "device_id", None):
        raise HTTPException(status_code=403, detail="Paired devices cannot register log sources")
    try:
        target = (_root() / payload.relative_path).resolve()
        item = _center(request).register_source(payload.name, target)
        return {"name": item.name, "path": item.path}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{source_name}/search")
async def search_logs(
    source_name: str,
    request: Request,
    contains: str = Query(default="", max_length=256),
    level: str = Query(default="", max_length=16),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        return {"source": source_name, "entries": _center(request).read(source_name, contains=contains, level=level, limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{source_name}/tail")
async def tail_logs(source_name: str, request: Request, offset: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    try:
        return _center(request).tail(source_name, offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{source_name}/incident")
async def incident_summary(source_name: str, request: Request, limit: int = Query(default=2000, ge=1, le=2000)) -> dict[str, Any]:
    try:
        return _center(request).incident(source_name, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


__all__ = ["router"]

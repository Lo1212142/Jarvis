"""Authenticated public conflict-news API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.monitoring.conflict_news import ConflictNewsService, ConflictNewsUnavailable, DEFAULT_CONFLICT_QUERY


class ConflictNewsConfigPatch(BaseModel):
    enabled: bool | None = None
    cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    stale_after_seconds: float | None = Field(default=None, ge=30.0, le=604800.0)
    max_items: int | None = Field(default=None, ge=1, le=100)


router = APIRouter(prefix="/api/news/conflicts", tags=["conflict-news"])


def get_service(request: Request) -> ConflictNewsService:
    service = getattr(request.app.state, "conflict_news_service", None)
    if not isinstance(service, ConflictNewsService):
        service = ConflictNewsService()
        request.app.state.conflict_news_service = service
    return service


@router.get("")
async def latest_conflict_news(request: Request, query: str = DEFAULT_CONFLICT_QUERY, refresh: bool = False) -> dict[str, Any]:
    if not bool(getattr(request.app.state, "conflict_news_enabled", True)):
        raise HTTPException(status_code=409, detail="conflict news is disabled in settings")
    try:
        snapshot = get_service(request).latest(query=query, force_refresh=refresh)
        return {"news": snapshot.to_dict(), "enabled": True}
    except ConflictNewsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/config")
async def conflict_news_config(patch: ConflictNewsConfigPatch, request: Request) -> dict[str, Any]:
    service = get_service(request)
    changes = patch.model_dump(exclude_none=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        request.app.state.conflict_news_enabled = bool(enabled)
    service.configure(**changes)
    return {"enabled": bool(getattr(request.app.state, "conflict_news_enabled", True)), "cache_ttl_seconds": service.cache_ttl_seconds, "stale_after_seconds": service.stale_after_seconds, "max_items": service.max_items}


__all__ = ["router"]

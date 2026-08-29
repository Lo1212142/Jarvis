"""Authenticated movie and series discovery API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.media.catalog import MediaCatalogService, MediaCatalogUnavailable


class CatalogSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    media_type: str = Field(default="all", pattern="^(all|movie|series)$")
    language: str = Field(default="en-US", max_length=32)
    include_summary: bool = False
    refresh: bool = False


class CatalogConfigPatch(BaseModel):
    enabled: bool | None = None
    cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    max_items: int | None = Field(default=None, ge=1, le=50)


router = APIRouter(prefix="/api/catalog", tags=["media-catalog"])


def get_service(request: Request) -> MediaCatalogService:
    service = getattr(request.app.state, "media_catalog_service", None)
    if not isinstance(service, MediaCatalogService):
        service = MediaCatalogService()
        request.app.state.media_catalog_service = service
    return service


@router.post("/search")
async def catalog_search(body: CatalogSearchRequest, request: Request) -> dict[str, Any]:
    if not bool(getattr(request.app.state, "media_catalog_enabled", True)):
        raise HTTPException(status_code=409, detail="media catalog is disabled in settings")
    try:
        result = get_service(request).search(body.query, media_type=body.media_type, language=body.language, include_summary=body.include_summary, force_refresh=body.refresh)
        return {"catalog": result.to_dict(include_summary=body.include_summary), "spoilers": body.include_summary}
    except (ValueError, MediaCatalogUnavailable) as exc:
        raise HTTPException(status_code=503 if isinstance(exc, MediaCatalogUnavailable) else 422, detail=str(exc)) from exc


@router.patch("/config")
async def catalog_config(patch: CatalogConfigPatch, request: Request) -> dict[str, Any]:
    service = get_service(request)
    changes = patch.model_dump(exclude_none=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        request.app.state.media_catalog_enabled = bool(enabled)
    service.configure(**changes)
    return {"enabled": bool(getattr(request.app.state, "media_catalog_enabled", True)), "cache_ttl_seconds": service.cache_ttl_seconds, "max_items": service.max_items, "tmdb_configured": bool(service.tmdb_api_key)}


__all__ = ["router"]

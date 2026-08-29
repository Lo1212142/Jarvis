"""Authenticated Cairo weather endpoint backed by Open-Meteo."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.monitoring.weather import CairoWeatherService, WeatherUnavailable


class WeatherConfigPatch(BaseModel):
    enabled: bool | None = None
    cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    stale_after_seconds: float | None = Field(default=None, ge=30.0, le=604800.0)


router = APIRouter(prefix="/api/weather", tags=["weather"])


def get_service(request: Request) -> CairoWeatherService:
    service = getattr(request.app.state, "weather_service", None)
    if not isinstance(service, CairoWeatherService):
        service = CairoWeatherService()
        request.app.state.weather_service = service
    return service


@router.get("/cairo")
async def cairo_weather(request: Request, refresh: bool = False) -> dict[str, Any]:
    if not bool(getattr(request.app.state, "weather_enabled", True)):
        raise HTTPException(status_code=409, detail="Cairo weather is disabled in settings")
    try:
        snapshot = get_service(request).current_cairo(force_refresh=refresh)
        return {"weather": snapshot.to_dict(), "enabled": True}
    except WeatherUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/config")
async def weather_config(patch: WeatherConfigPatch, request: Request) -> dict[str, Any]:
    service = get_service(request)
    changes = patch.model_dump(exclude_none=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        request.app.state.weather_enabled = bool(enabled)
    service.configure(**changes)
    return {"enabled": bool(getattr(request.app.state, "weather_enabled", True)), "cache_ttl_seconds": service.cache_ttl_seconds, "stale_after_seconds": service.stale_after_seconds}


__all__ = ["router"]

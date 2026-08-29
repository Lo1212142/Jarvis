"""Authenticated resource monitoring endpoints for the Jarvis server."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.monitoring.resource_monitor import ResourceMonitor


class ResourceMonitorPatch(BaseModel):
    enabled: bool | None = None
    poll_interval_seconds: float | None = Field(default=None, ge=1.0, le=3600.0)
    cpu_alert_percent: float | None = Field(default=None, ge=1.0, le=1000.0)
    memory_alert_percent: float | None = Field(default=None, ge=1.0, le=100.0)
    alert_cooldown_seconds: float | None = Field(default=None, ge=0.0, le=86400.0)


router = APIRouter(prefix="/api/resources", tags=["resources"])


def get_monitor(request: Request) -> ResourceMonitor:
    monitor = getattr(request.app.state, "resource_monitor", None)
    if not isinstance(monitor, ResourceMonitor):
        monitor = ResourceMonitor()
        request.app.state.resource_monitor = monitor
    return monitor


def _nim_limiter_status(request: Request) -> dict[str, Any] | None:
    """Expose only real limiter counters; never infer provider activity."""
    getter = getattr(getattr(request.app.state, "engine", None), "rate_limit_snapshot", None)
    if not callable(getter):
        return None
    try:
        snapshot = getter()
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    return {key: snapshot[key] for key in ("limit", "used", "remaining", "window_seconds", "reset_after_seconds") if key in snapshot}


@router.get("/current")
async def current_resources(request: Request) -> dict[str, Any]:
    monitor = get_monitor(request)
    snapshot = monitor.current()
    return {
        "snapshot": snapshot.to_dict(),
        "monitor": {"enabled": bool(getattr(request.app.state, "resource_monitor_enabled", True)), **monitor.config()},
        "alerts": monitor.alerts(20),
        "nim_limiter": _nim_limiter_status(request),
    }


@router.get("/history")
async def resource_history(request: Request, limit: int = 60) -> dict[str, Any]:
    if limit < 1 or limit > 240:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 240")
    return {"history": get_monitor(request).history(limit)}


@router.get("/alerts")
async def resource_alerts(request: Request, limit: int = 20) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return {"alerts": get_monitor(request).alerts(limit)}


@router.post("/alerts/drain")
async def drain_resource_alerts(request: Request, limit: int = 20) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return {"alerts": get_monitor(request).drain_alerts(limit)}


@router.patch("/config")
async def patch_resource_config(patch: ResourceMonitorPatch, request: Request) -> dict[str, Any]:
    monitor = get_monitor(request)
    changes = patch.model_dump(exclude_none=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        request.app.state.resource_monitor_enabled = bool(enabled)
        if enabled:
            monitor.start()
        else:
            monitor.stop()
    monitor.configure(**changes)
    return {
        "monitor": {"enabled": bool(getattr(request.app.state, "resource_monitor_enabled", True)), **monitor.config()},
        "snapshot": monitor.current().to_dict(),
        "alerts": monitor.alerts(20),
    }


__all__ = ["ResourceMonitorPatch", "router"]

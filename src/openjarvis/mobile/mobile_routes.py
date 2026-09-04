"""``/api/mobile`` routes + the one-call installer for the whole package.

Endpoints (device-token authenticated via the existing ``AuthMiddleware``
after the one-line scope mapping — see INTEGRATION.md):

* ``POST /api/mobile/push-token``    — register this device's Expo push token
* ``DELETE /api/mobile/push-token``  — unregister this device
* ``GET  /api/mobile/me``            — this device's push record + settings
* ``PUT  /api/mobile/settings``      — quiet hours / urgent bypass / enabled
* ``POST /api/mobile/test``          — send a test push to this device
* ``GET  /api/mobile/status``        — admin overview (global key only)
* ``POST /api/mobile/hotload``       — re-import tools into the RUNNING server

``install_mobile_routes(app)`` is idempotent: it imports the tool modules
(registration side effect), binds the shared registry/sender to
``app.state``, mounts the router, starts the proactive watcher on the
event bus, and injects the three tools into every live agent (via the
creative suite's runtime registry — the agent rebuilds its tool list from
``_tools`` on the next turn, so they are callable immediately).
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.core.registry import ToolRegistry
from openjarvis.mobile.push_registry import (
    PushRegistry,
    get_shared_registry,
    set_shared_registry,
)
from openjarvis.mobile.push_sender import get_shared_sender, set_shared_sender
from openjarvis.mobile.proactive import get_dispatcher, install_proactive_listeners

logger = logging.getLogger(__name__)

MOBILE_TOOLS = ["notify_user", "alert_user", "mobile_devices_status"]
_MODULE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")

router = APIRouter(prefix="/api/mobile", tags=["mobile"])


# --------------------------------------------------------------------- models
class PushTokenRequest(BaseModel):
    expo_push_token: str = Field(min_length=16, max_length=256)
    device_name: str = Field(default="", max_length=128)


class PushSettingsRequest(BaseModel):
    quiet_start_minutes: Optional[int] = Field(default=None, ge=0, le=1439)
    quiet_end_minutes: Optional[int] = Field(default=None, ge=0, le=1439)
    urgent_bypass: bool = True
    enabled: bool = True
    proactive: Optional[Dict[str, Dict[str, Any]]] = None


class PushTestRequest(BaseModel):
    title: str = Field(default="Jarivs push test", max_length=120)
    body: str = Field(default="If you can read this, the server→phone channel works.",
                      max_length=240)
    urgent: bool = False


class HotloadRequest(BaseModel):
    inject: bool = True


# ---------------------------------------------------------------------- utils
def _device_id(request: Request) -> str:
    device_id = str(getattr(request.state, "device_id", "") or "")
    if not device_id:
        raise HTTPException(status_code=401,
                            detail="This endpoint requires a paired device token.")
    return device_id


def _device_name(request: Request) -> str:
    profile = getattr(request.state, "device_scopes", None)
    return f"paired device {request.state.device_id}" if not profile else ""


# ---------------------------------------------------------------------- routes
@router.post("/push-token")
async def register_push_token(body: PushTokenRequest, request: Request) -> Dict[str, Any]:
    device_id = _device_id(request)
    registry = get_shared_registry()
    try:
        record = registry.register(device_id, body.expo_push_token,
                                    body.device_name or _device_name(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"registered": True, "record": record,
            "hint": "Jarivs can now reach this phone. Test with POST /api/mobile/test."}


@router.delete("/push-token")
async def unregister_push_token(request: Request) -> Dict[str, Any]:
    device_id = _device_id(request)
    removed = get_shared_registry().unregister(device_id)
    return {"device_id": device_id, "unregistered": removed}


@router.get("/me")
async def mobile_me(request: Request) -> Dict[str, Any]:
    device_id = _device_id(request)
    registry = get_shared_registry()
    record = registry.get(device_id)
    if record is None:
        return {"device_id": device_id, "push": None,
                "hint": "No push token registered yet for this device."}
    return {"device_id": device_id, "push": record}


@router.put("/settings")
async def mobile_settings(body: PushSettingsRequest, request: Request) -> Dict[str, Any]:
    device_id = _device_id(request)
    registry = get_shared_registry()
    record = registry.get(device_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail="Register a push token before configuring settings.")
    try:
        record = registry.set_quiet_hours(device_id, body.quiet_start_minutes,
                                          body.quiet_end_minutes, body.urgent_bypass)
        record = registry.set_enabled(device_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result: Dict[str, Any] = {"device_id": device_id, "record": record}
    if body.proactive is not None:
        result["proactive"] = get_dispatcher().update_config(body.proactive)
    return result


@router.post("/test")
async def mobile_test(body: PushTestRequest, request: Request) -> Dict[str, Any]:
    device_id = _device_id(request)
    sender = get_shared_sender()
    report = sender.notify(title=body.title, body=body.body,
                           data={"category": "test", "route": "chat"},
                           urgent=body.urgent, devices=[device_id])
    if not report.get("targeted") and not report.get("delivered"):
        raise HTTPException(status_code=404,
                            detail="No push token registered for this device.")
    return report


@router.get("/status")
async def mobile_status(request: Request) -> Dict[str, Any]:
    # Admin surface: device tokens carry a device_id; global key does not.
    if getattr(request.state, "device_id", None):
        raise HTTPException(status_code=403,
                            detail="This overview requires the global server key.")
    registry = get_shared_registry()
    dispatcher = get_dispatcher()
    sender = get_shared_sender()
    return {"registry": registry.stats(), "devices": registry.all_records(),
            "proactive": dispatcher.status(),
            "sender": {"endpoint": sender.endpoint, "dry_run": sender.dry_run,
                       "sent_recent": len(sender.sent)},
            "tools": MOBILE_TOOLS}


@router.post("/hotload")
async def mobile_hotload(body: HotloadRequest) -> Dict[str, Any]:
    """Re-import the mobile tools into the RUNNING server (no restart)."""
    results = []
    for module in ("notify_tools", "push_registry", "push_sender", "proactive"):
        try:
            importlib.import_module(f"openjarvis.mobile.{module}")
            results.append({"module": module, "ok": True})
        except Exception as exc:
            results.append({"module": module, "ok": False, "error": str(exc)[:300]})
    payload: Dict[str, Any] = {"modules": results}
    if body.inject:
        payload["injection"] = inject_mobile_tools_into_agents()
    return payload


# ------------------------------------------------------------- live injection
def inject_mobile_tools_into_agents() -> Dict[str, Any]:
    """Push the three tools into every live agent (idempotent, safe)."""
    injected: List[str] = []
    errors: List[str] = []
    agents = 0
    try:
        from openjarvis.creative.runtime import iter_agents

        for agent_id, agent in iter_agents():
            agents += 1
            for name in MOBILE_TOOLS:
                try:
                    # Canonical instantiation (matches agent_tool_sync / resolver).
                    tool = ToolRegistry.create(name)
                except Exception as exc:
                    errors.append(f"tool '{name}' not registered: {str(exc)[:120]}")
                    continue
                executor = getattr(agent, "_executor", None)
                executor_tools = getattr(executor, "_tools", None)
                if isinstance(executor_tools, dict) and name not in executor_tools:
                    executor_tools[name] = tool
                tools_list = getattr(agent, "_tools", None)
                if isinstance(tools_list, dict) and name not in tools_list:
                    tools_list[name] = tool
                elif isinstance(tools_list, list):
                    existing = {getattr(t, "name", None) or getattr(getattr(t, "spec", None), "name", None)
                                or getattr(t, "tool_id", None) for t in tools_list}
                    if name not in existing:
                        tools_list.append(tool)
                if name not in injected:
                    injected.append(name)
    except Exception as exc:
        errors.append(str(exc)[:300])
    return {"agents": agents, "tools": injected, "errors": errors,
            "hint": "Agents rebuild tool descriptions on their next turn."}


# ------------------------------------------------------------------- installer
_INSTALL_LOCK = threading.RLock()
_INSTALLED = {"installed": False}


def install_mobile_routes(app: Any) -> Dict[str, Any]:
    """Mount everything (idempotent — safe to call at startup or later)."""
    with _INSTALL_LOCK:
        summary: Dict[str, Any] = {"already_installed": _INSTALLED["installed"]}
        try:
            import openjarvis.mobile.notify_tools  # noqa: F401  (registers tools)
            import openjarvis.mobile.push_registry  # noqa: F401
            import openjarvis.mobile.push_sender  # noqa: F401
            import openjarvis.mobile.proactive  # noqa: F401
        except Exception as exc:
            logger.warning("mobile tool import failed: %s", exc)
            summary["import_error"] = str(exc)[:300]

        # Bind shared singletons to app.state so other routers/tests can see them.
        registry = get_shared_registry()
        sender = get_shared_sender()
        set_shared_registry(registry)
        set_shared_sender(sender)
        try:
            app.state.push_registry = registry
            app.state.push_sender = sender
            app.state.mobile_tools = MOBILE_TOOLS
        except Exception:
            pass

        mounted = False
        try:
            existing = getattr(app, "routes", [])
            if not any(getattr(route, "path", "") == "/api/mobile/push-token"
                       for route in existing):
                app.include_router(router)
            mounted = True
        except Exception as exc:
            logger.warning("mobile router mount failed: %s", exc)
            summary["router_error"] = str(exc)[:300]

        proactive: Dict[str, Any] = {}
        try:
            bus = getattr(app.state, "bus", None)
            if bus is None and hasattr(app.state, "event_bus"):
                bus = app.state.event_bus
            proactive = install_proactive_listeners(bus)
        except Exception as exc:
            proactive = {"installed": False, "error": str(exc)[:300]}

        injection = inject_mobile_tools_into_agents()
        _INSTALLED["installed"] = True
        summary.update({"installed": True, "router_mounted": mounted,
                        "proactive": proactive, "injection": injection,
                        "tools": MOBILE_TOOLS})
        logger.info("mobile companion installed: %s", summary)
        return summary


__all__ = ["router", "install_mobile_routes", "inject_mobile_tools_into_agents",
           "MOBILE_TOOLS"]

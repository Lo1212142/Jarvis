"""FastAPI routes for the creative suite: settings, keys, gallery, jobs.

Additive-only integration — one router mounted by a single call in
``server/app.py`` (``install_creative_routes(app)``), touching no
existing route modules.
"""

from __future__ import annotations

import importlib
import logging
import mimetypes
import os
import re as _re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from openjarvis.creative import _paths, media_settings
from openjarvis.creative import runtime, self_heal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/creative", tags=["creative"])
media_router = APIRouter(tags=["creative-media"])

# All creative-suite tools (auto-injected into live agents).
CREATIVE_TOOLS = [
    "media_image_generate", "image_edit", "video_edit",
    "media_video_generate", "demo_video", "tech_news", "tutor",
    "remember_preference", "self_dev_build",
    "osint_image", "osint_map", "osint_satellite", "osint_shodan",
]

# Tool modules inside the creative package (used for import + hotload).
_TOOL_MODULES = [
    "image_tools", "video_tools", "demo_video_tool", "news_tool",
    "tutor_tool", "preferences_tool", "self_dev",
    "geoint_tools", "geoint_map_tools", "geoint_satellite",
    "shodan_tools",
]

# Modules in the package that are NOT tools (never auto-discovered).
_NON_TOOL_MODULES = {
    "__init__", "_paths", "_sun_calc", "runtime", "media_settings",
    "text_render", "ffmpeg_engine", "providers", "self_heal",
    "server_watchdog", "guardian", "creative_routes",
}


def discover_creative_tool_modules() -> List[str]:
    """Module names in openjarvis.creative that look like tool modules.

    Everything on disk ending in ``_tools.py`` / ``*_tool.py`` / known
    tool modules, excluding infrastructure — so FUTURE drop-in tool
    files are picked up by the hotload endpoint automatically.
    """
    try:
        import openjarvis.creative as pkg

        root = Path(pkg.__file__).resolve().parent
    except Exception:
        return list(_TOOL_MODULES)
    found = set(_TOOL_MODULES)
    for path in root.glob("*.py"):
        name = path.stem
        if name in _NON_TOOL_MODULES or name.startswith("_"):
            continue
        if name.endswith("_tools") or name.endswith("_tool"):
            found.add(name)
    return sorted(found)

# ---------------------------------------------------------------------------
# Jobs (async renders for the frontend; the agent uses the sync tool path)
# ---------------------------------------------------------------------------

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.RLock()


class TimelineRenderRequest(BaseModel):
    timeline: Dict[str, Any] = Field(..., description="Studio timeline DSL project")
    name: Optional[str] = Field(default=None, max_length=64)


def _run_render_job(job_id: str, timeline: Dict[str, Any]) -> None:
    from openjarvis.creative.ffmpeg_engine import render_timeline

    with _JOBS_LOCK:
        _JOBS[job_id].update(status="running", started_at=time.time())
    try:
        result = render_timeline(timeline)
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="done", finished_at=time.time(), result=result
            )
    except Exception as exc:
        logger.warning("render job %s failed: %s", job_id, exc)
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="failed", finished_at=time.time(),
                error=str(exc)[:500],
            )


@router.post("/render")
async def create_render(req: TimelineRenderRequest) -> Dict[str, Any]:
    timeline = dict(req.timeline or {})
    if req.name:
        timeline.setdefault("name", req.name)
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"id": job_id, "status": "queued",
                         "created_at": time.time(), "result": None, "error": None}
    threading.Thread(
        target=_run_render_job, args=(job_id, timeline), daemon=True
    ).start()
    return {"job_id": job_id, "status": "queued",
            "poll": f"/api/creative/jobs/{job_id}"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"unknown job {job_id}")
    return job


# ---------------------------------------------------------------------------
# Settings + keys
# ---------------------------------------------------------------------------


class SettingsPatch(BaseModel):
    patch: Dict[str, Any] = Field(..., description="Partial settings to merge")


class ApiKeyBody(BaseModel):
    api_key: str = Field(..., max_length=4096, description="Provider API key")


@router.get("/settings")
async def get_settings() -> Dict[str, Any]:
    return {
        "settings": media_settings.load_settings(),
        "key_status": media_settings.key_status(),
    }


@router.patch("/settings/{section}")
async def patch_settings(section: str, body: SettingsPatch) -> Dict[str, Any]:
    try:
        media_settings.save_settings_section(section, body.patch)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"settings": media_settings.load_settings(),
            "key_status": media_settings.key_status()}


@router.post("/settings/{section}/reset")
async def reset_settings(section: str) -> Dict[str, Any]:
    try:
        media_settings.reset_settings_section(section)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"settings": media_settings.load_settings(),
            "key_status": media_settings.key_status()}


@router.post("/keys/{provider}")
async def save_key(provider: str, body: ApiKeyBody) -> Dict[str, Any]:
    try:
        media_settings.save_api_key(provider, body.api_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"provider": provider, "saved": True,
            "key_status": media_settings.key_status()}


# ---------------------------------------------------------------------------
# Gallery / tools / health
# ---------------------------------------------------------------------------

_MEDIA_EXTS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "video": {".mp4", ".webm", ".gif", ".mov", ".mkv"},
    "audio": {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac"},
}


@router.get("/gallery")
async def gallery(kind: str = "all", limit: int = 60) -> Dict[str, Any]:
    root = _paths.creative_root()
    items: List[Dict[str, Any]] = []
    for category, exts in _MEDIA_EXTS.items():
        if kind not in ("all", category):
            continue
        directory = root / category if (root / category).exists() else None
        # images live in images/, videos in videos/, audio in audio/
        directory = {"image": root / "images", "video": root / "videos",
                     "audio": root / "audio"}.get(category, directory)
        if not directory or not directory.exists():
            continue
        for path in directory.iterdir():
            if path.suffix.lower() not in exts or not path.is_file():
                continue
            stat = path.stat()
            items.append({
                "kind": category,
                "name": path.name,
                "path": str(path),
                "url": f"/media/creative/{category}/{path.name}",
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
    items.sort(key=lambda item: item["modified"], reverse=True)
    return {"items": items[: max(1, min(limit, 200))]}


@router.get("/tools")
async def creative_tools() -> Dict[str, Any]:
    from openjarvis.core.registry import ToolRegistry

    tools = []
    for name in CREATIVE_TOOLS:
        tools.append({"name": name,
                      "registered": ToolRegistry.contains(name)})
    try:
        from openjarvis.creative import self_dev

        for entry in self_dev.list_self_dev_tools():
            tools.append({"name": entry["tool"], "registered": True,
                          "self_dev": True, "module": entry["module"]})
    except Exception:
        pass
    return {"tools": tools}


@router.get("/health")
async def health() -> Dict[str, Any]:
    watcher = self_heal.current_watcher()
    payload: Dict[str, Any] = {
        "watcher_running": watcher is not None and watcher._thread is not None,
    }
    if watcher is not None:
        payload["repairs"] = dict(watcher.repairs)
        payload["checks"] = watcher.check_all()
    else:
        # One-off checks without the background watcher.
        from openjarvis.creative.self_heal import SelfHealWatcher

        payload["checks"] = SelfHealWatcher().check_all()
    agents = runtime.running_agents()
    payload["agents_tracked"] = list(agents.keys())

    # GEOINT services status (exiftool backend / data endpoints).
    try:
        from openjarvis.creative import geoint_tools

        payload["geoint"] = {
            "exiftool_backend": geoint_tools.exiftool_backend(),
            "overpass_endpoints": len(
                media_settings.load_settings().get("geoint", {})
                .get("overpass_endpoints", ["main"])),
        }
    except Exception as exc:
        payload["geoint"] = {"error": str(exc)[:200]}

    # Shodan status (key-less InternetDB by default; key upgrades data).
    try:
        from openjarvis.creative import shodan_tools

        payload["shodan"] = {
            "api_key_configured": bool(shodan_tools.resolve_shodan_key()),
            "internetdb_base": shodan_tools._shodan_settings()
                .get("internetdb_base"),
        }
    except Exception as exc:
        payload["shodan"] = {"error": str(exc)[:200]}

    # Whole-process self-recovery status (external guardian).
    try:
        from openjarvis.creative import guardian as guardian_mod

        cfg = guardian_mod.load_config()
        state = guardian_mod._read_json(guardian_mod._state_path())
        hb = guardian_mod._read_json(guardian_mod._heartbeat_path())
        guardian_status: Dict[str, Any] = {
            "command": cfg.get("command"),
            "supervised": state is not None,
            "restarts": (state or {}).get("restarts"),
            "crashes": (state or {}).get("crashes"),
            "hang_kills": (state or {}).get("hang_kills"),
            "boot_failures": (state or {}).get("boot_failures"),
            "recovery_runs": (state or {}).get("recovery_runs"),
            "circuit_open": (state or {}).get("circuit_open", False),
            "last_classification": (state or {}).get("last_classification"),
        }
        if hb and "ts" in hb:
            guardian_status["heartbeat_age_s"] = round(time.time() - float(hb["ts"]), 1)
            guardian_status["server_pid"] = hb.get("pid")
        payload["guardian"] = guardian_status
    except Exception as exc:
        payload["guardian"] = {"error": str(exc)[:200]}
    return payload


# ---------------------------------------------------------------------------
# Media serving (Range-supported for <video> seeking)
# ---------------------------------------------------------------------------

_CHUNK = 1024 * 1024


@media_router.get("/media/creative/{category}/{name}")
async def serve_media(category: str, name: str, request: Request) -> Any:
    if category not in ("images", "videos", "audio", "thumbs", "projects", "tmp"):
        raise HTTPException(404, "unknown media category")
    # Block traversal.
    if "/" in name or ".." in name or "\\" in name:
        raise HTTPException(400, "invalid media name")
    path = _paths.creative_root() / category / name
    if not path.is_file():
        raise HTTPException(404, "media not found")

    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})

    try:
        unit, _, spec = range_header.partition("=")
        start_s, _, end_s = spec.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except ValueError:
        raise HTTPException(400, "invalid range header")
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def _stream() -> Any:
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _stream(), status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


def _inject_tools_into_agents(names: List[str]) -> Dict[str, Any]:
    """Register + inject tools into every tracked live agent.

    The agent rebuilds its tool descriptions from ``agent._tools`` every
    turn, so injection takes effect on the very next message.
    """
    from openjarvis.core.registry import ToolRegistry

    injected: List[str] = []
    agents = runtime.running_agents()
    for name in names:
        if not ToolRegistry.contains(name):
            continue
        try:
            tool = ToolRegistry.create(name)
            for agent in agents.values():
                executor = getattr(agent, "_executor", None)
                if executor is not None and hasattr(executor, "_tools"):
                    executor._tools[name] = tool
                tools_list = getattr(agent, "_tools", None)
                if isinstance(tools_list, list):
                    tools_list[:] = [t for t in tools_list
                                     if getattr(t, "spec", None) is None
                                     or t.spec.name != name]
                    tools_list.append(tool)
            injected.append(name)
        except Exception as exc:
            logger.debug("inject %s failed: %s", name, exc)
    return {"injected": injected, "agents": list(agents.keys())}


_MODULE_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class HotloadBody(BaseModel):
    modules: Optional[List[str]] = None
    inject: bool = True


@router.post("/hotload")
async def hotload(body: HotloadBody) -> Dict[str, Any]:
    """Import creative tool modules into the RUNNING server (no restart).

    Drop new ``*_tools.py`` files into ``openjarvis/creative/`` (or update
    an existing module on disk), then POST {} — every listed module is
    imported, every registered creative tool is injected into the live
    agents and is callable from the very next chat message. Modules are
    restricted to the ``openjarvis.creative`` namespace on disk.
    """
    try:
        return hotload_modules(body.modules, inject=body.inject)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("hotload failed: %s", exc)
        raise HTTPException(500, f"hotload failed: {exc}") from exc


def hotload_modules(modules: Optional[List[str]] = None,
                    inject: bool = True) -> Dict[str, Any]:
    """Implementation behind POST /api/creative/hotload (idempotent)."""
    import openjarvis.creative as pkg

    root = Path(pkg.__file__).resolve().parent
    requested = modules or discover_creative_tool_modules()
    results: List[Dict[str, Any]] = []
    ok_modules: List[str] = []
    for name in requested:
        if not _MODULE_NAME_RE.match(name or ""):
            raise HTTPException(400, f"invalid module name '{name}'")
        if not (root / f"{name}.py").is_file():
            raise HTTPException(404, f"module '{name}' not found in the "
                                     f"creative package (allowed: files "
                                     f"inside openjarvis/creative only)")
        try:
            importlib.import_module(f"openjarvis.creative.{name}")
            ok_modules.append(name)
            results.append({"module": name, "ok": True})
        except HTTPException:
            raise
        except Exception as exc:
            results.append({"module": name, "ok": False,
                            "error": str(exc)[:300]})
    payload: Dict[str, Any] = {
        "modules": results,
        "imported": ok_modules,
        "discovered": discover_creative_tool_modules(),
    }
    if inject:
        payload.update(_inject_tools_into_agents(CREATIVE_TOOLS))
    return payload


# ---------------------------------------------------------------------------
# Install hook
# ---------------------------------------------------------------------------


def install_creative_routes(app: Any) -> Dict[str, Any]:
    """Mount the creative routers + start listeners (idempotent)."""
    mounted = False
    try:
        # Import tool modules so registration runs even if tools/__init__
        # hasn't been extended yet.
        import openjarvis.creative.image_tools  # noqa: F401
        import openjarvis.creative.video_tools  # noqa: F401
        import openjarvis.creative.demo_video_tool  # noqa: F401
        import openjarvis.creative.news_tool  # noqa: F401
        import openjarvis.creative.tutor_tool  # noqa: F401
        import openjarvis.creative.preferences_tool  # noqa: F401
        import openjarvis.creative.self_dev  # noqa: F401
        import openjarvis.creative.geoint_tools  # noqa: F401
        import openjarvis.creative.geoint_map_tools  # noqa: F401
        import openjarvis.creative.geoint_satellite  # noqa: F401
        import openjarvis.creative.shodan_tools  # noqa: F401
    except Exception as exc:
        logger.warning("creative tool import failed: %s", exc)

    app.include_router(router)
    app.include_router(media_router)
    mounted = True

    installed: Dict[str, Any] = {"router": mounted}

    # Track the primary agent for live tool injection.
    agent = getattr(getattr(app, "state", None), "agent", None)
    if agent is not None:
        agent_name = getattr(getattr(app, "state", None), "agent_name", "") or "primary"
        runtime.register_agent(agent_name, agent)
        # Inject creative tools into the live agent right now.
        installed.update(_inject_tools_into_agents(CREATIVE_TOOLS))

    # Auto preference capture from chat.
    bus = getattr(getattr(app, "state", None), "bus", None)
    if bus is not None:
        from openjarvis.creative.preferences_tool import install_preference_listener

        installed["preference_listener"] = install_preference_listener(bus)

    # Self-healing watcher.
    try:
        watcher = self_heal.install_self_heal(app)
        installed["self_heal"] = watcher is not None
    except Exception as exc:
        logger.warning("self-heal install skipped: %s", exc)

    # Server watchdog: heartbeat for the external guardian, crash
    # forensics, boot preflight and self-dev tool re-registration.
    try:
        from openjarvis.creative import server_watchdog

        installed["server_watchdog"] = server_watchdog.install_server_watchdog(app)
    except Exception as exc:
        logger.warning("server watchdog install skipped: %s", exc)

    logger.info("creative routes installed: %s", installed)
    return installed


__all__ = ["router", "media_router", "install_creative_routes",
           "hotload_modules", "discover_creative_tool_modules",
           "CREATIVE_TOOLS"]

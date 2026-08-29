"""Authenticated browser-computer API.

The API controls a server-side browser session; it never exposes browser
credentials or accepts arbitrary JavaScript. High-impact actions are gated by
a one-use approval token and CAPTCHA always requires manual takeover.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .computer import BrowserComputerError, BrowserComputerSession, PlaywrightRuntime


class BrowserSessionCreate(BaseModel):
    headless: bool = True


class BrowserNavigateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class BrowserActionRequest(BaseModel):
    action: str
    selector: str = Field(default="", max_length=1000)
    value: str = Field(default="", max_length=10000)
    key: str = Field(default="", max_length=32)
    approval_id: str = Field(default="", max_length=128)
    x: float | None = Field(default=None, ge=0, le=10000)
    y: float | None = Field(default=None, ge=0, le=10000)
    button: str = Field(default="left", max_length=8)


class BrowserApprovalRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)


class BrowserManualInputRequest(BaseModel):
    action: str
    key: str = Field(default="", max_length=32)
    x: float | None = Field(default=None, ge=0, le=10000)
    y: float | None = Field(default=None, ge=0, le=10000)
    button: str = Field(default="left", max_length=8)


class BrowserComputerManager:
    def __init__(self, runtime_factory: Callable[[str, bool], Any] | None = None, *, max_sessions: int = 4) -> None:
        self._custom_runtime_factory = runtime_factory
        self.max_sessions = max(1, min(int(max_sessions), 16))
        self._runtime_factory = runtime_factory or (lambda session_id, headless: PlaywrightRuntime(user_data_dir=f"/tmp/openjarvis-browser-{session_id}", headless=headless))
        self._sessions: dict[str, BrowserComputerSession] = {}
        self._lock = threading.RLock()

    def create(self, *, headless: bool) -> BrowserComputerSession:
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise BrowserComputerError("maximum browser session limit reached")
        session = BrowserComputerSession(runtime=self._runtime_factory("pending", headless), headless=headless)
        # Recreate the runtime with the final random session ID only for the default factory.
        if self._custom_runtime_factory is None:
            session.runtime = PlaywrightRuntime(user_data_dir=f"/tmp/openjarvis-browser-{session.session_id}", headless=headless)
        with self._lock:
            self._sessions[session.session_id] = session
        try:
            session.start()
        except Exception:
            with self._lock:
                self._sessions.pop(session.session_id, None)
            raise
        return session

    def get(self, session_id: str) -> BrowserComputerSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()


router = APIRouter(prefix="/api/browser/computers", tags=["browser-computer"])


def _manager(request: Request) -> BrowserComputerManager:
    manager = getattr(request.app.state, "browser_computer_manager", None)
    if manager is None:
        manager = BrowserComputerManager()
        request.app.state.browser_computer_manager = manager
    return manager


def _session_or_404(request: Request, session_id: str) -> BrowserComputerSession:
    try:
        return _manager(request).get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="browser session not found") from exc


def _result(result: Any) -> dict[str, Any]:
    return {
        "action": result.action,
        "success": result.success,
        "approval_required": result.approval_required,
        "approval_id": result.approval_id,
        "captcha_detected": result.captcha_detected,
        "data": result.data,
        "error": result.error,
    }


@router.post("/sessions")
async def create_session(payload: BrowserSessionCreate, request: Request) -> dict[str, Any]:
    try:
        session = await asyncio.to_thread(_manager(request).create, headless=payload.headless)
        return {"session": session.snapshot(), "security": {"isolated_profile": True, "captcha_bypass": False, "approval_required_for_side_effects": True}}
    except BrowserComputerError as exc:
        status = 429 if "maximum browser session" in str(exc) else 501
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"browser startup failed: {type(exc).__name__}") from exc


@router.get("/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    manager = _manager(request)
    with manager._lock:
        sessions = list(manager._sessions.values())
    return {"sessions": [session.snapshot() for session in sessions]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return {"session": _session_or_404(request, session_id).snapshot()}


@router.post("/sessions/{session_id}/navigate")
async def navigate(session_id: str, payload: BrowserNavigateRequest, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    result = await asyncio.to_thread(session.navigate, payload.url)
    return {"result": _result(result), "session": session.snapshot()}


@router.post("/sessions/{session_id}/actions")
async def action(session_id: str, payload: BrowserActionRequest, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    result = await asyncio.to_thread(session.act, payload.action, selector=payload.selector, value=payload.value, key=payload.key, approval_id=payload.approval_id, x=payload.x, y=payload.y, button=payload.button)
    return {"result": _result(result), "session": session.snapshot()}


@router.post("/sessions/{session_id}/manual-input")
async def manual_input(session_id: str, payload: BrowserManualInputRequest, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    result = await asyncio.to_thread(session.manual_act, payload.action, key=payload.key, x=payload.x, y=payload.y, button=payload.button)
    return {"result": _result(result), "session": session.snapshot()}


@router.post("/sessions/{session_id}/approve")
async def approve(session_id: str, payload: BrowserApprovalRequest, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    approved = await asyncio.to_thread(session.approve, payload.approval_id)
    return {"approved": approved, "session": session.snapshot()}


@router.post("/sessions/{session_id}/captcha/resume")
async def resume_captcha(session_id: str, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    resumed = await asyncio.to_thread(session.resume_after_captcha)
    return {"resumed": resumed, "session": session.snapshot()}


@router.get("/sessions/{session_id}/screenshot")
async def screenshot(session_id: str, request: Request) -> Response:
    session = _session_or_404(request, session_id)
    try:
        payload = await asyncio.to_thread(session.screenshot)
        return Response(content=base64.b64decode(payload["data_base64"]), media_type="image/png", headers={"X-Browser-Session": session_id, "X-Browser-Url": payload["url"]})
    except BrowserComputerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, request: Request, limit: int = 100) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    return {"events": [{"event_type": event.event_type, "payload": event.payload, "created_at": event.created_at} for event in session.events(limit)]}


@router.post("/sessions/{session_id}/pause")
async def pause(session_id: str, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    await asyncio.to_thread(session.pause)
    return {"session": session.snapshot()}


@router.post("/sessions/{session_id}/stop")
async def stop(session_id: str, request: Request) -> dict[str, Any]:
    session = _session_or_404(request, session_id)
    await asyncio.to_thread(session.stop)
    return {"session": session.snapshot()}


def install_browser_computer(app: Any) -> None:
    manager = BrowserComputerManager()
    app.state.browser_computer_manager = manager

    @app.on_event("shutdown")
    async def _shutdown_browser_computer() -> None:
        manager.stop_all()


__all__ = ["BrowserComputerManager", "install_browser_computer", "router"]

"""Authenticated remote audio playback API.

The API coordinates commands; the Windows client is responsible for decoding
and sending audio to its selected output device.  A command is not considered
successful until the client acknowledges it.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from openjarvis.audio.playback import AudioPlaybackService


class RegisterClientRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)


class RegisterTrackRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str = Field(default="", max_length=512)


class PlayRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    track_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)


class ControlRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=32)
    value: int | None = Field(default=None, ge=0, le=100)


class AcknowledgeRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    state: str = Field(default="playing", max_length=32)
    error: str = Field(default="", max_length=500)


router = APIRouter(prefix="/api/audio", tags=["audio"])


def get_service(request: Request) -> AudioPlaybackService:
    service = getattr(request.app.state, "audio_service", None)
    if not isinstance(service, AudioPlaybackService):
        service = AudioPlaybackService()
        request.app.state.audio_service = service
    return service


def _require_enabled(request: Request) -> None:
    if not bool(getattr(request.app.state, "audio_playback_enabled", False)):
        raise HTTPException(status_code=409, detail="remote audio playback is disabled in settings")


def _bad_request(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/clients")
async def register_client(body: RegisterClientRequest, request: Request) -> dict[str, Any]:
    _require_enabled(request)
    return get_service(request).register_client(body.client_id)


@router.delete("/clients/{client_id}")
async def unregister_client(client_id: str, request: Request) -> dict[str, bool]:
    return {"removed": get_service(request).unregister_client(client_id)}


@router.post("/tracks")
async def register_track(body: RegisterTrackRequest, request: Request) -> dict[str, Any]:
    _require_enabled(request)
    try:
        return get_service(request).register_track(body.path, title=body.title)
    except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
        raise _bad_request(exc) from exc


@router.get("/state")
async def playback_state(request: Request, client_id: str | None = None) -> dict[str, Any]:
    try:
        return {"states": get_service(request).state(client_id)} if client_id is None else {"state": get_service(request).state(client_id)}
    except (KeyError, ValueError) as exc:
        raise _bad_request(exc) from exc


@router.post("/play")
async def play(body: PlayRequest, request: Request) -> dict[str, Any]:
    _require_enabled(request)
    try:
        return get_service(request).play(body.client_id, body.track_id, title=body.title)
    except (FileNotFoundError, PermissionError, ValueError, KeyError) as exc:
        raise _bad_request(exc) from exc


@router.post("/control")
async def control(body: ControlRequest, request: Request) -> dict[str, Any]:
    _require_enabled(request)
    try:
        return get_service(request).control(body.client_id, body.action, value=body.value)
    except (KeyError, ValueError) as exc:
        raise _bad_request(exc) from exc


@router.post("/ack")
async def acknowledge(body: AcknowledgeRequest, request: Request) -> dict[str, Any]:
    try:
        return {"state": get_service(request).acknowledge(body.client_id, body.sequence, state=body.state, error=body.error)}
    except (KeyError, ValueError) as exc:
        raise _bad_request(exc) from exc


@router.get("/stream/{token}")
async def stream(token: str, request: Request) -> FileResponse:
    _require_enabled(request)
    try:
        path, content_type = get_service(request).resolve_grant(token)
    except (KeyError, FileNotFoundError, PermissionError) as exc:
        raise _bad_request(exc) from exc
    # FileResponse lets the client use HTTP range requests.  No server-side
    # transcoding occurs, so the original audio bytes reach the client.
    return FileResponse(
        Path(path),
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Jarvis-Audio-Quality": "source-bytes"},
    )


@router.websocket("/ws")
async def audio_websocket(websocket: WebSocket) -> None:
    """Push playback commands and receive client acknowledgements."""
    from openjarvis.server.auth_middleware import authenticate_websocket

    expected_key = getattr(websocket.app.state, "api_key", "")
    authorized, subprotocol = authenticate_websocket(websocket, expected_key)
    if not authorized:
        await websocket.close(code=1008)
        return
    if not bool(getattr(websocket.app.state, "audio_playback_enabled", False)):
        await websocket.close(code=1008)
        return
    client_id = websocket.query_params.get("client_id", "").strip()
    if not client_id:
        await websocket.close(code=1008)
        return
    await websocket.accept(subprotocol=subprotocol)
    import asyncio

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)

    def sender(command: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, command)
        except (RuntimeError, asyncio.QueueFull):
            pass

    service = get_service(websocket)  # type: ignore[arg-type]
    service.register_client(client_id, sender)
    await websocket.send_json({"type": "audio.ready", "protocol": 1, "client_id": client_id})
    receive_task: asyncio.Task | None = asyncio.create_task(websocket.receive_json())
    send_task: asyncio.Task | None = asyncio.create_task(queue.get())
    try:
        while True:
            done, _ = await asyncio.wait({receive_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
            if receive_task in done:
                message = receive_task.result()
                message_type = str(message.get("type", ""))
                if message_type == "audio.ack":
                    try:
                        service.acknowledge(client_id, int(message.get("sequence", 0)), state=str(message.get("state", "playing")), error=str(message.get("error", "")))
                    except (KeyError, ValueError):
                        await websocket.send_json({"type": "audio.error", "error": "invalid acknowledgement"})
                elif message_type == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack", "timestamp": time.time()})
                receive_task = asyncio.create_task(websocket.receive_json())
            if send_task in done:
                await websocket.send_json(send_task.result())
                send_task = asyncio.create_task(queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in (receive_task, send_task):
            if task is not None:
                task.cancel()
        service.unregister_client(client_id)


__all__ = ["router"]

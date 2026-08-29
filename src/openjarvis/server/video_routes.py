"""Authenticated, bounded video analysis endpoints."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from openjarvis.media.video_index import extract_thumbnail, probe_video, search_transcript

router = APIRouter(prefix="/api/media/video", tags=["video"])
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class TranscriptSearchRequest(BaseModel):
    transcript: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)
    query: str = Field(min_length=1, max_length=512)


def _workspace(request: Request) -> Path:
    root = Path(os.environ.get("OPENJARVIS_MEDIA_DIR", tempfile.gettempdir())) / "openjarvis-media"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    size = 0
    with destination.open("wb") as output:
        while True:
            chunk = await upload.read(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="video upload exceeds 512MB endpoint limit")
            output.write(chunk)
    return size


@router.post("/probe")
async def probe(request: Request, upload: UploadFile = File(...)) -> dict[str, Any]:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="video filename is required")
    suffix = Path(upload.filename).suffix.lower()[:12] or ".bin"
    target = _workspace(request) / f"upload-{os.urandom(12).hex()}{suffix}"
    try:
        size = await _save_upload(upload, target)
        metadata = probe_video(target)
        metadata["uploaded_bytes"] = size
        return metadata
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        target.unlink(missing_ok=True)


@router.post("/search-transcript")
async def transcript_search(payload: TranscriptSearchRequest) -> dict[str, Any]:
    hits = search_transcript(payload.transcript, payload.query)
    return {"query": payload.query, "hits": [{"timestamp_seconds": hit.timestamp_seconds, "text": hit.text} for hit in hits]}


@router.post("/thumbnail")
async def thumbnail(request: Request, upload: UploadFile = File(...), timestamp_seconds: float = Form(0.0)) -> Response:
    if timestamp_seconds < 0 or timestamp_seconds > 86_400:
        raise HTTPException(status_code=422, detail="timestamp_seconds must be between 0 and 86400")
    suffix = Path(upload.filename or "video.bin").suffix.lower()[:12] or ".bin"
    target = _workspace(request) / f"upload-{os.urandom(12).hex()}{suffix}"
    output = target.with_suffix(".jpg")
    try:
        await _save_upload(upload, target)
        extract_thumbnail(target, timestamp_seconds, output)
        content = output.read_bytes()
        return Response(content=content, media_type="image/jpeg", headers={"Content-Disposition": "inline; filename=thumbnail.jpg"})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        target.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def install_video_routes(app: Any) -> None:
    app.include_router(router)


__all__ = ["install_video_routes", "router"]

"""User-initiated, transient mobile camera vision endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from openjarvis.vision import NIMVisionService, VisionUnavailable

router = APIRouter(prefix="/api/vision", tags=["vision"])
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _service(request: Request) -> NIMVisionService:
    service = getattr(request.app.state, "vision_service", None)
    if not isinstance(service, NIMVisionService):
        service = NIMVisionService(request.app.state.engine)
        request.app.state.vision_service = service
    return service


@router.get("/status")
async def vision_status(request: Request) -> dict[str, Any]:
    return _service(request).status()


@router.post("/analyze")
async def analyze_camera_frame(
    request: Request,
    frame: UploadFile = File(...),
    question: str = Form(default="What is visible in this image?", max_length=800),
) -> dict[str, Any]:
    if frame.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="camera frame must be JPEG, PNG, or WebP")
    raw = await frame.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="camera frame exceeds 4MB upload limit")
    try:
        return _service(request).analyze(
            image_bytes=raw,
            content_type=frame.content_type,
            question=question.strip() or "What is visible in this image?",
        )
    except VisionUnavailable as exc:
        status = _service(request).status()
        code = 501 if not _service(request).enabled else 503
        raise HTTPException(status_code=code, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc
    finally:
        await frame.close()


__all__ = ["router"]

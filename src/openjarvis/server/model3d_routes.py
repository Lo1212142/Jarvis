"""Authenticated CPU-only 3D preview endpoints."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from openjarvis.media.model3d import inspect_model, render_preview

router = APIRouter(prefix="/api/media/3d", tags=["3d"])
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


def _workspace(request: Request) -> Path:
    root = Path(os.environ.get("OPENJARVIS_MEDIA_DIR", tempfile.gettempdir())) / "openjarvis-3d"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    size = 0
    with destination.open("wb") as output:
        while True:
            chunk = await upload.read(_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="3D upload exceeds 100MB limit")
            output.write(chunk)


@router.post("/inspect")
async def inspect(request: Request, upload: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(upload.filename or "model.bin").suffix.lower()
    target = _workspace(request) / f"model-{os.urandom(12).hex()}{suffix}"
    try:
        await _save_upload(upload, target)
        stats = inspect_model(target)
        return {
            "filename": stats.filename,
            "format": stats.format,
            "vertices": stats.vertices,
            "faces": stats.faces,
            "bounds_min": stats.bounds_min,
            "bounds_max": stats.bounds_max,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        target.unlink(missing_ok=True)


@router.post("/preview")
async def preview(request: Request, upload: UploadFile = File(...)) -> Response:
    suffix = Path(upload.filename or "model.bin").suffix.lower()
    target = _workspace(request) / f"model-{os.urandom(12).hex()}{suffix}"
    output = target.with_suffix(".png")
    try:
        await _save_upload(upload, target)
        render_preview(target, output)
        content = output.read_bytes()
        return Response(content=content, media_type="image/png", headers={"Content-Disposition": "inline; filename=model-preview.png"})
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        target.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


__all__ = ["router"]

"""Bounded image OCR endpoint; OCR dependencies remain optional."""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter(prefix="/api/media/image", tags=["image"])
_MAX_BYTES = 25 * 1024 * 1024


@router.post("/ocr")
async def ocr(upload: UploadFile = File(...)) -> dict[str, Any]:
    raw = await upload.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="image exceeds 25MB limit")
    try:
        from PIL import Image
        import pytesseract
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            text = pytesseract.image_to_string(image)[:2_000_000]
        return {"text": text, "characters": len(text), "engine": "pytesseract"}
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="OCR dependencies are not installed") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"image OCR failed: {type(exc).__name__}") from exc


__all__ = ["router"]

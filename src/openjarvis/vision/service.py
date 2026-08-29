"""Bounded, user-initiated NIM vision service for mobile camera sessions."""

from __future__ import annotations

import base64
import io
import os
from datetime import datetime, timezone
from typing import Any


class VisionUnavailable(RuntimeError):
    """Raised when vision is disabled or no compatible NIM model is configured."""


class NIMVisionService:
    """Analyze one transient camera frame without persisting source bytes."""

    def __init__(self, engine: Any, *, enabled: bool = False, model: str = "") -> None:
        self._engine = engine
        self.enabled = bool(enabled)
        self.model = (model or os.getenv("NIM_VISION_MODEL", "")).strip()

    def configure(self, *, enabled: bool | None = None, model: str | None = None) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if model is not None:
            self.model = model.strip()

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"available": False, "reason": "camera vision is disabled in server settings", "source": "nim"}
        if not self.model:
            return {"available": False, "reason": "NIM_VISION_MODEL is not configured", "source": "nim"}
        if not callable(getattr(self._nim_engine(), "generate_vision", None)):
            return {"available": False, "reason": "configured engine does not expose NIM vision", "source": "nim"}
        return {"available": True, "model": self.model, "source": "nim", "image_retained": False}

    def analyze(self, *, image_bytes: bytes, content_type: str, question: str) -> dict[str, Any]:
        capability = self.status()
        if not capability["available"]:
            raise VisionUnavailable(str(capability["reason"]))
        encoded_image, normalized_type = self._normalize_frame(image_bytes, content_type)
        engine = self._nim_engine()
        try:
            result = engine.generate_vision(
                model=self.model,
                image_data_url=f"data:{normalized_type};base64,{base64.b64encode(encoded_image).decode('ascii')}",
                question=question,
            )
        except Exception as exc:
            raise VisionUnavailable(f"NIM vision request failed: {type(exc).__name__}") from exc
        answer = str(result.get("content") or "").strip()
        if not answer:
            raise VisionUnavailable("NIM vision returned no observable answer")
        return {
            "answer": answer,
            "model": str(result.get("model") or self.model),
            "source": "nim",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "image_retained": False,
        }

    def _nim_engine(self) -> Any:
        candidate = self._engine
        for _ in range(6):
            if getattr(candidate, "engine_id", "") == "nim":
                return candidate
            inner = getattr(candidate, "_inner", None)
            if inner is None or inner is candidate:
                break
            candidate = inner
        return None

    @staticmethod
    def _normalize_frame(image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
        try:
            from PIL import Image, ImageOps
        except ImportError as exc:  # pragma: no cover - runtime optional dependency
            raise VisionUnavailable("image processing support is not installed") from exc
        try:
            with Image.open(io.BytesIO(image_bytes)) as frame:
                frame.verify()
            with Image.open(io.BytesIO(image_bytes)) as frame:
                frame = ImageOps.exif_transpose(frame).convert("RGB")
                frame.thumbnail((1600, 1600))
                output = io.BytesIO()
                frame.save(output, format="JPEG", quality=82, optimize=True)
                normalized = output.getvalue()
        except Exception as exc:
            raise VisionUnavailable(f"camera frame is invalid: {type(exc).__name__}") from exc
        if not normalized or len(normalized) > 2 * 1024 * 1024:
            raise VisionUnavailable("normalized camera frame exceeds the 2MB limit")
        return normalized, "image/jpeg"


__all__ = ["NIMVisionService", "VisionUnavailable"]

"""Tool for bounded video metadata, thumbnails, and transcript search."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.media.video_index import extract_thumbnail, probe_video, search_transcript
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("video_analyze")
class VideoAnalyzeTool(BaseTool):
    tool_id = "video_analyze"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_analyze",
            description=(
                "Inspect an authorized video with CPU-only metadata probing, extract a "
                "thumbnail, and search a timestamped transcript. Speech transcription "
                "is supplied by the configured STT provider and is not assumed here."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Authorized local video path."},
                    "query": {"type": "string", "description": "Optional transcript phrase to find."},
                    "transcript": {"type": "array", "description": "Optional timestamped segments.", "items": {"type": "object"}},
                    "thumbnail_at": {"type": "number", "minimum": 0},
                },
                "required": ["path"],
            },
            category="vision",
            requires_confirmation=True,
            timeout_seconds=120.0,
            required_capabilities=["vision:video-analysis"],
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            path = str(params.get("path", ""))
            metadata = probe_video(path)
            response: dict[str, Any] = {"metadata": metadata, "network_access": False}
            query = str(params.get("query", "")).strip()
            transcript = params.get("transcript") or []
            if query and isinstance(transcript, list):
                response["hits"] = [
                    {"timestamp_seconds": hit.timestamp_seconds, "text": hit.text}
                    for hit in search_transcript(transcript, query)
                ]
            if params.get("thumbnail_at") is not None:
                with tempfile.TemporaryDirectory(prefix="jarvis-video-") as temp_dir:
                    output = Path(temp_dir) / "thumbnail.jpg"
                    extract_thumbnail(path, float(params["thumbnail_at"]), output)
                    response["thumbnail_bytes"] = output.stat().st_size
                    # The temporary image is intentionally deleted after returning
                    # metadata; callers can request a controlled artifact later.
            return ToolResult(
                tool_name=self.tool_id,
                content=f"Inspected {metadata['filename']} ({metadata['duration_seconds']:.2f}s).",
                success=True,
                metadata=response,
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=f"Video analysis failed: {exc}", success=False)


__all__ = ["VideoAnalyzeTool"]

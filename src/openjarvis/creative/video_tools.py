"""Creative video tools — the agentic editing studio + generation.

* ``video_edit`` — the OpenCut replacement: one tool that exposes the whole
  ffmpeg engine either as single operations (cut/crop/zoom/…) or as a full
  declarative multi-track timeline (transitions, overlays, text, audio mix).
* ``media_video_generate`` — text-to-video / image-to-video through the
  configured provider (NVIDIA NIM Wan2.2 by default) with live polling
  status surfaced back into the tool result.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths
from openjarvis.creative.ffmpeg_engine import (
    FFmpegError,
    SimpleEditor,
    render_timeline,
)
from openjarvis.creative.providers import GenerationError, generate_video

logger = logging.getLogger(__name__)

_OPS = [
    "cut", "crop", "scale", "zoom", "speed", "reverse", "rotate", "flip",
    "volume", "mute", "fade", "effects", "concat", "watermark", "text",
    "extract_audio", "replace_audio", "add_audio", "boomerang", "loop",
    "gif", "thumbnail", "burn_subtitles", "timeline", "probe",
]


def _md_video(url: str, label: str = "") -> str:
    return f"[{label or 'video'}]({url})"


@ToolRegistry.register("video_edit")
class VideoEditTool(BaseTool):
    """Professional video editing studio, driven entirely from the chat."""

    tool_id = "video_edit"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_edit",
            description=(
                "Edit video like a professional editor — fully offline via"
                " ffmpeg, no browser. Two modes: (1) single operations:"
                " cut, crop, scale, zoom (Ken Burns), speed, reverse,"
                " rotate, flip, volume, mute, fade, effects (brightness/"
                " contrast/ saturation/blur/sharpen/sepia/vignette/...),"
                " concat (optionally with xfade transitions — fade, wipeleft,"
                " circleopen, slideup, dissolve and 50+ more), watermark,"
                " text burn (full Arabic support), extract_audio,"
                " replace_audio, add_audio (background music), boomerang,"
                " loop, gif, thumbnail, burn_subtitles, probe. (2) timeline"
                " mode: pass a full multi-track project as the 'timeline'"
                " JSON (video clips + overlays + text clips + audio tracks"
                " with per-clip effects, transitions, Ken Burns, fit modes),"
                " rendered with the studio pipeline. Set dry_run=true to"
                " preview the exact ffmpeg commands without executing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "video": {
                        "type": "string",
                        "description": "Path or URL of the video to edit.",
                    },
                    "operation": {
                        "type": "string",
                        "enum": _OPS,
                        "description": "Editing operation to perform.",
                    },
                    "start": {"type": "number", "description": "Start time in seconds (cut/zoom)."},
                    "end": {"type": "number", "description": "End time in seconds (cut/zoom)."},
                    "x": {"type": "integer", "description": "Crop left offset."},
                    "y": {"type": "integer", "description": "Crop top offset."},
                    "width": {"type": "integer", "description": "Target width (crop/scale)."},
                    "height": {"type": "integer", "description": "Target height (crop/scale)."},
                    "mode": {
                        "type": "string", "enum": ["cover", "contain", "fill"],
                        "description": "Scale fit mode (default cover).",
                    },
                    "value": {
                        "type": "number",
                        "description": "Generic parameter (speed factor, volume level, rotate degrees, fade seconds …).",
                    },
                    "factor": {"type": "number", "description": "Speed factor alias."},
                    "zoom_to": {"type": "number", "description": "Ken Burns target zoom (default 1.25)."},
                    "direction": {
                        "type": "string", "enum": ["center", "left", "right", "top", "bottom"],
                        "description": "Ken Burns pan direction.",
                    },
                    "effects": {
                        "type": "array", "items": {"type": "object"},
                        "description": "Effect list for the effects op (e.g. [{type: 'brightness', amount: 0.1}]).",
                    },
                    "videos": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Source list for concat.",
                    },
                    "transition": {
                        "type": "string",
                        "description": "xfade transition for concat (e.g. fade, circleopen, slideleft).",
                    },
                    "transition_duration": {"type": "number", "default": 0.7},
                    "text": {
                        "type": "string",
                        "description": "Text for the text op (Arabic supported).",
                    },
                    "text_style": {
                        "type": "string",
                        "enum": ["title", "subtitle", "caption", "kicker", "quote"],
                    },
                    "position": {"type": "string", "description": "Text/watermark position."},
                    "overlay_src": {"type": "string", "description": "Watermark image path."},
                    "opacity": {"type": "number", "description": "Watermark/overlay opacity 0-1."},
                    "audio_src": {
                        "type": "string",
                        "description": "Audio file for add_audio/replace_audio.",
                    },
                    "audio_volume": {"type": "number", "description": "Volume for add_audio (0-1)."},
                    "subtitle_src": {"type": "string", "description": "SRT/ASS subtitle file."},
                    "timeline": {
                        "type": "object",
                        "description": "Full timeline project (see studio DSL).",
                    },
                    "dry_run": {
                        "type": "boolean", "default": False,
                        "description": "Preview commands without executing.",
                    },
                },
                "required": ["operation"],
            },
            category="media",
            timeout_seconds=1800.0,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _editor() -> SimpleEditor:
        return SimpleEditor(timeout=1500)

    def _result_for(self, op: str, out: Any, *, extra: str = "") -> ToolResult:
        if out is None:
            return ToolResult(tool_name="video_edit", content="No output.", success=False)
        path = Path(str(out))
        if not path.exists():
            return ToolResult(tool_name="video_edit",
                              content=f"Output missing: {path}", success=False)
        size_mb = path.stat().st_size / (1024 * 1024)
        url = _paths.media_url(path)
        content = (
            f"{op} ✓ → `{path}` ({size_mb:.2f} MB)\n"
            + _md_video(url, op)
            + (f"\n{extra}" if extra else "")
        )
        return ToolResult(tool_name="video_edit", content=content, success=True)

    # -- dispatch ------------------------------------------------------------

    def execute(self, **params: Any) -> ToolResult:
        operation = str(params.get("operation") or "").strip().lower()
        if not operation or operation not in _OPS:
            return ToolResult(
                tool_name="video_edit",
                content=f"Unknown operation '{operation}'. Valid: {', '.join(_OPS)}",
                success=False,
            )
        dry_run = bool(params.get("dry_run"))
        ed = self._editor()
        name = self.tool_id

        try:
            if operation == "probe":
                from openjarvis.creative.ffmpeg_engine import probe, resolve_media

                info = probe(resolve_media(str(params.get("video") or "")))
                return ToolResult(tool_name=name, content=json.dumps(info, indent=2),
                                  success=True)

            if operation == "timeline":
                timeline = params.get("timeline")
                if not isinstance(timeline, dict):
                    return ToolResult(tool_name=name,
                                      content="'timeline' (object) is required for timeline mode.",
                                      success=False)
                if dry_run:
                    plan = render_timeline(timeline, dry_run=True)
                    return ToolResult(
                        tool_name=name,
                        content="PLANNED COMMANDS (dry run):\n\n```\n" +
                                "\n\n".join(plan["commands"]) + "\n```\n" +
                                f"Estimated duration: {plan.get('estimated_duration')}s @ {plan.get('canvas')}",
                        success=True,
                    )
                result = render_timeline(timeline)
                size_mb = result["size_bytes"] / (1024 * 1024)
                content = (
                    f"timeline render ✓ → `{result['path']}`\n"
                    f"- Duration: {result['duration']}s @ {result['canvas']}, {result['fps']}fps\n"
                    f"- Tracks: {result['tracks']}\n"
                    f"- Size: {size_mb:.2f} MB\n"
                    + _md_video(result["url"], "▶ watch")
                    + (f"\nThumbnail: {result['thumbnail_url']}" if result.get("thumbnail_url") else "")
                )
                return ToolResult(tool_name=name, content=content, success=True)

            video = str(params.get("video") or "").strip()
            if not video:
                return ToolResult(tool_name=name, content="'video' is required.", success=False)

            if operation == "cut":
                out = ed.cut(video, float(params.get("start") or 0),
                             float(params.get("end") or 0))
            elif operation == "crop":
                out = ed.crop(video, int(params.get("x") or 0), int(params.get("y") or 0),
                              int(params.get("width") or 0), int(params.get("height") or 0))
            elif operation == "scale":
                out = ed.scale(video, int(params.get("width") or 1280),
                               int(params.get("height") or 720),
                               mode=str(params.get("mode") or "cover"))
            elif operation == "zoom":
                out = ed.zoom(video, zoom_to=float(params.get("zoom_to") or params.get("value") or 1.25),
                              zoom_from=float(params.get("zoom_from") or 1.0),
                              direction=str(params.get("direction") or "center"),
                              start=float(params.get("start") or 0),
                              end=params.get("end"))
            elif operation == "speed":
                out = ed.speed(video, float(params.get("factor") or params.get("value") or 2.0))
            elif operation == "reverse":
                out = ed.reverse(video)
            elif operation == "rotate":
                out = ed.rotate(video, float(params.get("value") or 90))
            elif operation == "flip":
                axis = str(params.get("axis") or params.get("value") or "horizontal")
                out = ed.flip(video, "h" if str(axis).lower().startswith("h") else "v")
            elif operation == "volume":
                out = ed.volume(video, float(params.get("value") or 1.0))
            elif operation == "mute":
                out = ed.mute(video)
            elif operation == "fade":
                out = ed.fade(video,
                              fade_in=float(params.get("fade_in") or params.get("value") or 0),
                              fade_out=float(params.get("fade_out") or 0))
            elif operation == "effects":
                effects = params.get("effects") or []
                out = ed.effects(video, list(effects))
            elif operation == "concat":
                sources = list(params.get("videos") or [])
                if not sources:
                    return ToolResult(tool_name=name, content="'videos' list required.", success=False)
                out = ed.concat(sources, transition=params.get("transition"),
                                transition_duration=float(params.get("transition_duration") or 0.7))
            elif operation == "watermark":
                overlay_src = params.get("overlay_src") or params.get("audio_src")
                if not overlay_src:
                    return ToolResult(tool_name=name, content="'overlay_src' (image) required.", success=False)
                out = ed.watermark(video, str(overlay_src),
                                   position=str(params.get("position") or "bottom-right"),
                                   scale=float(params.get("scale") or 0.18),
                                   opacity=float(params.get("opacity") or 1.0))
            elif operation == "text":
                message = str(params.get("text") or "")
                if not message:
                    return ToolResult(tool_name=name, content="'text' required.", success=False)
                out = ed.text(video, message,
                              style=str(params.get("text_style") or "caption"),
                              position=str(params.get("position") or "bottom"))
            elif operation == "extract_audio":
                out = ed.extract_audio(video)
            elif operation == "replace_audio":
                audio_src = params.get("audio_src")
                if not audio_src:
                    return ToolResult(tool_name=name, content="'audio_src' required.", success=False)
                out = ed.replace_audio(video, str(audio_src))
            elif operation == "add_audio":
                audio_src = params.get("audio_src")
                if not audio_src:
                    return ToolResult(tool_name=name, content="'audio_src' required.", success=False)
                out = ed.add_audio(video, str(audio_src),
                                   audio_volume=float(params.get("audio_volume") or 0.4),
                                   fade_out=float(params.get("value") or 2.0))
            elif operation == "boomerang":
                out = ed.boomerang(video)
            elif operation == "loop":
                out = ed.loop(video, int(params.get("value") or 2))
            elif operation == "gif":
                out = ed.gif(video, fps=int(params.get("fps") or 12),
                             width=int(params.get("width") or 640))
            elif operation == "thumbnail":
                out = ed.thumbnail(video, at=params.get("at"))
            elif operation == "burn_subtitles":
                sub = params.get("subtitle_src")
                if not sub:
                    return ToolResult(tool_name=name, content="'subtitle_src' required.", success=False)
                out = ed.burn_subtitles(video, str(sub))
            else:  # pragma: no cover — _OPS covers everything
                return ToolResult(tool_name=name, content=f"Unhandled op {operation}.", success=False)

            if dry_run:
                return ToolResult(tool_name=name,
                                  content=f"DRY RUN [{operation}] planned: {out}",
                                  success=True)
            return self._result_for(operation, out)

        except (FFmpegError, ValueError) as exc:
            logger.warning("video_edit %s failed: %s", operation, exc)
            return ToolResult(tool_name=name, content=f"Edit failed: {exc}",
                              success=False)


@ToolRegistry.register("media_video_generate")
class MediaVideoGenerateTool(BaseTool):
    """Text-to-video / image-to-video via the configured provider."""

    tool_id = "media_video_generate"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="media_video_generate",
            description=(
                "Generate a video from a text prompt (text-to-video) or from"
                " a starting image plus prompt (image-to-video) using the"
                " configured media provider (default: NVIDIA NIM wan2.2)."
                " Generation is asynchronous — the tool polls the provider"
                " and reports progress; long jobs (30-120s) are normal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the video should show."},
                    "image": {"type": "string", "description": "Optional starting image path (image-to-video)."},
                    "duration": {"type": "number", "default": 5, "description": "Seconds (provider-dependent)."},
                    "aspect_ratio": {"type": "string", "default": "16:9",
                                     "description": "Aspect ratio (16:9, 9:16, 1:1)."},
                    "provider": {"type": "string", "description": "Provider override."},
                    "model": {"type": "string", "description": "Model override (e.g. wan2.2-t2v / wan2.2-i2v)."},
                },
                "required": ["prompt"],
            },
            category="media",
            timeout_seconds=1800.0,
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(tool_name="media_video_generate",
                              content="No prompt provided.", success=False)
        statuses: List[str] = []

        def on_status(message: str) -> None:
            statuses.append(message)
            logger.info("[media_video_generate] %s", message)

        try:
            result = generate_video(
                prompt,
                provider=params.get("provider"),
                model=params.get("model"),
                image=params.get("image"),
                duration=params.get("duration"),
                aspect_ratio=str(params.get("aspect_ratio") or "16:9"),
                on_status=on_status,
                stem="gen",
            )
        except (GenerationError, ValueError) as exc:
            logger.warning("media_video_generate failed: %s", exc)
            return ToolResult(tool_name="media_video_generate",
                              content=f"Video generation failed: {exc}", success=False)
        video = result["video"]
        path = Path(video["path"])
        size_mb = path.stat().st_size / (1024 * 1024)
        content = (
            f"Generated via **{result['provider']}** (`{result['model']}`)\n"
            f"→ `{path}` ({size_mb:.2f} MB)\n"
            + _md_video(video["url"], "▶ watch the generated video")
            + (f"\nProgress: {' → '.join(statuses[-3:])}" if statuses else "")
        )
        return ToolResult(tool_name="media_video_generate", content=content,
                          success=True)


__all__ = ["VideoEditTool", "MediaVideoGenerateTool"]

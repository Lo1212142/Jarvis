"""Bounded CPU-first video indexing primitives.

The analyzer treats videos as untrusted media. It invokes ffprobe/ffmpeg with
fixed argument arrays, never executes embedded content, and enforces size/time
limits. Speech-to-text can be plugged in separately through a configured API.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
_MAX_PROBE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class VideoHit:
    timestamp_seconds: float
    text: str


def _validate_path(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise ValueError("video path must be an existing file")
    if target.stat().st_size > _MAX_VIDEO_BYTES:
        raise ValueError("video exceeds the configured 2GB limit")
    return target


def probe_video(path: str | Path) -> dict[str, Any]:
    target = _validate_path(path)
    command = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(target),
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=_MAX_PROBE_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"video metadata probe failed: {exc}") from exc
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe returned invalid metadata") from exc
    fmt = data.get("format", {})
    return {
        "filename": target.name,
        "size_bytes": target.stat().st_size,
        "duration_seconds": float(fmt.get("duration", 0.0) or 0.0),
        "format_name": fmt.get("format_name", ""),
        "streams": [
            {
                "index": stream.get("index"),
                "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
            }
            for stream in data.get("streams", [])
        ],
    }


def search_transcript(transcript: list[dict[str, Any]], query: str) -> list[VideoHit]:
    """Search timestamped transcript segments case-insensitively."""
    needle = query.strip().casefold()
    if not needle:
        return []
    hits: list[VideoHit] = []
    for segment in transcript:
        text = str(segment.get("text", ""))
        if needle in text.casefold():
            try:
                timestamp = float(segment.get("start", segment.get("timestamp", 0.0)))
            except (TypeError, ValueError):
                timestamp = 0.0
            hits.append(VideoHit(timestamp_seconds=max(0.0, timestamp), text=text))
    return hits[:100]


def extract_thumbnail(path: str | Path, timestamp_seconds: float, output_path: str | Path) -> str:
    """Extract one bounded JPEG thumbnail at a requested timestamp."""
    target = _validate_path(path)
    if timestamp_seconds < 0:
        raise ValueError("timestamp_seconds cannot be negative")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp_seconds),
        "-i", str(target), "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
        "-q:v", "5", "-y", str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"thumbnail extraction failed: {exc}") from exc
    return str(output)


__all__ = ["VideoHit", "extract_thumbnail", "probe_video", "search_transcript"]

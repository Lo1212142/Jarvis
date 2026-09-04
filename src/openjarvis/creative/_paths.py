"""Filesystem layout for the creative suite.

All generated media lives under ``~/.openjarvis/media/creative`` so it is
easy to back up, never touches the source tree, and survives upgrades.
"""

from __future__ import annotations

import time
from pathlib import Path

from openjarvis.core.paths import get_config_dir


def creative_root() -> Path:
    """Root directory holding all creative-suite media (created on demand)."""
    root = get_config_dir() / "media" / "creative"
    root.mkdir(parents=True, exist_ok=True)
    return root


def subdir(name: str) -> Path:
    """Named subdirectory under the creative root (created on demand)."""
    path = creative_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def images_dir() -> Path:
    return subdir("images")


def videos_dir() -> Path:
    return subdir("videos")


def audio_dir() -> Path:
    return subdir("audio")


def projects_dir() -> Path:
    return subdir("projects")


def thumbs_dir() -> Path:
    return subdir("thumbs")


def tmp_dir() -> Path:
    return subdir("tmp")


def new_media_path(kind: str, ext: str, *, stem: str = "") -> Path:
    """Build a collision-free output path for a new media artifact.

    ``kind`` is one of ``image``/``video``/``audio``/``project``; the result
    lives in the matching directory with a timestamped filename.
    """
    directory = {
        "image": images_dir,
        "video": videos_dir,
        "audio": audio_dir,
        "project": projects_dir,
    }.get(kind, subdir)
    ts = time.strftime("%Y%m%d-%H%M%S")
    clean = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)[:48].strip("-")
    name = f"{ts}-{clean}.{ext.lstrip('.')}" if clean else f"{ts}.{ext.lstrip('.')}"
    return directory() / name


def tmp_workdir() -> Path:
    """Fresh scratch directory for one render job (caller cleans up)."""
    path = tmp_dir() / f"job-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000000}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_url(path: str | Path) -> str:
    """Relative URL under which ``creative_routes`` serves the media file."""
    try:
        rel = Path(path).resolve().relative_to(creative_root().resolve())
    except ValueError:
        rel = Path(path).name
    return f"/media/creative/{rel.as_posix()}"


__all__ = [
    "creative_root",
    "subdir",
    "images_dir",
    "videos_dir",
    "audio_dir",
    "projects_dir",
    "thumbs_dir",
    "tmp_dir",
    "tmp_workdir",
    "new_media_path",
    "media_url",
]

"""FFmpeg-powered professional video editing engine.

This module is the programmatic "editing studio" that replaces GUI editors
for agentic use: every CapCut-style capability (cut, crop, zoom, speed,
transitions, multi-track, effects, text, audio mixing …) is exposed as a
plain Python function that compiles to one or more ``ffmpeg`` commands —
no browser, no UI, fully deterministic.

Design:
* :func:`probe` — ffprobe metadata for any media file/URL.
* :func:`run_ffmpeg` — subprocess runner with timeout + rich errors.
* :class:`SimpleEditor` — single-operation fast path (one command).
* :func:`render_timeline` — declarative timeline DSL → multi-pass render
  (normalize segments → xfade transition chain → overlays → text → audio
  mix → export presets).

Everything is stream-copy-avoidant only where filters require re-encoding;
uniform segments allow lossless concat for transition-less timelines.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openjarvis.creative import _paths, text_render

logger = logging.getLogger(__name__)

FFMPEG_BIN = os.environ.get("JARVIS_FFMPEG", "ffmpeg")
FFPROBE_BIN = os.environ.get("JARVIS_FFPROBE", "ffprobe")

DEFAULT_TIMEOUT = 900.0  # 15 min per ffmpeg command


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg command fails or times out."""


# ---------------------------------------------------------------------------
# Probe & runner
# ---------------------------------------------------------------------------


def probe(path: str | Path) -> Dict[str, Any]:
    """Return media metadata: duration, width, height, fps, audio presence …"""
    target = str(path)
    cmd = [
        FFPROBE_BIN, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise FFmpegError(f"ffprobe not found at '{FFPROBE_BIN}'") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffprobe timed out on {target}") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {target}: {proc.stderr[-400:]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    def _frac(value: Any) -> float:
        try:
            num, _, den = str(value).partition("/")
            num_f, den_f = float(num), float(den or 1)
            return num_f / den_f if den_f else 0.0
        except (TypeError, ValueError):
            return 0.0

    width = int(video.get("width", 0) or 0) if video else 0
    height = int(video.get("height", 0) or 0) if video else 0
    return {
        "path": target,
        "duration": float(fmt.get("duration", 0) or 0) or _frac(
            (video or {}).get("duration", 0)
        ),
        "width": width,
        "height": height,
        "fps": _frac((video or {}).get("avg_frame_rate", "0/1")) or 30.0,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "vcodec": (video or {}).get("codec_name", ""),
        "acodec": (audio or {}).get("codec_name", ""),
        "sample_rate": int((audio or {}).get("sample_rate", 0) or 0),
        "nb_frames": int((video or {}).get("nb_frames", 0) or 0),
        "bit_rate": fmt.get("bit_rate", ""),
    }


def run_ffmpeg(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    label: str = "ffmpeg",
) -> Tuple[int, str]:
    """Run an ffmpeg command, returning (returncode, stderr_tail)."""
    cmd = [FFMPEG_BIN, "-hide_banner", "-nostdin", "-y", *args]
    logger.debug("[%s] %s", label, " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"ffmpeg not found at '{FFMPEG_BIN}'") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"[{label}] ffmpeg timed out after {timeout:.0f}s") from exc
    tail = (proc.stderr or "")[-2000:]
    if proc.returncode != 0:
        raise FFmpegError(f"[{label}] ffmpeg exited {proc.returncode}\n{tail}")
    return proc.returncode, tail


def resolve_media(src: str) -> str:
    """Resolve a media reference to an ffmpeg-ready input string.

    Accepts absolute paths, URLs (http/https — passed through), paths
    relative to the CWD, and paths relative to the creative media root.
    """
    if not src:
        raise FFmpegError("empty media path")
    if src.startswith(("http://", "https://", "rtmp://", "rtsp://")):
        return src
    candidate = Path(src).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)
    for base in (Path.cwd(), _paths.creative_root()):
        local = base / candidate
        if local.exists():
            return str(local.resolve())
    if candidate.exists():
        return str(candidate.resolve())
    raise FFmpegError(f"media file not found: {src}")


def _secs(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _canvas_of(project: Dict[str, Any]) -> Tuple[int, int, int]:
    """(width, height, fps) for a timeline project, with sane bounds."""
    width = int(project.get("width") or 0)
    height = int(project.get("height") or 0)
    if width <= 0 or height <= 0:
        canvas = str(project.get("canvas") or "1920x1080")
        try:
            width, height = (int(v) for v in canvas.lower().split("x"))
        except ValueError:
            width, height = 1920, 1080
    width = min(max(64, width), 4096)
    height = min(max(64, height), 4096)
    fps = int(float(project.get("fps") or 30))
    fps = min(max(1, fps), 120)
    return width, height, fps


# ---------------------------------------------------------------------------
# Filter builders (shared by simple ops and the timeline renderer)
# ---------------------------------------------------------------------------

_FIT_FILTERS = {
    "cover": "scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
    "contain": (
        "scale={w}:{h}:force_original_aspect_ratio=decrease,"
        "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={color}"
    ),
    "fill": "scale={w}:{h}",
    "blurpad": (
        "scale={w}:{h}:force_original_aspect_ratio=decrease,"
        "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    ),
}

_TRANSITIONS = {
    # All xfade transitions shipped with ffmpeg ≥ 4.3 (58 presets).
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance", "fadeblack", "fadewhite",
    "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose",
    "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur", "fadegrays", "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin",
}

_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}


def looks_like_image(src: str) -> bool:
    return Path(src.split("?")[0]).suffix.lower() in _IMAGE_EXTS


def looks_like_audio(src: str) -> bool:
    return Path(src.split("?")[0]).suffix.lower() in _AUDIO_EXTS


def build_effects_chain(effects: Optional[List[Dict[str, Any]]]) -> str:
    """Translate a declarative effect list into an ffmpeg filter chain.

    Supported effects (all with tunable parameters):
    brightness, contrast, saturation, grayscale, sepia, hue, blur, sharpen,
    vignette, noise, denoise, colorbalance, gamma, fade_in, fade_out.
    """
    parts: List[str] = []
    for effect in effects or []:
        kind = str((effect or {}).get("type", "")).lower()
        if not kind:
            continue
        amount = float(effect.get("amount", effect.get("value", 1.0)) or 1.0)
        if kind in ("brightness", "contrast", "saturation", "gamma"):
            key = {"brightness": "brightness", "contrast": "contrast",
                   "saturation": "saturation", "gamma": "gamma"}[kind]
            parts.append(f"eq={key}={amount}")
        elif kind == "grayscale":
            parts.append("eq=saturation=0")
        elif kind == "sepia":
            parts.append(
                "colorchannelmixer="
                "rr=.393:rg=.769:rb=.189:"
                "gr=.214:gg=.627:gb=.288:"
                "br=.15:bg=.293:bb=.062"
            )
        elif kind == "hue":
            parts.append(f"hue=h={amount}")
        elif kind == "blur":
            parts.append(f"gblur=sigma={max(0.3, amount)}")
        elif kind == "sharpen":
            parts.append(f"unsharp=5:5:{max(0.5, amount)}")
        elif kind == "vignette":
            parts.append("vignette")
        elif kind == "noise":
            parts.append(f"noise=alls={int(max(1, amount))}:allf=t+u")
        elif kind == "denoise":
            parts.append("hqdn3d=4:4:6:6")
        elif kind == "colorbalance":
            parts.append(
                f"colorbalance={amount:.2f}:{amount:.2f}:{max(0.0, amount - 0.05):.2f}"
            )
        elif kind == "fade_in":
            parts.append(f"fade=t=in:st=0:d={_secs(effect.get('duration', 0.6), 0.6)}")
        elif kind == "fade_out":
            # st is filled by the caller (needs clip duration) — placeholder
            parts.append(f"fade=t=out:st=__FOUT__+0:d={_secs(effect.get('duration', 0.6), 0.6)}")
        else:
            raise FFmpegError(f"unknown effect type: {kind}")
    return ",".join(parts)


def _atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5–2 per instance; chain for extreme speeds."""
    speed = min(max(speed, 0.1), 10.0)
    parts: List[str] = []
    remaining = speed
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.4f}")
    return ",".join(parts)


def _kenburns_filter(
    effect: Dict[str, Any], *, fps: int, width: int, height: int, frames: int
) -> str:
    """Build the zoompan filter for a Ken Burns (zoom/pan) effect.

    ``zoom_from``/``zoom_to`` (default 1.0→1.18), optional ``direction``
    for the pan anchor. Pre-scaling to ~2x suppresses zoompan jitter.
    """
    zoom_from = max(1.0, float(effect.get("zoom_from", 1.0) or 1.0))
    zoom_to = max(1.0, float(effect.get("zoom_to", 1.18) or 1.18))
    if zoom_to < zoom_from:
        zoom_from, zoom_to = zoom_to, zoom_from  # zoom out handled by swap
    direction = str(effect.get("direction", "center")).lower()
    upscale = max(2.0, zoom_to * 1.1)
    pre = f"scale={int(width * upscale)}:{int(height * upscale)}"
    total = max(1, frames)
    z = f"max({zoom_from}+({zoom_to}-{zoom_from})*on/{total},1.001)"
    if direction == "center":
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    else:
        # Pan anchor: start biased toward one edge, drift to the opposite.
        bias = {
            "left": ("0", "ih/2-(ih/zoom/2)"),
            "right": ("iw-(iw/zoom)", "ih/2-(ih/zoom/2)"),
            "top": ("iw/2-(iw/zoom/2)", "0"),
            "bottom": ("iw/2-(iw/zoom/2)", "ih-(ih/zoom)"),
        }.get(direction, ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"))
        x = f"{bias[0]}"
        y = f"{bias[1]}"
    return (
        f"{pre},zoompan=z='{z}':x='{x}':y='{y}'"
        f":d=1:fps={fps}:s={width}x{height},setpts=N/{fps}/TB"
    )


# ---------------------------------------------------------------------------
# SimpleEditor — single-operation fast path (one ffmpeg command)
# ---------------------------------------------------------------------------


class SimpleEditor:
    """One-shot operations on a single video/audio file.

    Every method takes plain parameters, executes exactly one ffmpeg
    command (plus optional Pillow passes for text), and returns the
    output path. ``dry_run=True`` returns the command instead.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    # -- helpers ----------------------------------------------------------

    def _out(self, src: str, suffix: str, ext: str = "mp4") -> Path:
        stem = Path(src).stem[:40] or "media"
        return _paths.new_media_path("video", ext, stem=f"{stem}-{suffix}")

    def _run(self, args: List[str], label: str, dry_run: bool = False) -> Path | str:
        if dry_run:
            return " ".join([FFMPEG_BIN, "-y", *args])
        run_ffmpeg(args, timeout=self.timeout, label=label)
        # The caller returns the concrete output path separately.
        return Path(args[-1])

    # -- core operations --------------------------------------------------

    def cut(self, src: str, start: float, end: float, *, dry_run: bool = False) -> Path | str:
        """Trim to [start, end] seconds (re-encoded for frame accuracy)."""
        src_r = resolve_media(src)
        info = probe(src_r)
        start = _secs(start)
        end = _secs(end, info["duration"]) or info["duration"]
        if end <= start:
            raise FFmpegError(f"cut end ({end}) must be > start ({start})")
        out = self._out(src, "cut")
        args = [
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", src_r,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "cut", dry_run)
        return out if not dry_run else args

    def crop(self, src: str, x: int, y: int, w: int, h: int, *, dry_run: bool = False) -> Path | str:
        """Crop a rectangle (pixels from top-left of the source)."""
        src_r = resolve_media(src)
        info = probe(src_r)
        w, h = int(w), int(h)
        x, y = int(x), int(y)
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise FFmpegError("crop rectangle must be positive")
        if x + w > info["width"] or y + h > info["height"]:
            raise FFmpegError(
                f"crop {x}+{w}/{y}+{h} exceeds frame {info['width']}x{info['height']}"
            )
        out = self._out(src, "crop")
        args = [
            "-i", src_r, "-vf", f"crop={w}:{h}:{x}:{y},setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "crop", dry_run)
        return out if not dry_run else args

    def scale(self, src: str, width: int, height: int, *, mode: str = "cover",
              dry_run: bool = False) -> Path | str:
        """Rescale to exact size; mode: cover | contain | fill."""
        src_r = resolve_media(src)
        width, height = int(width), int(height)
        vf = _FIT_FILTERS.get(mode, _FIT_FILTERS["cover"]).format(
            w=width, h=height, color="black"
        ) + ",setsar=1"
        out = self._out(src, "scale")
        args = [
            "-i", src_r, "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "scale", dry_run)
        return out if not dry_run else args

    def zoom(self, src: str, *, zoom_to: float = 1.25, zoom_from: float = 1.0,
             direction: str = "center", start: float = 0.0, end: float | None = None,
             fps: int = 30, dry_run: bool = False) -> Path | str:
        """Ken Burns zoom on (a slice of) the video."""
        src_r = resolve_media(src)
        info = probe(src_r)
        end = _secs(end, info["duration"]) or info["duration"]
        dur = max(0.5, end - _secs(start))
        canvas_w = info["width"]
        canvas_h = info["height"]
        # Fit even dims to keep yuv420p happy.
        canvas_w -= canvas_w % 2
        canvas_h -= canvas_h % 2
        frames = int(dur * fps)
        kb = _kenburns_filter(
            {"zoom_from": zoom_from, "zoom_to": zoom_to, "direction": direction},
            fps=fps, width=canvas_w, height=canvas_h, frames=frames,
        )
        out = self._out(src, "zoom")
        args = [
            "-ss", f"{_secs(start):.3f}", "-to", f"{end:.3f}", "-i", src_r,
            # NB: no trailing fps filter after zoompan — zoompan timestamps
            # are frame-index based; an fps filter after it explodes CFR
            # conversion. setpts=N/fps/TB (inside _kenburns_filter) is the
            # correct normalizer.
            "-vf", f"{kb},setsar=1,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "zoom", dry_run)
        return out if not dry_run else args

    def speed(self, src: str, factor: float, *, dry_run: bool = False) -> Path | str:
        """Change playback speed (audio tempo-compensated when present)."""
        if factor <= 0:
            raise FFmpegError("speed factor must be > 0")
        src_r = resolve_media(src)
        info = probe(src_r)
        out = self._out(src, f"speed{factor}x")
        vf = f"setpts=PTS/{factor:.4f}"
        if info["has_audio"]:
            args = ["-i", src_r, "-filter_complex",
                    f"[0:v]{vf}[v];[0:a]{_atempo_chain(factor)}[a]",
                    "-map", "[v]", "-map", "[a]"]
        else:
            args = ["-i", src_r, "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]"]
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "speed", dry_run)
        return out if not dry_run else args

    def reverse(self, src: str, *, audio: bool = True, dry_run: bool = False) -> Path | str:
        """Play backwards (audio reversed too when available)."""
        src_r = resolve_media(src)
        info = probe(src_r)
        out = self._out(src, "reverse")
        if audio and info["has_audio"]:
            fc = "[0:v]reverse[v];[0:a]areverse[a]"
            args = ["-i", src_r, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            args = ["-i", src_r, "-filter_complex", "[0:v]reverse[v]", "-map", "[v]"]
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "reverse", dry_run)
        return out if not dry_run else args

    def rotate(self, src: str, degrees: float, *, dry_run: bool = False) -> Path | str:
        """Rotate by degrees (90/180/270 use fast transpose)."""
        deg = float(degrees) % 360
        table = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}
        vf = table.get(int(deg) if deg == int(deg) else -1, f"rotate={deg * 3.141592653589793 / 180}:ow=rotw(iw):oh=roth(ih)")
        src_r = resolve_media(src)
        out = self._out(src, "rotate")
        args = [
            "-i", src_r, "-vf", f"{vf},setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "rotate", dry_run)
        return out if not dry_run else args

    def flip(self, src: str, axis: str = "horizontal", *, dry_run: bool = False) -> Path | str:
        """Mirror horizontally or vertically."""
        vf = "hflip" if axis.lower().startswith("h") else "vflip"
        src_r = resolve_media(src)
        out = self._out(src, "flip")
        args = [
            "-i", src_r, "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "flip", dry_run)
        return out if not dry_run else args

    def volume(self, src: str, level: float, *, dry_run: bool = False) -> Path | str:
        """Adjust audio volume (1.0 = unchanged)."""
        if level < 0:
            raise FFmpegError("volume level must be >= 0")
        src_r = resolve_media(src)
        out = self._out(src, "volume", ext="m4a")
        args = ["-i", src_r, "-af", f"volume={level}", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", str(out)]
        self._run(args, "volume", dry_run)
        return out if not dry_run else args

    def mute(self, src: str, *, dry_run: bool = False) -> Path | str:
        """Remove the audio track entirely."""
        src_r = resolve_media(src)
        out = self._out(src, "mute")
        args = ["-i", src_r, "-an", "-c:v", "copy", "-movflags", "+faststart", str(out)]
        self._run(args, "mute", dry_run)
        return out if not dry_run else args

    def fade(self, src: str, *, fade_in: float = 0.0, fade_out: float = 0.0,
             audio: bool = True, dry_run: bool = False) -> Path | str:
        """Add video (and audio) fade in/out in seconds."""
        if fade_in <= 0 and fade_out <= 0:
            raise FFmpegError("at least one of fade_in/fade_out must be > 0")
        src_r = resolve_media(src)
        info = probe(src_r)
        vparts: List[str] = []
        aparts: List[str] = []
        if fade_in > 0:
            vparts.append(f"fade=t=in:st=0:d={fade_in}")
            aparts.append(f"afade=t=in:st=0:d={fade_in}")
        if fade_out > 0:
            fout_st = max(0.0, info["duration"] - fade_out)
            vparts.append(f"fade=t=out:st={fout_st:.3f}:d={fade_out}")
            aparts.append(f"afade=t=out:st={fout_st:.3f}:d={fade_out}")
        out = self._out(src, "fade")
        args = ["-i", src_r]
        if info["has_audio"] and audio:
            args += ["-vf", ",".join(vparts) or "null", "-af", ",".join(aparts) or "null",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-vf", ",".join(vparts) or "null", "-an",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                     "-pix_fmt", "yuv420p"]
        args += ["-movflags", "+faststart", str(out)]
        self._run(args, "fade", dry_run)
        return out if not dry_run else args

    def effects(self, src: str, effect_list: List[Dict[str, Any]],
                *, dry_run: bool = False) -> Path | str:
        """Apply a color/effect chain (brightness, blur, sepia, …)."""
        src_r = resolve_media(src)
        info = probe(src_r)
        chain = build_effects_chain(effect_list)
        if "__FOUT__" in chain:
            chain = chain.replace("__FOUT__", f"{max(0.0, info['duration'] - 0.6):.3f}")
        if not chain:
            raise FFmpegError("no valid effects provided")
        out = self._out(src, "fx")
        args = [
            "-i", src_r, "-vf", chain,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out),
        ]
        self._run(args, "effects", dry_run)
        return out if not dry_run else args

    def concat(self, sources: List[str], *, transition: str | None = None,
               transition_duration: float = 0.7, dry_run: bool = False) -> Path | str:
        """Join clips — with an xfade transition when requested."""
        if len(sources) < 2:
            raise FFmpegError("concat needs at least two sources")
        resolved = [resolve_media(s) for s in sources]
        infos = [probe(p) for p in resolved]
        out = _paths.new_media_path("video", "mp4", stem="concat")
        if not transition:
            # Normalize every input to a common canvas/fps, then concat.
            # (Inputs may differ in resolution/codec — stream-copy concat is
            # unsafe, so this path re-encodes. The timeline renderer keeps
            # the fast -c copy path for pre-normalized segments.)
            width = max((i["width"] for i in infos if i["width"]), default=1280)
            height = max((i["height"] for i in infos if i["height"]), default=720)
            width -= width % 2
            height -= height % 2
            fps = int(max((i["fps"] for i in infos if i["fps"]), default=30)) or 30
            inputs: List[str] = []
            maps: List[str] = []
            fc: List[str] = []
            for idx, info in enumerate(infos):
                inputs += ["-i", info["path"]]
                fc.append(
                    f"[{idx}:v]scale={width}:{height}"
                    f":force_original_aspect_ratio=increase,crop={width}:{height},"
                    f"setsar=1,fps={fps}[v{idx}]"
                )
                maps.append(f"[v{idx}]")
            first_audio = next((i for i, inf in enumerate(infos) if inf["has_audio"]), None)
            args = inputs + [
                "-filter_complex",
                ";".join(fc + ["".join(maps) + f"concat=n={len(infos)}:v=1:a=0[outv]"]),
                "-map", "[outv]",
            ]
            if first_audio is not None:
                args += ["-map", f"{first_audio}:a"]
            else:
                args += ["-an"]
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", str(out)]
            self._run(args, "concat", dry_run)
            return out if not dry_run else args
        # Transitional concat: uniformize then xfade chain.
        result = render_timeline(
            {
                "width": max(i["width"] for i in infos),
                "height": max(i["height"] for i in infos),
                "fps": int(max(i["fps"] for i in infos)) or 30,
                "tracks": [
                    {
                        "type": "video",
                        "clips": [
                            {
                                "src": p,
                                "transition_out": {
                                    "type": transition,
                                    "duration": transition_duration,
                                }
                                if idx < len(resolved) - 1
                                else None,
                            }
                            for idx, p in enumerate(resolved)
                        ],
                    }
                ],
                "export": {"format": "mp4", "quality": "high"},
            },
            str(out),
            dry_run=dry_run,
            timeout=self.timeout,
        )
        # Keep the SimpleEditor Path contract (the rich dict stays available
        # via render_timeline for callers that want URLs/thumbnails).
        if isinstance(result, dict):
            return Path(result.get("path", out))
        return out

    def watermark(self, src: str, overlay_src: str, *, position: str = "bottom-right",
                  scale: float = 0.18, opacity: float = 1.0, margin: int = 36,
                  dry_run: bool = False) -> Path | str:
        """Overlay a logo/image watermark."""
        src_r = resolve_media(src)
        info = probe(src_r)
        wm_r = resolve_media(overlay_src)
        out = self._out(src, "watermark")
        pos_map = {
            "top-left": f"{margin}:{margin}",
            "top-right": f"W-w-{margin}:{margin}",
            "bottom-left": f"{margin}:H-h-{margin}",
            "bottom-right": f"W-w-{margin}:H-h-{margin}",
            "center": "(W-w)/2:(H-h)/2",
        }
        xy = pos_map.get(position, pos_map["bottom-right"])
        alpha = f",format=rgba,colorchannelmixer=aa={min(1.0, max(0.05, opacity))}" if opacity < 1.0 else ""
        overlay_vf = f"scale=iw*{min(2.0, max(0.02, scale))}:-1{alpha}"
        args = [
            "-i", src_r, "-i", wm_r,
            "-filter_complex", f"[1:v]{overlay_vf}[wm];[0:v][wm]overlay={xy}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "watermark", dry_run)
        return out if not dry_run else args

    def text(self, src: str, message: str, *, style: str = "caption",
             position: str = "bottom", color: str | None = None,
             fade_in: float = 0.4, fade_out: float = 0.4,
             font_path: str | None = None, dry_run: bool = False) -> Path | str:
        """Burn styled text (Pillow-rendered — full Arabic support)."""
        from openjarvis.creative.text_render import render_text_overlay, save_png

        src_r = resolve_media(src)
        info = probe(src_r)
        W = info["width"] - info["width"] % 2
        H = info["height"] - info["height"] % 2
        overlay = render_text_overlay(
            message, canvas_size=(W, H), style=style, color=color,
            position=position, font_path=font_path,
        )
        tmp = _paths.tmp_workdir() / "text.png"
        save_png(overlay, tmp)
        dur = info["duration"]
        fo_st = max(0.0, dur - fade_out)
        out = self._out(src, "text")
        args = [
            "-i", src_r, "-loop", "1", "-t", f"{dur:.3f}", "-i", str(tmp),
            "-filter_complex",
            f"[1:v]format=rgba,fade=t=in:st=0:d={fade_in}:alpha=1,"
            f"fade=t=out:st={fo_st:.3f}:d={fade_out}:alpha=1[t];"
            f"[0:v][t]overlay=0:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
            "-shortest", str(out),
        ]
        try:
            self._run(args, "text", dry_run)
        finally:
            if not dry_run:
                shutil.rmtree(tmp.parent, ignore_errors=True)
        return out if not dry_run else args

    def extract_audio(self, src: str, *, dry_run: bool = False) -> Path | str:
        src_r = resolve_media(src)
        out = self._out(src, "audio", ext="m4a")
        args = ["-i", src_r, "-vn", "-c:a", "aac", "-b:a", "192k", str(out)]
        self._run(args, "extract_audio", dry_run)
        return out if not dry_run else args

    def replace_audio(self, video_src: str, audio_src: str, *, fade_out: float = 0.0,
                      dry_run: bool = False) -> Path | str:
        """Swap the audio track (trimmed to video length)."""
        v_r = resolve_media(video_src)
        info = probe(v_r)
        a_r = resolve_media(audio_src)
        out = self._out(video_src, "newaudio")
        af = f"afade=t=out:st={max(0.0, info['duration'] - fade_out):.3f}:d={fade_out}" if fade_out > 0 else "anull"
        args = [
            "-i", v_r, "-i", a_r,
            "-filter_complex", f"[1:a]{af},atrim=0:{info['duration']:.3f},asetpts=N/SR/TB[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out),
        ]
        self._run(args, "replace_audio", dry_run)
        return out if not dry_run else args

    def add_audio(self, video_src: str, audio_src: str, *, audio_volume: float = 1.0,
                  duck_original: bool = False, fade_out: float = 2.0,
                  dry_run: bool = False) -> Path | str:
        """Mix an extra track (e.g. background music) under existing audio."""
        v_r = resolve_media(video_src)
        info = probe(v_r)
        a_r = resolve_media(audio_src)
        out = self._out(video_src, "mix")
        dur = info["duration"]
        extra = f"[1:a]aloop=loop=-1:size=2e9,atrim=0:{dur:.3f},volume={audio_volume}"
        if fade_out > 0:
            extra += f",afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out}"
        extra += "[extra]"
        if info["has_audio"]:
            orig = "[0:a]volume=" + ("0.35" if duck_original else "1.0") + "[orig]"
            fc = f"{orig};{extra};[orig][extra]amix=inputs=2:normalize=0[outa]"
            map_a = "[outa]"
        else:
            fc = f"{extra.replace('[extra]', '[outa]')}"
            map_a = "[outa]"
        args = [
            "-i", v_r, "-stream_loop", "-1", "-i", a_r,
            "-filter_complex", fc, "-map", "0:v", "-map", map_a,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-t", f"{dur:.3f}", str(out),
        ]
        self._run(args, "add_audio", dry_run)
        return out if not dry_run else args

    def boomerang(self, src: str, *, dry_run: bool = False) -> Path | str:
        """Forward + reverse loop (social-media style)."""
        fwd = self.reverse(resolve_media(src), audio=True)
        out = self.concat([resolve_media(src), str(fwd)])
        return out

    def loop(self, src: str, times: int = 2, *, dry_run: bool = False) -> Path | str:
        """Repeat the clip N times."""
        if times < 2:
            raise FFmpegError("loop times must be >= 2")
        src_r = resolve_media(src)
        out = self._out(src, "loop")
        args = [
            "-stream_loop", str(times - 1), "-i", src_r,
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "loop", dry_run)
        return out if not dry_run else args

    def gif(self, src: str, *, fps: int = 12, width: int = 640,
            dry_run: bool = False) -> Path | str:
        """Convert to an optimized looping GIF."""
        src_r = resolve_media(src)
        out = self._out(src, "anim", ext="gif")
        vf = f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        args = ["-i", src_r, "-vf", vf, "-loop", "0", str(out)]
        self._run(args, "gif", dry_run)
        return out if not dry_run else args

    def thumbnail(self, src: str, *, at: float | None = None, width: int = 640,
                  dry_run: bool = False) -> Path | str:
        """Extract a poster frame as JPG."""
        src_r = resolve_media(src)
        info = probe(src_r)
        moment = _secs(at, info["duration"] / 2) if at is not None else max(0.0, info["duration"] / 2)
        out = _paths.new_media_path("image", "jpg", stem=Path(src_r).stem[:40])
        args = [
            "-ss", f"{moment:.3f}", "-i", src_r, "-frames:v", "1",
            "-vf", f"scale={width}:-2", str(out),
        ]
        self._run(args, "thumbnail", dry_run)
        return out if not dry_run else args

    def burn_subtitles(self, src: str, subtitle_path: str, *, dry_run: bool = False) -> Path | str:
        """Burn SRT/ASS subtitles into the video (libass)."""
        src_r = resolve_media(src)
        sub_r = resolve_media(subtitle_path)
        out = self._out(src, "subs")
        escaped = str(Path(sub_r).resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        args = [
            "-i", src_r, "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out),
        ]
        self._run(args, "subtitles", dry_run)
        return out if not dry_run else args


# ---------------------------------------------------------------------------
# Timeline DSL renderer — declarative multi-track project → rendered video
# ---------------------------------------------------------------------------


_POSITION_XY = {
    "center": ("(W-w)/2", "(H-h)/2"),
    "center-top": ("(W-w)/2", "0"),
    "top": ("(W-w)/2", "0"),
    "bottom": ("(W-w)/2", "(H-h)"),
    "top-left": ("0", "0"),
    "top-right": ("W-w", "0"),
    "bottom-left": ("0", "H-h"),
    "bottom-right": ("W-w", "H-h"),
    "left": ("0", "(H-h)/2"),
    "right": ("W-w", "(H-h)/2"),
}


def _xy_for(position: str, custom_x: Any = None, custom_y: Any = None) -> Tuple[str, str]:
    if custom_x is not None or custom_y is not None:
        return (str(int(custom_x or 0)), str(int(custom_y or 0)))
    key = str(position or "center").lower()
    return _POSITION_XY.get(key, _POSITION_XY["center"])


@dataclass
class _ClipSpec:
    """Internal normalized description of one video-track clip."""
    index: int
    src: str = ""
    kind: str = "video"          # video | image | gradient | solid
    gradient_style: str = "dark"
    accent: str = "#6C8CFF"
    accent2: str = "#9A6CFF"
    duration: float = 4.0        # final on-timeline duration
    in_point: float = 0.0
    fit: str = "cover"
    effects: List[Dict[str, Any]] = field(default_factory=list)
    kenburns: Optional[Dict[str, Any]] = None
    speed: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    keep_audio: bool = True
    audio_volume: float = 1.0
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    transition_out: Optional[Dict[str, Any]] = None
    source_duration: float = 0.0
    source_has_audio: bool = False
    segment_path: Path | None = None


def _parse_clip(index: int, raw: Dict[str, Any]) -> _ClipSpec:
    src = str(raw.get("src") or raw.get("source") or "").strip()
    kind = str(raw.get("kind") or "").strip().lower()
    color_hint = str(raw.get("color") or raw.get("background") or "").strip()
    if not kind:
        if not src:
            kind = "gradient"
        elif color_hint.startswith("#"):
            kind = "solid"
        elif looks_like_image(src):
            kind = "image"
        else:
            kind = "video"
    if kind == "solid" and not str(raw.get("style") or "").startswith("#") and color_hint.startswith("#"):
        raw = {**raw, "style": color_hint}
    duration = raw.get("duration")
    spec = _ClipSpec(
        index=index,
        src=src,
        kind=kind,
        gradient_style=str(raw.get("style") or raw.get("gradient") or "dark"),
        accent=str(raw.get("accent") or "#6C8CFF"),
        accent2=str(raw.get("accent2") or "#9A6CFF"),
        duration=_secs(duration, 0.0) if duration is not None else 0.0,
        in_point=_secs(raw.get("in") or raw.get("in_point") or 0.0),
        fit=str(raw.get("fit") or "cover").lower(),
        effects=list(raw.get("effects") or []),
        kenburns=raw.get("kenburns") or raw.get("zoom"),
        speed=max(0.1, float(raw.get("speed") or 1.0)),
        fade_in=_secs(raw.get("fade_in"), 0.0),
        fade_out=_secs(raw.get("fade_out"), 0.0),
        keep_audio=bool(raw.get("keep_audio", True)),
        audio_volume=max(0.0, float(raw.get("audio_volume", raw.get("volume", 1.0)) or 1.0)),
        audio_fade_in=_secs(raw.get("audio_fade_in"), 0.0),
        audio_fade_out=_secs(raw.get("audio_fade_out"), 0.0),
        transition_out=raw.get("transition_out") or None,
    )
    # Kenburns can also be expressed as an effect entry.
    if spec.kenburns is None:
        for effect in spec.effects:
            if str(effect.get("type", "")).lower() in ("kenburns", "zoom", "zoompan"):
                spec.kenburns = dict(effect)
                break
    return spec


def _normalize_transition(
    clip: _ClipSpec, next_clip: _ClipSpec
) -> Tuple[str, float]:
    tr = clip.transition_out or {}
    kind = str(tr.get("type") or "fade").lower()
    if kind not in _TRANSITIONS:
        kind = "fade"
    duration = _secs(tr.get("duration"), 0.7) or 0.7
    max_allowed = min(clip.duration, next_clip.duration) * 0.6
    duration = min(max(0.2, duration), max(0.25, max_allowed))
    return kind, round(duration, 3)


def _build_segment(
    clip: _ClipSpec, *, width: int, height: int, fps: int, quality: str,
    workdir: Path, timeout: float, dry_run: bool,
) -> Tuple[Path | str, float]:
    """Normalize one clip into a uniform segment (canvas, fps, audio, fx).

    Returns (segment_path, effective_duration).
    """
    crf, preset = {"high": ("18", "medium"), "balanced": ("21", "veryfast"),
                   "fast": ("24", "veryfast")}.get(quality, ("21", "veryfast"))
    seg = workdir / f"seg_{clip.index:03d}.mp4"

    # -- Resolve source & duration ---------------------------------------
    if clip.kind in ("gradient", "solid"):
        from openjarvis.creative.text_render import render_gradient_background, save_png

        if clip.kind == "solid" or str(clip.gradient_style).startswith("#"):
            color = clip.gradient_style if str(clip.gradient_style).startswith("#") else "#101018"
            bg_img = Image_single_color(width, height, color)
        else:
            bg_img = render_gradient_background(
                (width, height), style=clip.gradient_style,
                accent=clip.accent, accent2=clip.accent2,
            )
        src_path = workdir / f"bg_{clip.index:03d}.png"
        save_png(bg_img, src_path)
        clip.src = str(src_path)
        clip.kind = "image"
        clip.source_duration = 1e6
        clip.source_has_audio = False

    elif clip.kind == "image":
        clip.src = resolve_media(clip.src)
        clip.source_duration = 1e6
        clip.source_has_audio = False

    else:  # video
        clip.src = resolve_media(clip.src)
        info = probe(clip.src)
        clip.source_duration = info["duration"]
        clip.source_has_audio = info["has_audio"]
        if clip.speed != 1.0:
            clip.source_duration /= clip.speed

    if clip.duration <= 0:
        clip.duration = (
            min(clip.source_duration, 600.0)
            if clip.kind == "video" or clip.source_duration < 1e5
            else 4.0
        )
    if clip.kind == "video":
        clip.duration = min(clip.duration, max(0.2, clip.source_duration - clip.in_point))

    # -- Build the filter chains -----------------------------------------
    vparts: List[str] = []
    if clip.kenburns:
        frames = max(2, int(clip.duration * fps))
        kb = dict(clip.kenburns)
        vparts.append(_kenburns_filter(kb, fps=fps, width=width, height=height, frames=frames))
        # zoompan output is already CFR at `fps` via setpts — no fps filter.
    else:
        vparts.append(_FIT_FILTERS.get(clip.fit, _FIT_FILTERS["cover"]).format(
            w=width, h=height, color="black"
        ))
        vparts.append(f"fps={fps}")
    vparts.append("setsar=1")

    fx_chain = build_effects_chain(clip.effects)
    if fx_chain:
        fx_chain = fx_chain.replace("__FOUT__", f"{max(0.0, clip.duration - 0.6):.3f}")
        vparts.append(fx_chain)
    if clip.speed != 1.0 and clip.kind == "video":
        vparts.append(f"setpts=PTS/{clip.speed:.4f}")
    if clip.fade_in > 0:
        vparts.append(f"fade=t=in:st=0:d={clip.fade_in:.3f}")
    if clip.fade_out > 0:
        vparts.append(f"fade=t=out:st={max(0.0, clip.duration - clip.fade_out):.3f}:d={clip.fade_out:.3f}")
    vparts.append("format=yuv420p")
    vchain = ",".join(p for p in vparts if p)

    aparts: List[str] = []
    if clip.speed != 1.0 and clip.kind == "video":
        aparts.append(_atempo_chain(clip.speed))
    if clip.audio_volume != 1.0:
        aparts.append(f"volume={clip.audio_volume}")
    if clip.audio_fade_in > 0:
        aparts.append(f"afade=t=in:st=0:d={clip.audio_fade_in:.3f}")
    if clip.audio_fade_out > 0:
        aparts.append(f"afade=t=out:st={max(0.0, clip.duration - clip.audio_fade_out):.3f}:d={clip.audio_fade_out:.3f}")
    achain = ",".join(aparts) if aparts else "anull"

    # -- Input flags -------------------------------------------------------
    inputs: List[str] = []
    if clip.kind == "image":
        inputs += ["-loop", "1", "-t", f"{clip.duration:.3f}", "-i", clip.src]
    else:
        inputs += ["-ss", f"{clip.in_point:.3f}", "-t", f"{clip.duration * clip.speed:.3f}", "-i", clip.src]

    use_source_audio = clip.kind == "video" and clip.source_has_audio and clip.keep_audio
    if use_source_audio:
        filter_complex = f"[0:v]{vchain}[v];[0:a]{achain}[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        inputs += [
            "-f", "lavfi", "-t", f"{clip.duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        filter_complex = f"[0:v]{vchain}[v];[1:a]anull[a]"
        maps = ["-map", "[v]", "-map", "[a]"]

    args = inputs + [
        "-filter_complex", filter_complex, *maps,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart", "-t", f"{clip.duration:.3f}", str(seg),
    ]
    if dry_run:
        return " ".join([FFMPEG_BIN, "-y", *args]), clip.duration
    run_ffmpeg(args, timeout=timeout, label=f"segment-{clip.index}")
    return seg, clip.duration


def Image_single_color(width: int, height: int, color: str):
    """Tiny helper: solid-color PNG background."""
    from PIL import Image

    return Image.new("RGB", (width, height), color)


def _compose_video_track(
    segments: List[Path], durations: List[float],
    transitions: List[Tuple[str, float]], *, workdir: Path,
    timeout: float, quality: str, dry_run: bool,
) -> Tuple[Path | str, float]:
    """Join uniform segments — xfade chain when transitions exist, else concat."""
    crf, preset = {"high": ("18", "medium"), "balanced": ("21", "veryfast"),
                   "fast": ("24", "veryfast")}.get(quality, ("21", "veryfast"))
    out = workdir / "master_video.mp4"
    total = sum(durations) - sum(t for _, t in transitions)
    total = max(0.2, total)

    if not transitions:
        # Lossless concat of uniform segments via the concat demuxer.
        listing = workdir / "concat.txt"
        lines = [f"file '{Path(s).as_posix()}'" for s in segments]
        listing.write_text("\n".join(lines) + "\n", "utf-8")
        args = [
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        if dry_run:
            return " ".join([FFMPEG_BIN, "-y", *args]), total
        run_ffmpeg(args, timeout=timeout, label="concat")
        return out, total

    # xfade chain: pair every consecutive segment.
    inputs: List[str] = []
    for seg in segments:
        inputs += ["-i", str(seg)]
    n = len(segments)
    fc_parts: List[str] = []
    vlabel = "0:v"
    alabel = "0:a"
    offset_acc = 0.0
    for k in range(1, n):
        kind, t = transitions[k - 1]
        offset_acc += durations[k - 1] - t
        fc_parts.append(
            f"[{vlabel}][{k}:v]xfade=transition={kind}:duration={t:.3f}:"
            f"offset={offset_acc:.3f}[xv{k}]"
        )
        fc_parts.append(
            f"[{alabel}][{k}:a]acrossfade=d={t:.3f}[xa{k}]"
        )
        vlabel = f"xv{k}"
        alabel = f"xa{k}"
    args = inputs + [
        "-filter_complex", ";".join(fc_parts),
        "-map", f"[{vlabel}]", "-map", f"[{alabel}]",
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    if dry_run:
        return " ".join([FFMPEG_BIN, "-y", *args]), total
    run_ffmpeg(args, timeout=timeout, label="xfade-chain")
    return out, total


def _overlay_pass(
    master: Path, total: float, *, overlays: List[Dict[str, Any]],
    texts: List[Dict[str, Any]], width: int, height: int,
    workdir: Path, timeout: float, quality: str, dry_run: bool,
    font_path: Optional[str] = None,
) -> Tuple[Path | str, float]:
    """Burn overlay images and Pillow-rendered text clips onto the master.

    Each layer becomes one extra ffmpeg input whose stream is faded in/out
    (alpha) at its timeline window, then chained onto the base video with
    the ``overlay`` filter. Text layers are Pillow-rendered PNGs, which is
    what gives the studio full Arabic support and studio-grade typography.
    """
    from openjarvis.creative.text_render import render_text_overlay, save_png

    if not overlays and not texts:
        return master, total

    crf, preset = {"high": ("18", "medium"), "balanced": ("21", "veryfast"),
                   "fast": ("24", "veryfast")}.get(quality, ("21", "veryfast"))
    out = workdir / "overlaid.mp4"
    args: List[str] = ["-i", str(master)]
    chains: List[str] = []
    labels: List[str] = []
    xy_list: List[Tuple[str, str]] = []
    n_inputs = 1  # input 0 is the master video

    def _add_layer(
        *, start: float, duration: float, fade_in: float, fade_out: float,
        position: str, x: Any, y: Any, opacity: float,
        image_path: Optional[Path] = None, video_src: Optional[str] = None,
        scale: Optional[float] = None,
    ) -> None:
        nonlocal n_inputs
        if video_src is not None:
            # Video overlay: fit into canvas on a transparent pad, then
            # shift in time with tpad so it appears exactly at `start`.
            args.extend(["-i", video_src])
            idx = n_inputs
            n_inputs += 1
            chain = (
                f"[{idx}:v]format=rgba,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
                f"tpad=start_duration={start:.3f}:color=transparent"
            )
            if opacity < 1.0:
                chain += f",colorchannelmixer=aa={min(1.0, max(0.05, opacity))}"
        else:
            assert image_path is not None
            dur_needed = max(0.3, start + duration)
            args.extend(["-loop", "1", "-t", f"{dur_needed:.3f}", "-i", str(image_path)])
            idx = n_inputs
            n_inputs += 1
            chain = f"[{idx}:v]format=rgba"
            if scale is not None:
                chain += f",scale=iw*{min(2.0, max(0.02, scale))}:-1"
            if opacity < 1.0:
                chain += f",colorchannelmixer=aa={min(1.0, max(0.05, opacity))}"
        # Alpha fades define the visibility window.
        chain += f",fade=t=in:st={max(0.0, start - 0.001):.3f}:d={max(0.01, fade_in):.3f}:alpha=1"
        if fade_out > 0:
            chain += (
                f",fade=t=out:st={max(0.0, start + duration - fade_out):.3f}"
                f":d={fade_out:.3f}:alpha=1"
            )
        else:
            chain += f",fade=t=out:st={max(0.0, start + duration - 0.01):.3f}:d=0.01:alpha=1"
        label = f"lay{idx}"
        chains.append(f"{chain}[{label}]")
        labels.append(label)
        xy_list.append(_xy_for(position, x, y))

    for ov in overlays:
        src = str(ov.get("src") or "").strip()
        if not src:
            continue
        start = _secs(ov.get("start"), 0.0)
        duration = _secs(ov.get("duration"), 0.0) or total
        fade_in = _secs(ov.get("fade_in"), 0.3)
        fade_out = _secs(ov.get("fade_out"), 0.3)
        opacity = float(ov.get("opacity", 1.0) or 1.0)
        position = str(ov.get("position") or "center")
        if looks_like_image(src):
            _add_layer(
                start=start, duration=duration, fade_in=fade_in,
                fade_out=fade_out, position=position,
                x=ov.get("x"), y=ov.get("y"), opacity=opacity,
                image_path=Path(resolve_media(src)), scale=ov.get("scale"),
            )
        else:
            _add_layer(
                start=start, duration=duration, fade_in=fade_in,
                fade_out=fade_out, position=position,
                x=ov.get("x"), y=ov.get("y"), opacity=opacity,
                video_src=resolve_media(src),
            )

    for tx in texts:
        message = str(tx.get("text") or tx.get("content") or "").strip()
        if not message:
            continue
        start = _secs(tx.get("start"), 0.0)
        duration = _secs(tx.get("duration"), 0.0) or 3.0
        fade_in = _secs(tx.get("fade_in"), 0.35)
        fade_out = _secs(tx.get("fade_out"), 0.35)
        png = render_text_overlay(
            message,
            canvas_size=(width, height),
            style=str(tx.get("style") or "caption"),
            color=tx.get("color"),
            position=str(tx.get("position") or "bottom"),
            align=str(tx.get("align") or "center"),
            font_path=tx.get("font_path", font_path),
            bold=tx.get("bold"),
        )
        png_path = workdir / f"text_{len(chains):03d}.png"
        save_png(png, png_path)
        _add_layer(
            start=start, duration=duration, fade_in=fade_in,
            fade_out=fade_out, position=str(tx.get("position") or "bottom"),
            x=tx.get("x"), y=tx.get("y"),
            opacity=float(tx.get("opacity", 1.0) or 1.0),
            image_path=png_path,
        )

    if not chains:
        return master, total

    filter_lines = list(chains)
    current = "0:v"
    for i, (label, xy) in enumerate(zip(labels, xy_list)):
        nxt = f"ov{i}"
        filter_lines.append(f"[{current}][{label}]overlay={xy[0]}:{xy[1]}[{nxt}]")
        current = nxt
    args += [
        "-filter_complex", ";".join(filter_lines),
        "-map", f"[{current}]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", "-t", f"{total:.3f}", str(out),
    ]
    if dry_run:
        return " ".join([FFMPEG_BIN, "-y", *args]), total
    run_ffmpeg(args, timeout=timeout, label="overlays")
    return out, total


def _audio_pass(
    master: Path, total: float, *, audio_clips: List[Dict[str, Any]],
    workdir: Path, timeout: float, dry_run: bool,
    duck: float = 1.0,
) -> Tuple[Path | str, float]:
    """Mix explicit audio-track clips (music, SFX, voice) at their windows."""
    if not audio_clips:
        return master, total
    out = workdir / "final_audio.mp4"
    args: List[str] = ["-i", str(master)]
    fc: List[str] = []
    mix_labels: List[str] = []
    n_inputs = 1

    for clip in audio_clips:
        src = str(clip.get("src") or "").strip()
        if not src:
            continue
        resolved = resolve_media(src)
        start = _secs(clip.get("start"), 0.0)
        duration = _secs(clip.get("duration"), 0.0)
        volume = max(0.0, float(clip.get("volume", 1.0) or 1.0))
        fade_in = _secs(clip.get("fade_in"), 0.5)
        fade_out = _secs(clip.get("fade_out"), 1.0)
        loop_fill = bool(clip.get("loop_to_fill", clip.get("loop", False)))
        need = (duration or (total - start)) if duration else max(0.5, total - start)
        if loop_fill:
            args.extend(["-stream_loop", "-1", "-t", f"{need:.3f}", "-i", resolved])
        else:
            args.extend(["-i", resolved])
        idx = n_inputs
        n_inputs += 1
        chain = f"[{idx}:a]aloop=loop=-1:size=2e9,atrim=0:{need:.3f}"
        if volume != 1.0:
            chain += f",volume={volume}"
        if fade_in > 0:
            chain += f",afade=t=in:st=0:d={fade_in:.3f}"
        if fade_out > 0:
            chain += f",afade=t=out:st={max(0.0, need - fade_out):.3f}:d={fade_out:.3f}"
        delay_ms = int(start * 1000)
        chain += f",adelay={delay_ms}|{delay_ms}"
        label = f"aud{idx}"
        fc.append(f"{chain}[{label}]")
        mix_labels.append(f"[{label}]")

    if not mix_labels:
        return master, total

    # Keep original audio when present; duck it under narration/music.
    has_audio = True
    try:
        info = probe(str(master))
        has_audio = info["has_audio"]
    except Exception:
        pass
    if has_audio and duck is not None:
        fc.append(f"[0:a]volume={max(0.0, duck)}[orig]")
        mix_labels.insert(0, "[orig]")
    fc.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:normalize=0:dropout_transition=0,"
        f"atrim=0:{total:.3f}[mix]"
    )
    args += [
        "-filter_complex", ";".join(fc),
        "-map", "0:v", "-map", "[mix]",
        "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart", "-t", f"{total:.3f}", str(out),
    ]
    if dry_run:
        return " ".join([FFMPEG_BIN, "-y", *args]), total
    run_ffmpeg(args, timeout=timeout, label="audio-mix")
    return out, total


_EXPORT_PRESETS = {
    # format: (video codec args…, audio codec args…)
    "mp4": (["-c:v", "libx264"], ["-c:a", "aac", "-b:a", "192k"]),
    "webm": (["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32"], ["-c:a", "libopus", "-b:a", "128k"]),
}


def _export_pass(
    src: Path, total: float, *, export: Dict[str, Any], out_path: Path,
    timeout: float, dry_run: bool,
) -> Path | str:
    """Final container/format handling (mp4 default, webm, gif)."""
    fmt = str(export.get("format") or "mp4").lower()
    if fmt not in _EXPORT_PRESETS or fmt == "mp4":
        # mp4 (and unknown formats): move into place with faststart.
        if str(src) != str(out_path):
            if dry_run:
                return f"cp {src} {out_path}"
            shutil.copyfile(src, out_path)
            return out_path
        return out_path
    vcodec, acodec = _EXPORT_PRESETS[fmt]
    args = ["-i", str(src), *vcodec, *acodec, str(out_path)]
    if dry_run:
        return " ".join([FFMPEG_BIN, "-y", *args])
    run_ffmpeg(args, timeout=timeout, label=f"export-{fmt}")
    return out_path


def render_timeline(
    project: Dict[str, Any], out_path: Optional[str] = None, *,
    dry_run: bool = False, timeout: float = DEFAULT_TIMEOUT,
    keep_workdir: bool = False,
) -> Dict[str, Any]:
    """Render a declarative timeline project into a finished video.

    Project schema (JSON-friendly — the agent composes this directly)::

        {
          "width": 1920, "height": 1080, "fps": 30,
          "tracks": [
            {"type": "video", "clips": [
                {"src": "clip.mp4", "duration": 4, "in": 1.5,
                 "fit": "cover",
                 "effects": [{"type": "brightness", "amount": 0.1}],
                 "kenburns": {"zoom_to": 1.2, "direction": "left"},
                 "speed": 1.0, "fade_out": 0.5,
                 "transition_out": {"type": "circleopen", "duration": 0.8}},
                {"kind": "gradient", "style": "dark", "duration": 2,
                 "text": ... }
            ]},
            {"type": "overlay", "clips": [
                {"src": "logo.png", "position": "top-right", "scale": 0.12,
                 "opacity": 0.9, "start": 0, "duration": 10}]
            },
            {"type": "text", "clips": [
                {"text": "Hello", "style": "title", "position": "center",
                 "start": 0.5, "duration": 2.5, "fade_in": 0.4}]
            },
            {"type": "audio", "clips": [
                {"src": "music.mp3", "volume": 0.25, "loop_to_fill": true,
                 "fade_out": 2}]
            }
          ],
          "export": {"format": "mp4", "quality": "high"}
        }

    Returns a result dict with the output path, URL, duration and a
    thumbnail path. With ``dry_run=True`` the planned ffmpeg commands are
    returned instead of executing (agent transparency + cheap validation).
    """
    if not isinstance(project, dict):
        raise FFmpegError("project must be a dict")
    tracks = project.get("tracks") or []
    if not tracks:
        raise FFmpegError("project has no tracks")

    width, height, fps = _canvas_of(project)
    export = dict(project.get("export") or {})
    quality = str(export.get("quality") or "balanced").lower()
    format_ = str(export.get("format") or "mp4").lower()
    workdir = _paths.tmp_workdir()
    commands: List[str] = []

    video_clips: List[Dict[str, Any]] = []
    overlay_clips: List[Dict[str, Any]] = []
    text_clips: List[Dict[str, Any]] = []
    audio_clips: List[Dict[str, Any]] = []

    for track in tracks:
        ttype = str((track or {}).get("type") or "").lower()
        clips = (track or {}).get("clips") or []
        if ttype in ("", "video", "main"):
            video_clips.extend(clips)
        elif ttype in ("overlay", "image"):
            overlay_clips.extend(clips)
        elif ttype == "text":
            text_clips.extend(clips)
        elif ttype == "audio":
            audio_clips.extend(clips)
        else:
            raise FFmpegError(f"unknown track type: {ttype}")

    try:
        # ---- 1) Normalize video-track clips into uniform segments ------
        specs: List[_ClipSpec] = []
        for i, raw in enumerate(video_clips):
            specs.append(_parse_clip(i, raw))
        if not specs:
            # Text/overlay over a generated gradient canvas.
            specs.append(_ClipSpec(index=0, kind="gradient", duration=5.0))

        segments: List[Path | str] = []
        durations: List[float] = []
        for spec in specs:
            seg, dur = _build_segment(
                spec, width=width, height=height, fps=fps, quality=quality,
                workdir=workdir, timeout=timeout, dry_run=dry_run,
            )
            if dry_run and isinstance(seg, str):
                commands.append(seg)
            segments.append(seg)  # type: ignore[arg-type]
            durations.append(dur)

        transitions: List[Tuple[str, float]] = []
        for i in range(len(specs) - 1):
            tr_raw = specs[i].transition_out
            if not tr_raw:
                transitions.append(("fade", 0.0))
                continue
            transitions.append(_normalize_transition(specs[i], specs[i + 1]))
        has_real_transition = any(t > 0 for _, t in transitions)
        if not has_real_transition:
            transitions = []

        # ---- 2) Compose the video track ---------------------------------
        master, total = _compose_video_track(
            [Path(s) for s in segments], durations, transitions,
            workdir=workdir, timeout=timeout, quality=quality, dry_run=dry_run,
        )
        if dry_run and isinstance(master, str):
            commands.append(master)

        # ---- 3) Overlays + text -----------------------------------------
        overlaid, total = _overlay_pass(
            Path(master) if not isinstance(master, str) else master, total,
            overlays=overlay_clips, texts=text_clips,
            width=width, height=height, workdir=workdir,
            timeout=timeout, quality=quality, dry_run=dry_run,
            font_path=project.get("font_path"),
        )
        if dry_run and isinstance(overlaid, str):
            commands.append(overlaid)

        # ---- 4) Audio mix -------------------------------------------------
        ducked = float(project.get("duck_original", 0.9))
        mixed, total = _audio_pass(
            Path(overlaid) if not isinstance(overlaid, str) else overlaid, total,
            audio_clips=audio_clips, workdir=workdir, timeout=timeout,
            dry_run=dry_run, duck=ducked,
        )
        if dry_run and isinstance(mixed, str):
            commands.append(mixed)

        # ---- 5) Export -----------------------------------------------------
        out = Path(out_path) if out_path else _paths.new_media_path(
            "video", format_ if format_ in ("mp4", "webm") else "mp4",
            stem=str(project.get("name") or "timeline")[:40],
        )
        final = _export_pass(
            Path(mixed) if not isinstance(mixed, str) else mixed, total,
            export={"format": format_, "quality": quality},
            out_path=out, timeout=timeout, dry_run=dry_run,
        )
        if dry_run:
            commands.append(final if isinstance(final, str) else str(final))
            return {
                "dry_run": True,
                "commands": commands,
                "output": str(out),
                "estimated_duration": round(total, 3),
                "canvas": f"{width}x{height}",
                "fps": fps,
            }

        # ---- 6) Thumbnail + result -----------------------------------------
        thumb: Optional[Path] = None
        try:
            editor = SimpleEditor(timeout=60)
            thumb = Path(editor.thumbnail(str(out)))  # type: ignore[arg-type]
        except Exception as exc:
            logger.debug("thumbnail failed: %s", exc)
        info = probe(str(out))
        return {
            "path": str(out),
            "url": _paths.media_url(out),
            "thumbnail": str(thumb) if thumb else None,
            "thumbnail_url": _paths.media_url(thumb) if thumb else None,
            "duration": round(info["duration"] or total, 3),
            "width": info["width"] or width,
            "height": info["height"] or height,
            "fps": info["fps"] or fps,
            "size_bytes": out.stat().st_size,
            "canvas": f"{width}x{height}",
            "tracks": {
                "video": len(specs),
                "overlay": len(overlay_clips),
                "text": len(text_clips),
                "audio": len(audio_clips),
            },
            "workdir": str(workdir) if keep_workdir else None,
        }
    finally:
        if not keep_workdir and not dry_run:
            shutil.rmtree(workdir, ignore_errors=True)


__all__ = [
    "FFmpegError",
    "probe",
    "run_ffmpeg",
    "resolve_media",
    "SimpleEditor",
    "render_timeline",
]

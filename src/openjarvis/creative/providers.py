"""Generation providers — text-to-image, image-to-image, text-to-video.

Backed by the flexible ``media_settings`` store so the user picks providers
and models from the settings UI / API. Two provider modes are supported:

* ``openai_images`` / ``openai_videos`` — OpenAI-compatible endpoints
  (NVIDIA NIM hosted at ai.api.nvidia.com, any self-hosted NIM container,
  OpenAI itself, …). Video uses the async job + polling protocol.
* ``custom_http`` — fully templated request/poll definitions for arbitrary
  REST providers (Replicate-style, fal.ai-style, in-house gateways…).

No browser, no UI — pure HTTP from the agent's tool call.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from openjarvis.creative import _paths, media_settings

logger = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """Raised when a generation provider fails."""


_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


def _headers_for(cfg: Dict[str, Any], provider_name: str, *, accept: str = "application/json") -> Dict[str, str]:
    headers = {"Accept": accept, "Content-Type": "application/json"}
    api_key = media_settings.resolve_api_key(cfg, provider_name)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _dotted_get(data: Any, path: str) -> Any:
    """Fetch a nested value via a dotted path ('data.0.b64_json')."""
    if not path:
        return None
    current: Any = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _template_substitute(value: Any, mapping: Dict[str, str]) -> Any:
    """Recursively substitute ``{key}`` placeholders in strings/dicts/lists."""
    if isinstance(value, str):
        out = value
        for key, val in mapping.items():
            out = out.replace("{" + key + "}", val)
        return out
    if isinstance(value, dict):
        return {k: _template_substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_template_substitute(v, mapping) for v in value]
    return value


def _save_image_bytes(data: bytes, *, stem: str) -> Path:
    """Save raw image bytes, sniffing the format (png/jpg/webp)."""
    ext = "png"
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
    path = _paths.new_media_path("image", ext, stem=stem)
    path.write_bytes(data)
    return path


def _download(url: str, headers: Dict[str, str], *, stem: str, kind: str) -> Path:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPError as exc:
        raise GenerationError(f"download failed ({url}): {exc}") from exc
    if kind == "image":
        return _save_image_bytes(data, stem=stem)
    ext = "mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        ext = "webm"
    path = _paths.new_media_path("video", ext, stem=stem)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------


def generate_image(
    prompt: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    width: int = 1024,
    height: int = 1024,
    n: int = 1,
    extra: Optional[Dict[str, Any]] = None,
    seed: Optional[int] = None,
    stem: str = "generated",
) -> Dict[str, Any]:
    """Generate one or more images from a text prompt.

    Returns ``{"images": [{"path", "url", "size"}...], "provider", "model"}``.
    """
    if not prompt or not prompt.strip():
        raise GenerationError("prompt is required")
    name, cfg = media_settings.get_provider("image", provider)
    model = model or cfg.get("model") or ""
    mode = str(cfg.get("mode") or "openai_images")
    base_url = str(cfg.get("base_url") or "").rstrip("/")

    if mode == "openai_images":
        return _generate_image_openai(
            prompt, name=name, cfg=cfg, model=model, width=width,
            height=height, n=n, extra=extra, seed=seed, stem=stem,
        )
    if mode == "custom_http":
        return _generate_image_custom(
            prompt, name=name, cfg=cfg, model=model, width=width,
            height=height, n=n, extra=extra, seed=seed, stem=stem,
        )
    raise GenerationError(f"unsupported image provider mode: {mode}")


def _generate_image_openai(
    prompt: str, *, name: str, cfg: Dict[str, Any], model: str,
    width: int, height: int, n: int, extra: Optional[Dict[str, Any]],
    seed: Optional[int], stem: str,
) -> Dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise GenerationError(f"provider '{name}' has no base_url configured")
    body: Dict[str, Any] = {
        "prompt": prompt,
        "n": max(1, min(n, 4)),
        "model": model or None,
        "size": f"{int(width)}x{int(height)}",
    }
    body = {k: v for k, v in body.items() if v is not None}
    if seed is not None:
        body["seed"] = int(seed)
    merged_extra = {**(cfg.get("extra") or {}), **(extra or {})}
    body.update(merged_extra)

    url = f"{base_url}/images/generations"
    headers = _headers_for(cfg, name)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GenerationError(f"image generation request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GenerationError(
            f"image generation HTTP {resp.status_code}: {resp.text[:500]}"
        )
    payload = resp.json()

    images: List[Dict[str, Any]] = []
    data = payload.get("data") or payload.get("images") or []
    if not data:
        raise GenerationError(f"no image data in response: {str(payload)[:300]}")
    for item in data[: max(1, min(n, 4))]:
        b64 = item.get("b64_json")
        if b64:
            path = _save_image_bytes(base64.b64decode(b64), stem=stem)
        elif item.get("url"):
            path = _download(
                item["url"],
                {k: v for k, v in headers.items() if k != "Content-Type"},
                stem=stem, kind="image",
            )
        else:
            raise GenerationError("response item has neither b64_json nor url")
        images.append({
            "path": str(path),
            "url": _paths.media_url(path),
            "size": f"{width}x{height}",
        })
    return {"provider": name, "model": model, "images": images}


def _generate_image_custom(
    prompt: str, *, name: str, cfg: Dict[str, Any], model: str,
    width: int, height: int, n: int, extra: Optional[Dict[str, Any]],
    seed: Optional[int], stem: str,
) -> Dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    request = cfg.get("request") or {}
    mapping = {
        "prompt": prompt,
        "width": str(int(width)),
        "height": str(int(height)),
        "model": model or "",
        "seed": str(int(seed) if seed is not None else 0),
    }
    method = str(request.get("method") or "POST").upper()
    path_tmpl = str(request.get("path") or "")
    url = base_url + path_tmpl
    headers = _headers_for(cfg, name)
    headers.update(_template_substitute(request.get("headers") or {}, mapping))
    body = _template_substitute(request.get("body") or {"prompt": "{prompt}"}, mapping)
    if extra:
        if isinstance(body, dict):
            body.update(extra)

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            if method == "GET":
                resp = client.get(url, headers=headers, params=body if isinstance(body, dict) else None)
            else:
                fmt = "json" if isinstance(body, (dict, list)) else "text"
                resp = client.request(method, url, json=body if fmt == "json" else None,
                                      content=body if fmt == "text" else None,
                                      headers=headers)
    except httpx.HTTPError as exc:
        raise GenerationError(f"custom image request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GenerationError(f"custom image HTTP {resp.status_code}: {resp.text[:500]}")

    kind = str(request.get("response_image_kind") or "b64")
    if kind == "raw":
        path = _save_image_bytes(resp.content, stem=stem)
        return {"provider": name, "model": model,
                "images": [{"path": str(path), "url": _paths.media_url(path),
                            "size": f"{width}x{height}"}]}
    payload = resp.json()
    value = _dotted_get(payload, str(request.get("response_image_path") or ""))
    if value is None:
        raise GenerationError(
            f"image not found at '{request.get('response_image_path')}': {str(payload)[:300]}"
        )
    values = value if isinstance(value, list) else [value]
    images = []
    for item in values[: max(1, min(n, 4))]:
        if kind == "b64":
            path = _save_image_bytes(base64.b64decode(item), stem=stem)
        else:
            path = _download(str(item), {k: v for k, v in headers.items()
                                         if k != "Content-Type"},
                             stem=stem, kind="image")
        images.append({"path": str(path), "url": _paths.media_url(path),
                       "size": f"{width}x{height}"})
    return {"provider": name, "model": model, "images": images}


# ---------------------------------------------------------------------------
# Video generation (async job + polling)
# ---------------------------------------------------------------------------


def generate_video(
    prompt: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    image: Optional[str] = None,
    duration: Optional[float] = None,
    aspect_ratio: str = "16:9",
    extra: Optional[Dict[str, Any]] = None,
    poll_interval: Optional[float] = None,
    poll_timeout: Optional[float] = None,
    stem: str = "generated",
    on_status=None,
) -> Dict[str, Any]:
    """Generate a video from text (and optionally a starting image).

    Handles both synchronous JSON responses and async job protocols with
    polling. ``on_status`` (callable) receives progress updates.
    """
    if not prompt or not prompt.strip():
        raise GenerationError("prompt is required")
    name, cfg = media_settings.get_provider("video", provider)
    model = model or cfg.get("model") or ""
    mode = str(cfg.get("mode") or "openai_videos")

    if mode == "openai_videos":
        return _generate_video_openai(
            prompt, name=name, cfg=cfg, model=model, image=image,
            duration=duration, aspect_ratio=aspect_ratio, extra=extra,
            poll_interval=poll_interval, poll_timeout=poll_timeout,
            stem=stem, on_status=on_status,
        )
    if mode == "custom_http":
        return _generate_video_custom(
            prompt, name=name, cfg=cfg, model=model, image=image,
            duration=duration, aspect_ratio=aspect_ratio, extra=extra,
            poll_interval=poll_interval, poll_timeout=poll_timeout,
            stem=stem, on_status=on_status,
        )
    raise GenerationError(f"unsupported video provider mode: {mode}")


def _notify(on_status, message: str) -> None:
    if callable(on_status):
        try:
            on_status(message)
        except Exception:  # never let logging break generation
            pass


def _generate_video_openai(
    prompt: str, *, name: str, cfg: Dict[str, Any], model: str,
    image: Optional[str], duration: Optional[float], aspect_ratio: str,
    extra: Optional[Dict[str, Any]], poll_interval: Optional[float],
    poll_timeout: Optional[float], stem: str, on_status,
) -> Dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise GenerationError(f"provider '{name}' has no base_url configured")
    headers = _headers_for(cfg, name)
    body: Dict[str, Any] = {
        "prompt": prompt,
        "model": model or None,
        "aspect_ratio": aspect_ratio,
    }
    if duration:
        body["seconds"] = float(duration)
    if image:
        image_path = Path(image).expanduser()
        if image_path.exists():
            body["image"] = "data:image/png;base64," + base64.b64encode(
                image_path.read_bytes()
            ).decode("ascii")
        else:
            raise GenerationError(f"image-to-video source not found: {image}")
    body = {k: v for k, v in body.items() if v is not None}
    merged_extra = {**(cfg.get("extra") or {}), **(extra or {})}
    body.update(merged_extra)

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(f"{base_url}/videos/generations",
                               json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GenerationError(f"video request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GenerationError(f"video HTTP {resp.status_code}: {resp.text[:500]}")

    payload = resp.json()
    job_id = payload.get("id") or payload.get("job_id") or ""
    # Synchronous completion (some providers return content immediately).
    direct = _extract_video_result(payload)
    if direct:
        return _finalize_video(direct, headers, name=name, model=model, stem=stem)
    if not job_id:
        raise GenerationError(f"no job id in response: {str(payload)[:300]}")

    interval = float(poll_interval or cfg.get("poll_interval_seconds") or 10)
    timeout = float(poll_timeout or cfg.get("poll_timeout_seconds") or 900)
    deadline = time.time() + timeout
    _notify(on_status, f"video job {job_id} submitted — polling every {interval:.0f}s")
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        while time.time() < deadline:
            time.sleep(interval)
            try:
                poll = client.get(
                    f"{base_url}/videos/generations/{job_id}", headers=headers
                )
            except httpx.HTTPError as exc:
                logger.debug("poll transient error: %s", exc)
                continue
            if poll.status_code >= 500:
                continue
            if poll.status_code >= 400:
                raise GenerationError(
                    f"polling HTTP {poll.status_code}: {poll.text[:300]}"
                )
            state = poll.json()
            status = str(
                state.get("status") or state.get("state") or ""
            ).lower()
            result = _extract_video_result(state)
            if result:
                _notify(on_status, f"video job {job_id} completed")
                return _finalize_video(result, headers, name=name, model=model,
                                       stem=stem)
            if status in ("failed", "error", "cancelled", "canceled"):
                error = state.get("error") or state.get("message") or state
                raise GenerationError(f"video job failed: {str(error)[:300]}")
            _notify(on_status, f"job {job_id}: {status or 'in progress'}")
    raise GenerationError(f"video job {job_id} timed out after {timeout:.0f}s")


def _extract_video_result(payload: Dict[str, Any]) -> Optional[str]:
    """Locate a downloadable video URL in a provider response."""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("video_url"), payload.get("output_url"), payload.get("url"),
        payload.get("download_url"),
        (payload.get("result") or {}).get("url") if isinstance(payload.get("result"), dict) else None,
        (payload.get("content") or {}).get("url") if isinstance(payload.get("content"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


def _finalize_video(url: str, headers: Dict[str, str], *, name: str, model: str,
                    stem: str) -> Dict[str, Any]:
    path = _download(url, headers, stem=stem, kind="video")
    return {
        "provider": name,
        "model": model,
        "video": {"path": str(path), "url": _paths.media_url(path)},
    }


def _generate_video_custom(
    prompt: str, *, name: str, cfg: Dict[str, Any], model: str,
    image: Optional[str], duration: Optional[float], aspect_ratio: str,
    extra: Optional[Dict[str, Any]], poll_interval: Optional[float],
    poll_timeout: Optional[float], stem: str, on_status,
) -> Dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    request = cfg.get("request") or {}
    poll_cfg = request.get("poll") or {}
    mapping = {
        "prompt": prompt,
        "model": model or "",
        "duration": str(float(duration or 5)),
        "aspect_ratio": aspect_ratio,
    }
    method = str(request.get("method") or "POST").upper()
    url = base_url + str(request.get("path") or "")
    headers = _headers_for(cfg, name)
    headers.update(_template_substitute(request.get("headers") or {}, mapping))
    body = _template_substitute(request.get("body") or {"prompt": "{prompt}"}, mapping)
    if image:
        image_path = Path(image).expanduser()
        if image_path.exists():
            if isinstance(body, dict):
                body["image"] = base64.b64encode(image_path.read_bytes()).decode("ascii")
    if extra and isinstance(body, dict):
        body.update(extra)

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            else:
                resp = client.request(method, url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GenerationError(f"custom video request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise GenerationError(f"custom video HTTP {resp.status_code}: {resp.text[:500]}")

    # Immediate result?
    direct = _dotted_get(resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {},
                         str(poll_cfg.get("video_path") or ""))
    if isinstance(direct, str) and direct.startswith("http"):
        return _finalize_video(direct, headers, name=name, model=model, stem=stem)

    job_id = _dotted_get(resp.json(), str(request.get("response_job_id_path") or "id"))
    if not job_id:
        raise GenerationError(f"no job id in custom video response: {resp.text[:300]}")

    interval = float(poll_interval or cfg.get("poll_interval_seconds") or 10)
    timeout = float(poll_timeout or cfg.get("poll_timeout_seconds") or 1800)
    deadline = time.time() + timeout
    poll_method = str(poll_cfg.get("method") or "GET").upper()
    poll_url_tmpl = base_url + str(poll_cfg.get("path") or "{job_id}")
    poll_headers = _headers_for(cfg, name)
    poll_headers.update(_template_substitute(poll_cfg.get("headers") or {}, mapping))
    _notify(on_status, f"custom video job {job_id} submitted")

    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        while time.time() < deadline:
            time.sleep(interval)
            poll_url = poll_url_tmpl.replace("{job_id}", str(job_id))
            try:
                if poll_method == "GET":
                    poll = client.get(poll_url, headers=poll_headers)
                else:
                    poll = client.request(poll_method, poll_url, headers=poll_headers)
            except httpx.HTTPError as exc:
                logger.debug("poll transient error: %s", exc)
                continue
            if poll.status_code >= 500:
                continue
            if poll.status_code >= 400:
                raise GenerationError(f"poll HTTP {poll.status_code}: {poll.text[:300]}")
            try:
                state = poll.json()
            except ValueError:
                continue
            status = str(_dotted_get(state, str(poll_cfg.get("status_path") or "status")) or "").lower()
            video = _dotted_get(state, str(poll_cfg.get("video_path") or ""))
            if isinstance(video, str) and video.startswith("http"):
                _notify(on_status, f"job {job_id} completed")
                return _finalize_video(video, poll_headers, name=name, model=model, stem=stem)
            if status in ("failed", "error", "cancelled", "canceled"):
                raise GenerationError(f"video job failed: {str(state)[:300]}")
            _notify(on_status, f"job {job_id}: {status or 'in progress'}")
    raise GenerationError(f"custom video job {job_id} timed out after {timeout:.0f}s")


__all__ = [
    "GenerationError",
    "generate_image",
    "generate_video",
]

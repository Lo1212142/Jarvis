"""Flexible media-provider settings for the creative suite.

Self-contained, additive settings store — mirrors the pattern of
``server/settings_routes.py`` (runtime-settings.json) without touching it:

* ``~/.openjarvis/media-settings.json`` — non-secret provider configuration
  (default provider, model, base URL, request templates …).
* ``~/.openjarvis/media-keys.json`` — API keys only, chmod 600, never
  returned in clear text by the API (masked status only).

Resolution order for an API key: media-keys.json → environment variable
(``NIM_API_KEY`` or ``<PROVIDER>_API_KEY``).
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.paths import get_config_dir

_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

IMAGE_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "nvidia_nim": {
        "label": "NVIDIA NIM (Visual GenAI)",
        "mode": "openai_images",
        "base_url": "https://ai.api.nvidia.com/v1",
        "model": "black-forest-labs/flux.1-schnell",
        "api_key_env": "NIM_API_KEY",
        "models": [
            "black-forest-labs/flux.1-schnell",
            "black-forest-labs/flux.1-dev",
            "stabilityai/stable-diffusion-3.5-large",
            "qwen/qwen-image",
            "black-forest-labs/flux.1-kontext-dev",
            "qwen/qwen-image-edit",
        ],
        "extra": {"steps": 4, "cfg_scale": 0.0, "aspect_ratio": "16:9"},
    },
    "openai": {
        "label": "OpenAI Images",
        "mode": "openai_images",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-image-1",
        "api_key_env": "OPENAI_API_KEY",
        "models": ["gpt-image-1", "dall-e-3"],
        "extra": {},
    },
    "custom_http": {
        "label": "Custom HTTP endpoint",
        "mode": "custom_http",
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "models": [],
        # Request template for full flexibility (any REST provider):
        # {prompt}, {width}, {height}, {aspect_ratio}, {model} are substituted.
        "request": {
            "method": "POST",
            "path": "",
            "headers": {"Content-Type": "application/json"},
            "body": {"prompt": "{prompt}"},
            "response_image_path": "data.0.b64_json",
            "response_image_kind": "b64",
        },
    },
}

VIDEO_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "nvidia_nim": {
        "label": "NVIDIA NIM (Wan2.2)",
        "mode": "openai_videos",
        "base_url": "https://ai.api.nvidia.com/v1",
        "model": "wan-video/wan2.2-t2v",
        "api_key_env": "NIM_API_KEY",
        "models": [
            "wan-video/wan2.2-t2v",
            "wan-video/wan2.2-i2v",
        ],
        "extra": {"duration": 5, "aspect_ratio": "16:9"},
        "poll_interval_seconds": 10,
        "poll_timeout_seconds": 900,
    },
    "custom_http": {
        "label": "Custom HTTP endpoint",
        "mode": "custom_http",
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "models": [],
        "request": {
            "method": "POST",
            "path": "",
            "headers": {"Content-Type": "application/json"},
            "body": {"prompt": "{prompt}"},
            # Async providers: where to read the job id, how to poll,
            # where the final video URL/b64 lives.
            "response_job_id_path": "id",
            "poll": {
                "method": "GET",
                "path": "{job_id}",
                "headers": {},
                "status_path": "status",
                "done_value": "completed",
                "error_value": "failed",
                "video_path": "video_url",
            },
        },
        "poll_interval_seconds": 10,
        "poll_timeout_seconds": 1800,
    },
}

EDITING_DEFAULTS: Dict[str, Any] = {
    "engine": "ffmpeg",
    "canvas": "1920x1080",
    "fps": 30,
    "quality": "high",
}

SHODAN_DEFAULTS: Dict[str, Any] = {
    "api_base": "https://api.shodan.io",
    "internetdb_base": "https://internetdb.shodan.io",
    "internetdb_legacy_prefix": "/api",
    "timeout_s": 25,
    "cache_ttl_s": 600,
    "max_lookup_ips": 10,
    "max_search_results": 20,
    "max_banners": 8,
}

GEOINT_DEFAULTS: Dict[str, Any] = {
    "overpass_endpoints": [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ],
    "overpass_timeout_s": 60,
    "cache_ttl_s": 600,
    "odata_base": "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
    "identity_token_url": (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
        "protocol/openid-connect/token"),
    "gibs_wms": "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi",
    "max_features_per_category": 15,
}

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "image_generation": {"default_provider": "nvidia_nim", "providers": IMAGE_PROVIDERS},
    "video_generation": {"default_provider": "nvidia_nim", "providers": VIDEO_PROVIDERS},
    "editing": dict(EDITING_DEFAULTS),
    "geoint": dict(GEOINT_DEFAULTS),
    "shodan": dict(SHODAN_DEFAULTS),
}

_SETTINGS_SECTIONS = (
    "image_generation", "video_generation", "editing", "geoint",
    "shodan",
)

_SECRET_MASK = "••••••••"


# ---------------------------------------------------------------------------
# Settings store
# ---------------------------------------------------------------------------


def _settings_path() -> Path:
    return get_config_dir() / "media-settings.json"


def _keys_path() -> Path:
    return get_config_dir() / "media-keys.json"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> Dict[str, Any]:
    """Load media settings merged over defaults (never raises)."""
    with _LOCK:
        data: Dict[str, Any] = {}
        path = _settings_path()
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                data = {}
        merged = _deep_merge(_DEFAULT_SETTINGS, data if isinstance(data, dict) else {})
        return merged


def save_settings_section(section: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *patch* into one top-level section and persist atomically."""
    if section not in _SETTINGS_SECTIONS:
        raise ValueError(f"Unknown settings section: {section}")
    with _LOCK:
        current = {}
        path = _settings_path()
        if path.exists():
            try:
                current = json.loads(path.read_text("utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                current = {}
        if not isinstance(current, dict):
            current = {}
        merged_section = _deep_merge(current.get(section, {}), patch)
        # Validate provider references.
        if section in ("image_generation", "video_generation"):
            default = merged_section.get("default_provider", "")
            providers = merged_section.get("providers", {})
            if default and default not in providers:
                raise ValueError(
                    f"default_provider '{default}' is not defined in providers"
                )
        current[section] = merged_section
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(path)
        return load_settings()


def reset_settings_section(section: str) -> Dict[str, Any]:
    """Restore one section to factory defaults."""
    if section not in _SETTINGS_SECTIONS:
        raise ValueError(f"Unknown settings section: {section}")
    with _LOCK:
        current: Dict[str, Any] = {}
        path = _settings_path()
        if path.exists():
            try:
                current = json.loads(path.read_text("utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                current = {}
        current.pop(section, None)
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False), "utf-8")
        return load_settings()


# ---------------------------------------------------------------------------
# API keys (secret store)
# ---------------------------------------------------------------------------


def _load_keys() -> Dict[str, str]:
    path = _keys_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8") or "{}")
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_api_key(provider: str, api_key: str) -> None:
    """Persist an API key for *provider* with tight file permissions."""
    provider = (provider or "").strip()
    api_key = (api_key or "").strip()
    if not provider:
        raise ValueError("provider name is required")
    with _LOCK:
        keys = _load_keys()
        if api_key:
            keys[provider] = api_key
        else:
            keys.pop(provider, None)
        path = _keys_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(keys, indent=2), "utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 before rename
        tmp.replace(path)


def resolve_api_key(provider_cfg: Dict[str, Any], provider_name: str = "") -> str:
    """Resolve an API key: media-keys.json → environment → empty string."""
    env_name = (provider_cfg.get("api_key_env") or "").strip()
    if env_name and os.environ.get(env_name, "").strip():
        return os.environ[env_name].strip()
    keys = _load_keys()
    for candidate in (provider_name, env_name, env_name.lower()):
        if candidate and keys.get(candidate):
            return keys[candidate]
    return ""


def key_status() -> Dict[str, Any]:
    """Masked key status per provider (never exposes raw values)."""
    env_map = {
        "image_generation": "image",
        "video_generation": "video",
    }
    settings = load_settings()
    keys = _load_keys()
    status: Dict[str, Any] = {}
    for section in ("image_generation", "video_generation"):
        providers = settings.get(section, {}).get("providers", {})
        env_kind = env_map.get(section, section)
        entry: Dict[str, Any] = {}
        for name, cfg in providers.items():
            env_name = (cfg.get("api_key_env") or "").strip()
            from_env = bool(env_name and os.environ.get(env_name, "").strip())
            from_file = bool(keys.get(name) or keys.get(env_name))
            entry[name] = {
                "configured": from_env or from_file,
                "source": "env" if from_env else ("file" if from_file else "none"),
                "masked": _SECRET_MASK if (from_env or from_file) else "",
                "api_key_env": env_name,
            }
        status[env_kind] = entry
    return status


# ---------------------------------------------------------------------------
# Provider resolution helpers
# ---------------------------------------------------------------------------


def get_provider(
    capability: str, provider_name: Optional[str] = None
) -> tuple[str, Dict[str, Any]]:
    """Return ``(name, config)`` for a generation capability.

    ``capability`` is ``image`` or ``video``. Falls back to the configured
    default provider and finally to ``nvidia_nim``.
    """
    section = f"{capability}_generation"
    settings = load_settings()
    gen_cfg = settings.get(section, {})
    providers: Dict[str, Any] = gen_cfg.get("providers", {})
    name = (provider_name or gen_cfg.get("default_provider") or "").strip()
    if not name or name not in providers:
        name = "nvidia_nim" if "nvidia_nim" in providers else next(iter(providers), "")
    cfg = providers.get(name, {})
    return name, dict(cfg)


def editing_settings() -> Dict[str, Any]:
    return load_settings().get("editing", dict(EDITING_DEFAULTS))


def public_settings() -> Dict[str, Any]:
    """Settings view safe for the API (no secrets — keys are masked)."""
    settings = load_settings()
    return settings


__all__ = [
    "load_settings",
    "save_settings_section",
    "reset_settings_section",
    "save_api_key",
    "resolve_api_key",
    "key_status",
    "get_provider",
    "editing_settings",
    "public_settings",
    "IMAGE_PROVIDERS",
    "VIDEO_PROVIDERS",
]

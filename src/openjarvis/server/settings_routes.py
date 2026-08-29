"""Safe runtime settings API for the server dashboard.

Secrets are deliberately excluded. Provider credentials stay in the server
secret store/environment and this API only exposes masked status flags.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.core.paths import get_config_dir
from openjarvis.self_development.capability_catalog import capabilities_for_catalog


class RuntimeSettingsPatch(BaseModel):
    llm_provider: str | None = Field(default=None, max_length=64)
    llm_model: str | None = Field(default=None, max_length=256)
    nim_rpm_limit: int | None = Field(default=None, ge=1, le=40)
    speech_stt_backend: str | None = Field(default=None, max_length=64)
    speech_tts_backend: str | None = Field(default=None, max_length=64)
    wake_word_enabled: bool | None = None
    wake_words: list[str] | None = None
    wake_word_language: str | None = Field(default=None, pattern="^(auto|ar|en|multi)$")
    wake_word_sensitivity: float | None = Field(default=None, ge=0.0, le=1.0)
    push_to_talk_enabled: bool | None = None
    audio_hot_window_seconds: int | None = Field(default=None, ge=1, le=60)
    telegram_enabled: bool | None = None
    telegram_allowed_chat_ids: list[str] | None = None
    proactive_enabled: bool | None = None
    proactive_voice_enabled: bool | None = None
    proactive_quiet_start: str | None = Field(default=None, max_length=5)
    proactive_quiet_end: str | None = Field(default=None, max_length=5)
    proactive_cooldown_minutes: int | None = Field(default=None, ge=0, le=10080)
    proactive_daily_cap: int | None = Field(default=None, ge=0, le=1000)
    humor_enabled: bool | None = None
    humor_style: str | None = Field(default=None, max_length=32)
    sarcasm_level: str | None = Field(default=None, max_length=16)
    predictive_suggestions: bool | None = None
    max_predictive_suggestions: int | None = Field(default=None, ge=0, le=5)
    browser_enabled: bool | None = None
    browser_profile: str | None = Field(default=None, max_length=128)
    sandbox_enabled: bool | None = None
    sandbox_network: bool | None = None
    sandbox_memory_mb: int | None = Field(default=None, ge=64, le=16384)
    sandbox_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    resource_monitor_enabled: bool | None = None
    resource_poll_interval_seconds: float | None = Field(default=None, ge=1.0, le=3600.0)
    resource_cpu_alert_percent: float | None = Field(default=None, ge=1.0, le=1000.0)
    resource_memory_alert_percent: float | None = Field(default=None, ge=1.0, le=100.0)
    resource_alert_cooldown_seconds: float | None = Field(default=None, ge=0.0, le=86400.0)
    audio_playback_enabled: bool | None = None
    audio_allowed_roots: list[str] | None = None
    audio_default_client_id: str | None = Field(default=None, max_length=128)
    weather_enabled: bool | None = None
    weather_cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    weather_stale_after_seconds: float | None = Field(default=None, ge=30.0, le=604800.0)
    conflict_news_enabled: bool | None = None
    conflict_news_cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    conflict_news_stale_after_seconds: float | None = Field(default=None, ge=30.0, le=604800.0)
    conflict_news_max_items: int | None = Field(default=None, ge=1, le=100)
    media_catalog_enabled: bool | None = None
    media_catalog_cache_ttl_seconds: float | None = Field(default=None, ge=30.0, le=86400.0)
    media_catalog_max_items: int | None = Field(default=None, ge=1, le=50)
    vision_enabled: bool | None = None


router = APIRouter(prefix="/api/settings", tags=["settings"])

_DEFAULTS: dict[str, Any] = {
    "llm_provider": "nim",
    "llm_model": "",
    "nim_rpm_limit": 40,
    "speech_stt_backend": "openai",
    "speech_tts_backend": "openai",
    "wake_word_enabled": True,
    "wake_words": ["jarvis", "يا جارفيس"],
    "wake_word_language": "multi",
    "wake_word_sensitivity": 0.75,
    "push_to_talk_enabled": True,
    "audio_hot_window_seconds": 8,
    "telegram_enabled": False,
    "telegram_allowed_chat_ids": [],
    "proactive_enabled": False,
    "proactive_voice_enabled": False,
    "proactive_quiet_start": "22:00",
    "proactive_quiet_end": "08:00",
    "proactive_cooldown_minutes": 120,
    "proactive_daily_cap": 5,
    "humor_enabled": True,
    "humor_style": "dry",
    "sarcasm_level": "light",
    "predictive_suggestions": True,
    "max_predictive_suggestions": 2,
    "browser_enabled": False,
    "browser_profile": "jarvis-isolated",
    "sandbox_enabled": True,
    "sandbox_network": False,
    "sandbox_memory_mb": 512,
    "sandbox_timeout_seconds": 300,
    "resource_monitor_enabled": True,
    "resource_poll_interval_seconds": 5.0,
    "resource_cpu_alert_percent": 85.0,
    "resource_memory_alert_percent": 85.0,
    "resource_alert_cooldown_seconds": 120.0,
    "audio_playback_enabled": False,
    "audio_allowed_roots": [],
    "audio_default_client_id": "",
    "weather_enabled": True,
    "weather_cache_ttl_seconds": 300.0,
    "weather_stale_after_seconds": 1800.0,
    "conflict_news_enabled": True,
    "conflict_news_cache_ttl_seconds": 300.0,
    "conflict_news_stale_after_seconds": 1800.0,
    "conflict_news_max_items": 25,
    "media_catalog_enabled": True,
    "media_catalog_cache_ttl_seconds": 900.0,
    "media_catalog_max_items": 20,
    "vision_enabled": False,
}


def _settings_path() -> Path:
    path = get_config_dir() / "runtime-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_settings() -> dict[str, Any]:
    path = _settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**_DEFAULTS, **{k: v for k, v in data.items() if k in _DEFAULTS}}
    except (OSError, ValueError):
        pass
    return dict(_DEFAULTS)


def _write_settings(data: dict[str, Any]) -> None:
    path = _settings_path()
    fd, temp_name = tempfile.mkstemp(prefix="runtime-settings.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _credential_status() -> dict[str, bool]:
    names = (
        "NIM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "CARTESIA_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TAVILY_API_KEY",
    )
    return {name: bool(os.environ.get(name, "").strip()) for name in names}


@router.get("")
async def get_settings(request: Request) -> dict[str, Any]:
    settings = _read_settings()
    # Never return environment values or token material.
    return {
        "settings": settings,
        "credentials": _credential_status(),
        "restart_required_for": ["llm_provider", "llm_model", "nim_rpm_limit", "speech_stt_backend", "speech_tts_backend", "wake_word_language"],
        "security": {
            "secrets_returned": False,
            "nim_rpm_hard_ceiling": 40,
            "sandbox_network_default": False,
        },
    }


@router.patch("")
async def patch_settings(patch: RuntimeSettingsPatch, request: Request) -> dict[str, Any]:
    data = _read_settings()
    changes = patch.model_dump(exclude_none=True)
    if "wake_words" in changes:
        words = [str(value).strip() for value in changes["wake_words"]]
        changes["wake_words"] = sorted({value for value in words if value})[:10]
    if "telegram_allowed_chat_ids" in changes:
        ids = [str(value).strip() for value in changes["telegram_allowed_chat_ids"]]
        changes["telegram_allowed_chat_ids"] = sorted({value for value in ids if value})[:100]
    if "audio_allowed_roots" in changes:
        roots = [str(value).strip() for value in changes["audio_allowed_roots"]]
        changes["audio_allowed_roots"] = sorted({value for value in roots if value})[:20]
    if "nim_rpm_limit" in changes and changes["nim_rpm_limit"] > 40:
        raise HTTPException(status_code=422, detail="NIM RPM limit cannot exceed 40")
    data.update(changes)
    _write_settings(data)
    request.app.state.runtime_settings = data
    media_catalog_service = getattr(request.app.state, "media_catalog_service", None)
    if media_catalog_service is not None:
        media_catalog_service.configure(
            cache_ttl_seconds=data["media_catalog_cache_ttl_seconds"],
            max_items=data["media_catalog_max_items"],
        )
        request.app.state.media_catalog_enabled = bool(data["media_catalog_enabled"])

    conflict_news_service = getattr(request.app.state, "conflict_news_service", None)
    if conflict_news_service is not None:
        conflict_news_service.configure(
            cache_ttl_seconds=data["conflict_news_cache_ttl_seconds"],
            stale_after_seconds=data["conflict_news_stale_after_seconds"],
            max_items=data["conflict_news_max_items"],
        )
        request.app.state.conflict_news_enabled = bool(data["conflict_news_enabled"])

    weather_service = getattr(request.app.state, "weather_service", None)
    if weather_service is not None:
        weather_service.configure(
            cache_ttl_seconds=data["weather_cache_ttl_seconds"],
            stale_after_seconds=data["weather_stale_after_seconds"],
        )
        request.app.state.weather_enabled = bool(data["weather_enabled"])

    audio_service = getattr(request.app.state, "audio_service", None)
    if audio_service is not None:
        audio_service.configure_roots(data["audio_allowed_roots"])
        request.app.state.audio_playback_enabled = bool(data["audio_playback_enabled"])

    monitor = getattr(request.app.state, "resource_monitor", None)
    if monitor is not None:
        monitor.configure(
            poll_interval_seconds=data["resource_poll_interval_seconds"],
            cpu_alert_percent=data["resource_cpu_alert_percent"],
            memory_alert_percent=data["resource_memory_alert_percent"],
            alert_cooldown_seconds=data["resource_alert_cooldown_seconds"],
        )
        request.app.state.resource_monitor_enabled = bool(data["resource_monitor_enabled"])
        if data["resource_monitor_enabled"]:
            monitor.start()
        else:
            monitor.stop()
    vision_service = getattr(request.app.state, "vision_service", None)
    if vision_service is not None:
        vision_service.configure(enabled=bool(data["vision_enabled"]))
    return {
        "settings": data,
        "credentials": _credential_status(),
        "restart_required": True,
        "security": {
            "secrets_returned": False,
            "nim_rpm_hard_ceiling": 40,
            "sandbox_network_default": False,
        },
    }


@router.get("/capabilities")
async def get_capabilities() -> dict[str, Any]:
    return {"capabilities": capabilities_for_catalog()}


@router.post("/test-credentials")
async def test_credentials(request: Request) -> dict[str, Any]:
    """Return presence only; network tests are explicit per provider elsewhere."""
    return {"credentials": _credential_status(), "secrets_returned": False}


__all__ = ["router"]

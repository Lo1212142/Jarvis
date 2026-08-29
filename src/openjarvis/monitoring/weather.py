"""Truthful Cairo weather retrieval with bounded caching and provenance."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from openjarvis.security.ssrf import check_ssrf

CAIRO_LATITUDE = 30.0444
CAIRO_LONGITUDE = 31.2357
CAIRO_TIMEZONE = "Africa/Cairo"
OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"


class WeatherUnavailable(RuntimeError):
    """Raised when no fresh or cached weather observation can be returned."""


@dataclass(slots=True)
class WeatherSnapshot:
    location: str
    latitude: float
    longitude: float
    timezone: str
    current: dict[str, Any]
    source_url: str
    retrieved_at: str
    stale: bool = False
    error: str | None = None
    _retrieved_epoch: float = field(default_factory=time.time, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "current": self.current,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "stale": self.stale,
            "error": self.error,
        }


class CairoWeatherService:
    def __init__(self, *, cache_ttl_seconds: float = 300.0, stale_after_seconds: float = 1800.0) -> None:
        self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        self.stale_after_seconds = max(self.cache_ttl_seconds, min(float(stale_after_seconds), 7 * 86400.0))
        self._cache: WeatherSnapshot | None = None

    def configure(self, *, cache_ttl_seconds: float | None = None, stale_after_seconds: float | None = None) -> None:
        if cache_ttl_seconds is not None:
            self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        if stale_after_seconds is not None:
            self.stale_after_seconds = max(self.cache_ttl_seconds, min(float(stale_after_seconds), 7 * 86400.0))

    def current_cairo(self, *, force_refresh: bool = False) -> WeatherSnapshot:
        now = time.time()
        if self._cache is not None and not force_refresh and now - self._cache._retrieved_epoch <= self.cache_ttl_seconds:
            return self._cache
        query = {
            "latitude": CAIRO_LATITUDE,
            "longitude": CAIRO_LONGITUDE,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
            "timezone": CAIRO_TIMEZONE,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
        }
        source_url = f"{OPEN_METEO_ENDPOINT}?{urlencode(query)}"
        ssrf_error = check_ssrf(source_url)
        if ssrf_error:
            raise WeatherUnavailable(f"weather source blocked: {ssrf_error}")
        try:
            response = httpx.get(source_url, timeout=10.0, follow_redirects=False, headers={"User-Agent": "OpenJarvis-Weather/1.0"})
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise WeatherUnavailable("weather response exceeds 1MB limit")
            payload = response.json()
            current = payload.get("current")
            if not isinstance(current, dict) or not current:
                raise WeatherUnavailable("weather response has no current observation")
            snapshot = WeatherSnapshot(
                location="Cairo, Egypt",
                latitude=CAIRO_LATITUDE,
                longitude=CAIRO_LONGITUDE,
                timezone=str(payload.get("timezone") or CAIRO_TIMEZONE),
                current=current,
                source_url=source_url,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                _retrieved_epoch=now,
            )
            self._cache = snapshot
            return snapshot
        except (httpx.HTTPError, ValueError, TypeError, KeyError, WeatherUnavailable) as exc:
            if self._cache is not None:
                age = now - self._cache._retrieved_epoch
                cached = WeatherSnapshot(**{**self._cache.to_dict(), "stale": age > self.stale_after_seconds, "error": f"refresh failed: {type(exc).__name__}", "_retrieved_epoch": self._cache._retrieved_epoch})
                self._cache = cached
                return cached
            raise WeatherUnavailable(f"Cairo weather unavailable: {type(exc).__name__}") from exc


_default_service = CairoWeatherService()


def get_default_weather_service() -> CairoWeatherService:
    return _default_service


__all__ = ["CAIRO_LATITUDE", "CAIRO_LONGITUDE", "CAIRO_TIMEZONE", "CairoWeatherService", "WeatherSnapshot", "WeatherUnavailable", "get_default_weather_service"]

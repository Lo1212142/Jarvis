"""Bounded movie/series discovery with provider provenance and spoiler controls."""

from __future__ import annotations

import html
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from openjarvis.security.ssrf import check_ssrf

TVMAZE_ENDPOINT = "https://api.tvmaze.com"
TMDB_ENDPOINT = "https://api.themoviedb.org/3"


class MediaCatalogUnavailable(RuntimeError):
    """Raised when the requested metadata provider is unavailable."""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    item_id: str
    media_type: str
    title: str
    overview: str
    year: int | None
    genres: tuple[str, ...]
    rating: float | None
    runtime_minutes: int | None
    source: str
    source_url: str
    retrieved_at: str
    spoiler_warning: bool = True

    def to_dict(self, *, include_summary: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["genres"] = list(self.genres)
        if not include_summary:
            data.pop("overview", None)
        return data


@dataclass(slots=True)
class CatalogResult:
    query: str
    items: list[CatalogItem]
    retrieved_at: str
    source_providers: tuple[str, ...]
    error: str | None = None

    def to_dict(self, *, include_summary: bool = False) -> dict[str, Any]:
        return {
            "query": self.query,
            "items": [item.to_dict(include_summary=include_summary) for item in self.items],
            "retrieved_at": self.retrieved_at,
            "source_providers": list(self.source_providers),
            "error": self.error,
        }


class MediaCatalogService:
    def __init__(self, *, tmdb_api_key: str | None = None, cache_ttl_seconds: float = 900.0, max_items: int = 20) -> None:
        self.tmdb_api_key = tmdb_api_key or os.getenv("TMDB_API_KEY", "")
        self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        self.max_items = max(1, min(int(max_items), 50))
        self._cache: dict[tuple[str, str, str], tuple[float, CatalogResult]] = {}

    def configure(self, *, cache_ttl_seconds: float | None = None, max_items: int | None = None) -> None:
        if cache_ttl_seconds is not None:
            self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        if max_items is not None:
            self.max_items = max(1, min(int(max_items), 50))

    def search(self, query: str, *, media_type: str = "all", language: str = "en-US", include_summary: bool = False, force_refresh: bool = False) -> CatalogResult:
        clean_query = " ".join(str(query).split())[:160]
        if not clean_query:
            raise ValueError("query is required")
        if media_type not in {"all", "movie", "series"}:
            raise ValueError("media_type must be all, movie, or series")
        key = (clean_query.casefold(), media_type, language[:32])
        now = time.time()
        cached = self._cache.get(key)
        if cached and not force_refresh and now - cached[0] <= self.cache_ttl_seconds:
            return cached[1]
        errors: list[str] = []
        items: list[CatalogItem] = []
        providers: list[str] = []
        if media_type in {"all", "series"}:
            try:
                items.extend(self._tvmaze_search(clean_query))
                providers.append("TVmaze")
            except MediaCatalogUnavailable as exc:
                errors.append(str(exc))
        if media_type in {"all", "movie"}:
            if self.tmdb_api_key:
                try:
                    items.extend(self._tmdb_movie_search(clean_query, language=language))
                    providers.append("TMDB")
                except MediaCatalogUnavailable as exc:
                    errors.append(str(exc))
            elif media_type == "movie":
                errors.append("movie metadata requires an enabled TMDB API key")
        if not items and errors:
            raise MediaCatalogUnavailable("; ".join(errors)[:1000])
        result = CatalogResult(clean_query, items[: self.max_items], datetime.now(timezone.utc).isoformat(), tuple(dict.fromkeys(providers)), "; ".join(errors)[:1000] or None)
        self._cache[key] = (now, result)
        return result

    def _tvmaze_search(self, query: str) -> list[CatalogItem]:
        url = f"{TVMAZE_ENDPOINT}/search/shows?{urlencode({'q': query})}"
        self._check_url(url)
        try:
            response = httpx.get(url, timeout=12.0, follow_redirects=False, headers={"User-Agent": "OpenJarvis-MediaCatalog/1.0"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MediaCatalogUnavailable(f"TVmaze unavailable: {type(exc).__name__}") from exc
        retrieved_at = datetime.now(timezone.utc).isoformat()
        results: list[CatalogItem] = []
        for entry in payload[: self.max_items]:
            show = entry.get("show", {}) if isinstance(entry, dict) else {}
            if not isinstance(show, dict) or not show.get("name"):
                continue
            premiered = str(show.get("premiered") or "")
            year = int(premiered[:4]) if premiered[:4].isdigit() else None
            rating_raw = (show.get("rating") or {}).get("average")
            runtime = show.get("runtime") or show.get("averageRuntime")
            results.append(CatalogItem(str(show.get("id")), "series", str(show["name"])[:300], _clean_summary(show.get("summary")), year, tuple(str(g) for g in (show.get("genres") or [])[:10]), float(rating_raw) if rating_raw is not None else None, int(runtime) if isinstance(runtime, (int, float)) else None, "TVmaze", str((show.get("url") or ""))[:2000], retrieved_at))
        return results

    def _tmdb_movie_search(self, query: str, *, language: str) -> list[CatalogItem]:
        params = {"query": query, "language": language[:32], "include_adult": "false", "page": 1}
        url = f"{TMDB_ENDPOINT}/search/movie?{urlencode(params)}"
        self._check_url(url)
        try:
            response = httpx.get(url, timeout=12.0, follow_redirects=False, headers={"User-Agent": "OpenJarvis-MediaCatalog/1.0", "Authorization": f"Bearer {self.tmdb_api_key}"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MediaCatalogUnavailable(f"TMDB unavailable: {type(exc).__name__}") from exc
        retrieved_at = datetime.now(timezone.utc).isoformat()
        results: list[CatalogItem] = []
        for movie in (payload.get("results") or [])[: self.max_items]:
            if not isinstance(movie, dict) or not movie.get("title"):
                continue
            date = str(movie.get("release_date") or "")
            results.append(CatalogItem(str(movie.get("id")), "movie", str(movie["title"])[:300], _clean_summary(movie.get("overview")), int(date[:4]) if date[:4].isdigit() else None, (), float(movie["vote_average"]) if movie.get("vote_average") is not None else None, None, "TMDB", f"https://www.themoviedb.org/movie/{movie.get('id')}", retrieved_at))
        return results

    @staticmethod
    def _check_url(url: str) -> None:
        if check_ssrf(url):
            raise MediaCatalogUnavailable("provider URL was blocked by SSRF policy")


def _clean_summary(value: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return " ".join(text.split())[:4000]


__all__ = ["CatalogItem", "CatalogResult", "MediaCatalogService", "MediaCatalogUnavailable", "TMDB_ENDPOINT", "TVMAZE_ENDPOINT"]

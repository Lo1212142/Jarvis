"""Bounded public conflict-news retrieval with provenance and no operational targeting."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from openjarvis.security.ssrf import check_ssrf

GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_CONFLICT_QUERY = '(war OR conflict OR ceasefire OR "armed conflict" OR hostilities)'


class ConflictNewsUnavailable(RuntimeError):
    """Raised when conflict news cannot be fetched and no cache is available."""


@dataclass(frozen=True, slots=True)
class ConflictNewsItem:
    item_id: str
    title: str
    url: str
    domain: str
    published_at: str
    language: str
    source_country: str
    retrieved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ConflictNewsSnapshot:
    query: str
    items: list[ConflictNewsItem]
    source_url: str
    retrieved_at: str
    stale: bool = False
    error: str | None = None
    _retrieved_epoch: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "items": [item.to_dict() for item in self.items],
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "stale": self.stale,
            "error": self.error,
        }


class ConflictNewsService:
    def __init__(self, *, cache_ttl_seconds: float = 300.0, stale_after_seconds: float = 1800.0, max_items: int = 25) -> None:
        self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        self.stale_after_seconds = max(self.cache_ttl_seconds, min(float(stale_after_seconds), 7 * 86400.0))
        self.max_items = max(1, min(int(max_items), 100))
        self._cache: dict[str, ConflictNewsSnapshot] = {}

    def configure(self, *, cache_ttl_seconds: float | None = None, stale_after_seconds: float | None = None, max_items: int | None = None) -> None:
        if cache_ttl_seconds is not None:
            self.cache_ttl_seconds = max(30.0, min(float(cache_ttl_seconds), 86400.0))
        if stale_after_seconds is not None:
            self.stale_after_seconds = max(self.cache_ttl_seconds, min(float(stale_after_seconds), 7 * 86400.0))
        if max_items is not None:
            self.max_items = max(1, min(int(max_items), 100))

    def latest(self, *, query: str = DEFAULT_CONFLICT_QUERY, force_refresh: bool = False) -> ConflictNewsSnapshot:
        clean_query = " ".join(str(query).split())[:200] or DEFAULT_CONFLICT_QUERY
        now = time.time()
        cached = self._cache.get(clean_query)
        if cached is not None and not force_refresh and now - cached._retrieved_epoch <= self.cache_ttl_seconds:
            return cached
        params = {"query": clean_query, "mode": "artlist", "format": "json", "maxrecords": self.max_items, "timespan": "24h", "sort": "datedesc"}
        source_url = f"{GDELT_DOC_ENDPOINT}?{urlencode(params)}"
        ssrf_error = check_ssrf(source_url)
        if ssrf_error:
            raise ConflictNewsUnavailable(f"conflict news source blocked: {ssrf_error}")
        try:
            response = httpx.get(source_url, timeout=15.0, follow_redirects=False, headers={"User-Agent": "OpenJarvis-ConflictNews/1.0"})
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise ConflictNewsUnavailable("conflict news response exceeds 5MB limit")
            payload = response.json()
            raw_items = payload.get("articles", [])
            if not isinstance(raw_items, list):
                raise ConflictNewsUnavailable("conflict news response has no article list")
            retrieved_at = datetime.now(timezone.utc).isoformat()
            items: list[ConflictNewsItem] = []
            seen: set[str] = set()
            for raw in raw_items[: self.max_items]:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or "").strip()[:1000]
                url = str(raw.get("url") or "").strip()[:2000]
                if not title or not url or not url.startswith("https://"):
                    continue
                item_id = hashlib.sha256(f"{url}|{title}".encode("utf-8")).hexdigest()
                if item_id in seen:
                    continue
                seen.add(item_id)
                items.append(ConflictNewsItem(item_id, title, url, str(raw.get("domain") or "")[:255], str(raw.get("seendate") or raw.get("published") or "")[:80], str(raw.get("language") or "")[:80], str(raw.get("sourcecountry") or "")[:80], retrieved_at))
            snapshot = ConflictNewsSnapshot(clean_query, items, source_url, retrieved_at, _retrieved_epoch=now)
            self._cache[clean_query] = snapshot
            return snapshot
        except (httpx.HTTPError, ValueError, TypeError, KeyError, ConflictNewsUnavailable) as exc:
            if cached is not None:
                age = now - cached._retrieved_epoch
                stale = ConflictNewsSnapshot(cached.query, cached.items, cached.source_url, cached.retrieved_at, age > self.stale_after_seconds, f"refresh failed: {type(exc).__name__}", cached._retrieved_epoch)
                self._cache[clean_query] = stale
                return stale
            raise ConflictNewsUnavailable(f"conflict news unavailable: {type(exc).__name__}") from exc


__all__ = ["ConflictNewsItem", "ConflictNewsService", "ConflictNewsSnapshot", "ConflictNewsUnavailable", "DEFAULT_CONFLICT_QUERY", "GDELT_DOC_ENDPOINT"]

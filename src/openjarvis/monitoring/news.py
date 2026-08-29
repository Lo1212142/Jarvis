"""Allowlisted RSS/Atom change monitor with SSRF and bounded parsing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from openjarvis.security.ssrf import check_ssrf


@dataclass(frozen=True, slots=True)
class NewsItem:
    item_id: str
    title: str
    url: str
    summary: str
    published_at: str
    source: str


class NewsMonitor:
    def __init__(self, allowed_feeds: list[str], *, max_items_per_feed: int = 100) -> None:
        self.allowed_feeds = tuple(allowed_feeds[:50])
        self.max_items_per_feed = max(1, min(int(max_items_per_feed), 500))
        self.seen: set[str] = set()

    def fetch(self, feed_url: str) -> list[NewsItem]:
        self._validate_feed(feed_url)
        response = httpx.get(feed_url, timeout=15.0, follow_redirects=False, headers={"User-Agent": "OpenJarvis-NewsMonitor/1.0"})
        response.raise_for_status()
        if len(response.content) > 5 * 1024 * 1024:
            raise ValueError("feed exceeds 5MB limit")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ValueError("feed is not valid XML") from exc
        items: list[NewsItem] = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag not in {"item", "entry"}:
                continue
            fields: dict[str, str] = {}
            for child in list(element):
                child_tag = child.tag.rsplit("}", 1)[-1].lower()
                value = (child.text or "").strip()
                if child_tag == "link" and not value:
                    value = str(child.attrib.get("href", "")).strip()
                fields.setdefault(child_tag, value[:20_000])
            url = fields.get("link", "")
            title = fields.get("title", "")
            if not title or not url:
                continue
            item_id = fields.get("guid") or fields.get("id") or hashlib.sha256(f"{feed_url}|{url}|{title}".encode()).hexdigest()
            item = NewsItem(item_id[:256], title, url, fields.get("description") or fields.get("summary", ""), fields.get("pubdate") or fields.get("published", "") or datetime.now(timezone.utc).isoformat(), feed_url)
            if item.item_id not in self.seen:
                self.seen.add(item.item_id)
                items.append(item)
            if len(items) >= self.max_items_per_feed:
                break
        return items

    def _validate_feed(self, feed_url: str) -> None:
        if feed_url not in self.allowed_feeds:
            raise ValueError("feed URL is not in the configured allowlist")
        parsed = urlparse(feed_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("news feeds must use absolute HTTPS URLs")
        ssrf_error = check_ssrf(feed_url)
        if ssrf_error:
            raise ValueError(f"SSRF blocked: {ssrf_error}")


__all__ = ["NewsItem", "NewsMonitor"]

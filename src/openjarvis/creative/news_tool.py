"""Tech & science news tool — deep, aggregated, multi-source briefings.

``tech_news`` searches multiple queries (English + Arabic), deduplicates,
ranks by source quality, categorizes by domain (AI, biotech, space,
energy, robotics, computing), and in ``deep`` mode fetches the top
articles and extracts their text to compose a rich briefing — ready for
Jarvis to reason over and answer like an analyst.

Uses the ``ddgs`` library when available (same as the built-in web_search
fallback) with an HTML fallback so no API key is ever required.
"""

from __future__ import annotations

import base64
import html as _html
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en,ar;q=0.8",
}

# Source credibility heuristics (higher = more weight).
_SOURCE_RANKS = {
    "nature.com": 10, "science.org": 10, "arxiv.org": 9, "ieee.org": 9,
    "reuters.com": 9, "apnews.com": 9, "bbc.com": 8, "npr.org": 8,
    "mit.edu": 9, "technologyreview.com": 9, "sciam.com": 8,
    "theverge.com": 7, "techcrunch.com": 7, "arstechnica.com": 7,
    "wired.com": 7, "engadget.com": 6, "venturebeat.com": 6,
    "nytimes.com": 8, "wsj.com": 8, "economist.com": 8,
    "pnas.org": 10, "cell.com": 10, "thelancet.com": 10,
    "interestingengineering.com": 5, "livescience.com": 6,
    "space.com": 6, "phys.org": 7, "eurekalert.org": 7,
}

_CATEGORIES = {
    "AI & ML": ("ai", "artificial intelligence", "machine learning", "llm",
                "neural", "gpt", "model", "deep learning", "روبوت ذكاء",
                "ذكاء اصطناعي", "تعلم الآلة"),
    "Biotech & Medicine": ("brain", "cell", "gene", "crispr", "dna", "protein",
                           "cancer", "vaccine", "clinical", "neuron",
                           "خلايا", "دماغ", "جينات", "طب", "لقاح"),
    "Computing & Chips": ("chip", "semiconductor", "gpu", "cpu", "quantum",
                          "processor", "silicon", "datacenter", "server",
                          "شرائح", "معالج", "حوسبة", "كمبيوتر"),
    "Space & Physics": ("space", "nasa", "mars", "telescope", "satellite",
                        "rocket", "photon", "particle", "فضاء", "صاروخ"),
    "Energy & Climate": ("battery", "solar", "fusion", "energy", "climate",
                         "hydrogen", "carbon", "طاقة", "مناخ", "بطاريات"),
    "Robotics": ("robot", "drone", "autonomous", "humanoid", "actuator",
                 "روبوت", "طائرة بدون طيار"),
}

_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")


def _unwrap_bing_url(href: str) -> str:
    """Bing wraps result URLs in ``bing.com/ck/a`` redirects; the real
    target is the base64 value of the ``u=a1<base64>`` query parameter."""
    try:
        if "bing.com/ck/" not in href:
            return href
        href = _html.unescape(href)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1") and len(u) > 2:
            b64 = u[2:]
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64).decode("utf-8", "replace")
            if decoded.startswith("http"):
                return decoded
    except Exception:
        pass
    return href


def _ddg_search(query: str, max_results: int = 8) -> List[Dict[str, str]]:
    """Multi-endpoint text search, no API key required.

    Order: ddgs library → html.duckduckgo → lite.duckduckgo → Bing HTML.
    Each endpoint is tried until one returns usable results (search
    endpoints rate-limit and challenge unpredictably).
    """
    # 1) ddgs library (when installed).
    try:
        from ddgs import DDGS

        raw = list(DDGS().text(query, max_results=max_results))
        out = []
        for r in raw:
            out.append({
                "title": r.get("title", ""),
                "url": r.get("href", r.get("url", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            })
        if out:
            return out
    except Exception as exc:
        logger.debug("ddgs library failed: %s", exc)

    # 2) DuckDuckGo HTML + lite endpoints.
    for ddg_url in ("https://html.duckduckgo.com/html/?q=",
                    "https://lite.duckduckgo.com/lite/?q="):
        try:
            with httpx.Client(timeout=15.0, headers=_HEADERS,
                              follow_redirects=True) as client:
                resp = client.get(ddg_url + urllib.parse.quote(query))
            if resp.status_code != 200:
                continue
            html = resp.text
            blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.DOTALL,
            )
            if not blocks:
                blocks = re.findall(
                    r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    html, re.DOTALL,
                )
            snippets = re.findall(
                r'class="(?:result__snippet|result-snippet)"[^>]*>(.*?)</a>',
                html, re.DOTALL,
            )
            results: List[Dict[str, str]] = []
            for i, (href, title) in enumerate(blocks[:max_results]):
                clean_url = urllib.parse.unquote(
                    href.split("uddg=", 1)[-1].split("&rut=", 1)[0]
                ) if "uddg=" in href else href
                snippet = _RE_WS.sub(" ", _RE_TAG.sub(" ", snippets[i])).strip() \
                    if i < len(snippets) else ""
                title = _RE_WS.sub(" ", _RE_TAG.sub(" ", title)).strip()
                if title and clean_url.startswith("http"):
                    results.append({"title": title, "url": clean_url,
                                    "snippet": snippet})
            if results:
                return results
        except Exception as exc:
            logger.debug("DDG endpoint failed: %s", exc)

    # 3) Bing HTML (very reliable without a key).
    try:
        with httpx.Client(timeout=15.0, headers=_HEADERS,
                          follow_redirects=True) as client:
            resp = client.get("https://www.bing.com/search?q="
                              + urllib.parse.quote(query) + "&count=20")
        if resp.status_code == 200 and "b_algo" in resp.text:
            results = []
            blocks = re.findall(r'<li class="b_algo".*?</li>', resp.text,
                                re.DOTALL)
            for block in blocks[:max_results]:
                link = re.search(
                    r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    block, re.DOTALL)
                if not link:
                    continue
                url, title = _unwrap_bing_url(link.group(1)), link.group(2)
                snippet_m = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
                snippet = _RE_WS.sub(" ", _RE_TAG.sub(" ",
                                  snippet_m.group(1))).strip() if snippet_m else ""
                title = _html.unescape(
                    _RE_WS.sub(" ", _RE_TAG.sub(" ", title))).strip()
                if title and url.startswith("http"):
                    results.append({"title": title, "url": url,
                                    "snippet": snippet})
            if results:
                return results
    except Exception as exc:
        logger.debug("Bing fallback failed: %s", exc)
    return []


def _rank(url: str) -> int:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return 0
    host = host.lower().lstrip("www.")
    for domain, score in _SOURCE_RANKS.items():
        if host == domain or host.endswith("." + domain):
            return score
    return 3


def _categorize(text: str) -> str:
    low = " ".join(text.lower().split())
    best, best_hits = "General Tech", 0
    for category, keywords in _CATEGORIES.items():
        hits = sum(1 for kw in keywords if kw in low)
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _dedup(results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for r in results:
        try:
            norm = urllib.parse.urlparse(r["url"]).netloc.lower().lstrip("www.") + \
                urllib.parse.urlparse(r["url"]).path.rstrip("/")
        except ValueError:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(r)
    return out


def _fetch_article(url: str, max_chars: int = 6000) -> str:
    """Fetch an article and extract its readable text (crude readability)."""
    try:
        with httpx.Client(timeout=20.0, headers=_HEADERS,
                          follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
        html = resp.text
        # Prefer <article>/<main> blocks.
        for pattern in (r"<article[^>]*>(.*?)</article>", r"<main[^>]*>(.*?)</main>"):
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                html = match.group(1)
                break
        # Drop non-content blocks.
        html = re.sub(r"<(script|style|nav|footer|header|aside|form)[^>]*>.*?</\1>",
                      " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Paragraph-ish extraction.
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html,
                                re.DOTALL | re.IGNORECASE)
        text_parts = []
        for para in paragraphs:
            clean = _RE_WS.sub(" ", _RE_TAG.sub(" ", para)).strip()
            if len(clean) > 60:
                text_parts.append(clean)
        text = "\n\n".join(text_parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[truncated]"
        return text
    except Exception as exc:
        logger.debug("article fetch failed (%s): %s", url, exc)
        return ""


def search_tech_news(
    topic: str = "",
    *,
    days: int = 14,
    max_results: int = 12,
    language: str = "auto",
) -> Dict[str, Any]:
    """Aggregate tech/science news across queries, languages and sources."""
    topic = (topic or "").strip()
    arabic = language == "ar" or (language == "auto" and
                                  any("\u0600" <= c <= "\u06FF" for c in topic))
    if topic:
        queries = [
            f"{topic} latest news breakthrough",
            f"{topic} research announcement",
            (f"{topic} آخر الأخبار والتطورات" if arabic else f"{topic} science news this month"),
        ]
    else:
        queries = [
            "artificial intelligence breakthrough announcement",
            "science technology news this week",
            "biotech computing space research news",
        ]
    recency_suffix = f" past {days} days" if days and days <= 30 else ""

    all_results: List[Dict[str, str]] = []
    for query in queries:
        for r in _ddg_search(query + recency_suffix,
                             max_results=max(5, max_results // 2)):
            r["query"] = query
            all_results.append(r)
        time.sleep(0.3)  # be polite to the search endpoint

    results = _dedup(all_results)
    for r in results:
        r["rank"] = _rank(r["url"])
        r["category"] = _categorize(r["title"] + " " + r["snippet"])
    results.sort(key=lambda r: r["rank"], reverse=True)
    results = results[:max_results]

    by_category: Dict[str, List[Dict[str, str]]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
    return {"results": results, "by_category": by_category, "topic": topic}


@ToolRegistry.register("tech_news")
class TechNewsTool(BaseTool):
    """Deep, aggregated technology & science news briefing."""

    tool_id = "tech_news"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="tech_news",
            description=(
                "Search and aggregate the latest technology and science news"
                " with professional depth. Covers AI, biotech, computing,"
                " space, energy and robotics. Options: 'brief' (headline"
                " digest with sources, fast) or 'deep' (also fetches the top"
                " articles' full text for an analyst-grade briefing)."
                " Pass a topic to focus (e.g. 'human brain cells in"
                " servers', 'quantum chips', 'خلايا دماغ بشرية'). Works"
                " without any API key."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Focus topic (empty = general tech/science roundup).",
                    },
                    "depth": {
                        "type": "string", "enum": ["brief", "deep"],
                        "default": "brief",
                        "description": "brief = digest; deep = fetch top articles' text.",
                    },
                    "days": {"type": "integer", "default": 14,
                             "description": "Recency window in days."},
                    "max_results": {"type": "integer", "default": 12},
                    "language": {
                        "type": "string", "enum": ["auto", "en", "ar"],
                        "description": "Query language hint (default auto).",
                    },
                "required": [],
                },
            },
            category="news",
            timeout_seconds=180.0,
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        topic = str(params.get("topic") or "").strip()
        depth = str(params.get("depth") or "brief").lower()
        days = int(params.get("days") or 14)
        max_results = int(params.get("max_results") or 12)
        language = str(params.get("language") or "auto")
        try:
            data = search_tech_news(
                topic, days=days, max_results=max_results, language=language
            )
        except Exception as exc:
            logger.warning("tech_news failed: %s", exc)
            return ToolResult(tool_name="tech_news",
                              content=f"News search failed: {exc}", success=False)
        results = data["results"]
        if not results:
            return ToolResult(
                tool_name="tech_news",
                content="No news results found — the search endpoint may be"
                        " rate-limited. Try again in a moment.",
                success=False,
            )

        lines: List[str] = []
        header = f"Tech & science briefing — {topic or 'general roundup'}"
        lines.append(f"## {header}\n")
        lines.append(f"{len(results)} sources across "
                     f"{len(data['by_category'])} categories.\n")
        for category, items in data["by_category"].items():
            lines.append(f"### {category}")
            for r in items:
                host = ""
                try:
                    host = urllib.parse.urlparse(r["url"]).hostname or ""
                except ValueError:
                    pass
                lines.append(f"- **{r['title']}**  \n  {r['snippet'][:220]}"
                             f"\n  Source: [{host}]({r['url']})")
            lines.append("")

        if depth == "deep":
            lines.append("## Deep dive — top articles\n")
            fetched = 0
            for r in results[:4]:
                text = _fetch_article(r["url"])
                if text:
                    fetched += 1
                    lines.append(f"### {r['title']}\nSource: {r['url']}\n")
                    # Keep the meatiest excerpt.
                    excerpt = text[:2200]
                    lines.append(f"> {excerpt}\n")
            if not fetched:
                lines.append("(Article bodies could not be fetched — snippets"
                             " above are from search results.)")

        lines.append(
            "Use these sources to answer with specifics: cite the source"
            " host, connect findings, and flag what is verified vs claim."
        )
        return ToolResult(tool_name="tech_news", content="\n".join(lines),
                          success=True,
                          metadata={"sources": [r["url"] for r in results],
                                    "topic": topic, "depth": depth})


__all__ = ["TechNewsTool", "search_tech_news", "_fetch_article"]

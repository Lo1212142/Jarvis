"""Internet-device intelligence — ``osint_shodan``.

Shodan queries without friction — works out of the box with NO API key
and upgrades itself automatically when the user saves one:

* ``action=lookup`` (default) — full report for any public IP:
  organisation, ASN, ISP, country/city + coordinates, hostnames,
  open ports, service banners, operating system, known CVE
  vulnerabilities and an automatic risk assessment. Data source
  strategy (failover chain, no configuration needed):

  1. **Shodan full REST API** — used when a ``shodan_api_key`` is saved
     (free account works) *or* when the endpoint allows key-less host
     lookups (verified live: it currently does; if that changes the
     tool degrades gracefully to 2).
  2. **Shodan InternetDB** — the key-less free endpoint
     (``https://internetdb.shodan.io/{ip}``, plus the legacy
     ``/api/{ip}`` path as automatic fallback): ports, hostnames,
     CPEs, tags and CVEs.
  3. Merged — when both respond, InternetDB enriches the full API
     record with the fresh CVE list.

* ``action=search`` — the real Shodan search engine (query language:
  ``apache country:EG``, ``port:22 has_screenshot:true``…). Requires
  ``shodan_api_key`` — returns a table of matching hosts.

* ``action=profile`` — the saved key's account: plan, credits left,
  scan credits… Requires ``shodan_api_key``.

Multi-IP lookups are supported (comma list, capped by settings).

Key storage (optional, free account): register at shodan.io then
``POST /api/creative/keys/shodan_api_key`` (or export SHODAN_API_KEY).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import media_settings

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "openjarvis-shodan/1.0"}

# Ports that meaningfully raise exposure when reachable from the internet.
_RISKY_PORTS = {
    21: "FTP", 23: "Telnet", 445: "SMB", 1433: "MSSQL", 1521: "Oracle",
    3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC",
    6379: "Redis", 9200: "Elasticsearch", 11211: "Memcached",
    27017: "MongoDB", 5984: "CouchDB", 2375: "Docker API",
    161: "SNMP (public)", 502: "Modbus", 47808: "BACnet",
}

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Settings (section "shodan" in media-settings.json — optional overrides)
# ---------------------------------------------------------------------------

_SHODAN_DEFAULTS: Dict[str, Any] = {
    "api_base": "https://api.shodan.io",
    "internetdb_base": "https://internetdb.shodan.io",
    "internetdb_legacy_prefix": "/api",   # older documented path, kept as failover
    "timeout_s": 25,
    "cache_ttl_s": 600,
    "max_lookup_ips": 10,
    "max_search_results": 20,
    "max_banners": 8,
}


def _shodan_settings() -> Dict[str, Any]:
    try:
        stored = media_settings.load_settings().get("shodan", {}) or {}
    except Exception:
        stored = {}
    merged = dict(_SHODAN_DEFAULTS)
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if v})
    return merged


def resolve_shodan_key() -> str:
    """Saved key from the creative key store → SHODAN_API_KEY env → ''."""
    try:
        return media_settings.resolve_api_key(
            {"api_key_env": "SHODAN_API_KEY"}, "shodan_api_key")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str, ttl: float) -> Optional[Any]:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------


def _validate_ip(raw: str) -> str:
    """Normalise/validate one IPv4 address; raises ValueError otherwise."""
    text = str(raw or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a valid IP address") from None
    if not addr.is_global:
        raise ValueError(
            f"{text} is a private/reserved address — Shodan only indexes "
            f"internet-facing hosts")
    return str(addr)


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


def internetdb_lookup(ip: str) -> Optional[Dict[str, Any]]:
    """Key-less InternetDB lookup. ``None`` when no data is available.

    Tries the live endpoint form first (``/{ip}``), then the legacy
    documented path (``/api/{ip}``) — both were verified against the
    real service. A 404 with ``No information available`` means the IP
    simply isn't indexed; it is returned as ``None``, not an error.
    """
    cfg = _shodan_settings()
    timeout = float(cfg["timeout_s"])
    paths = [f"/{ip}", f"{cfg['internetdb_legacy_prefix']}/{ip}"]
    last_error: Optional[str] = None
    for path in paths:
        url = cfg["internetdb_base"].rstrip("/") + path
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=timeout)
            if resp.status_code == 404:
                # This form says "no data" — try the other path once,
                # then conclude the IP is not indexed.
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    detail = ""
                if "No information" in str(detail) or path == paths[-1]:
                    if path == paths[-1]:
                        return None
                    last_error = f"404 {detail}"
                    continue
                return None
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("ip") or payload.get("ports"):
                return payload
            return None
        except Exception as exc:
            last_error = f"{url}: {exc}"
            continue
    if last_error:
        logger.debug("internetdb exhausted: %s", last_error)
    return None


def full_api_host(ip: str, api_key: str) -> Dict[str, Any]:
    """Full Shodan host record (banners, org, ASN, geo). Raises on auth."""
    cfg = _shodan_settings()
    params = {"key": api_key} if api_key else {}
    resp = httpx.get(
        f"{cfg['api_base'].rstrip('/')}/shodan/host/{ip}",
        params=params, headers=_HEADERS,
        timeout=float(cfg["timeout_s"]))
    if resp.status_code in (401, 403):
        raise PermissionError(
            "the full Shodan API now requires a key "
            "(save one via POST /api/creative/keys/shodan_api_key)")
    resp.raise_for_status()
    return resp.json()


def lookup_ip(ip: str, api_key: str, enrich: bool = True) -> Dict[str, Any]:
    """Orchestrated single-IP lookup: full API → InternetDB → merged."""
    cache_key = f"host:{ip}:{bool(api_key)}:{enrich}"
    cached = _cache_get(cache_key, float(_shodan_settings()["cache_ttl_s"]))
    if cached is not None:
        return cached

    report: Dict[str, Any] = {"ip": ip, "sources": []}
    full: Optional[Dict[str, Any]] = None
    try:
        full = full_api_host(ip, api_key)
        report["sources"].append("shodan_full_api")
    except PermissionError:
        pass  # fall through to InternetDB
    except Exception as exc:
        logger.debug("full api host %s failed: %s", ip, exc)

    idb: Optional[Dict[str, Any]] = None
    if enrich or full is None:
        try:
            idb = internetdb_lookup(ip)
            if idb is not None:
                report["sources"].append("internetdb")
        except Exception as exc:
            logger.debug("internetdb %s failed: %s", ip, exc)

    if full is None and idb is None:
        report["no_data"] = True
        _cache_put(cache_key, report)
        return report

    if full is not None:
        loc = full.get("location") or {}
        banners = full.get("data") or []
        report.update({
            "org": full.get("org"),
            "isp": full.get("isp"),
            "asn": full.get("asn"),
            "os": full.get("os"),
            "country": full.get("country_name") or loc.get("country_name"),
            "country_code": full.get("country_code") or loc.get("country_code"),
            "city": full.get("city") or loc.get("city"),
            "region": full.get("region_code") or loc.get("region_code"),
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "ports": sorted(set(full.get("ports") or [])),
            "hostnames": sorted(set(full.get("hostnames") or [])),
            "domains": sorted(set(full.get("domains") or [])),
            "tags": sorted(set(full.get("tags") or [])),
            "last_update": full.get("last_update"),
            "banners": [{
                "port": b.get("port"),
                "transport": b.get("transport"),
                "org": b.get("org"),
                "product": b.get("product"),
                "version": b.get("version"),
                "os": b.get("os"),
                "timestamp": b.get("timestamp"),
                "text": re.sub(r"\s+", " ", str(b.get("data") or ""))[:160],
            } for b in banners],
        })
    if idb is not None:
        vulns = list(idb.get("vulns") or [])
        # InternetDB sometimes packs CVEs into "tags" — normalise both.
        for tag in idb.get("tags") or []:
            if _CVE_RE.search(str(tag)) and tag not in vulns:
                vulns.append(str(tag))
        report["vulns"] = sorted(set(vulns))
        if not report.get("ports"):
            report["ports"] = sorted(idb.get("ports") or [])
        if not report.get("hostnames"):
            report["hostnames"] = sorted(idb.get("hostnames") or [])
        if not report.get("tags"):
            report["tags"] = [t for t in (idb.get("tags") or [])
                              if not _CVE_RE.search(str(t))]
        report.setdefault("cpes", sorted(idb.get("cpes") or []))
        if idb.get("ip") and not report.get("ip"):
            report["ip"] = idb.get("ip")
    report.setdefault("vulns", [])
    report.pop("no_data", None)
    report["risk"] = assess_risk(report)
    _cache_put(cache_key, report)
    return report


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------


def assess_risk(report: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic exposure scoring — CVEs, risky ports, breadth."""
    vulns = report.get("vulns") or []
    ports = report.get("ports") or []
    score = 0
    reasons: List[str] = []
    if vulns:
        score += min(40 + (len(vulns) - 1) * 10, 70)
        reasons.append(f"{len(vulns)} known CVEs")
    risky = [(p, _RISKY_PORTS[p]) for p in ports if p in _RISKY_PORTS]
    if risky:
        score += min(len(risky) * 8, 40)
        reasons.append("risky services: " +
                       ", ".join(f"{p} ({name})" for p, name in risky))
    if len(ports) > 10:
        score += 10
        reasons.append(f"{len(ports)} open ports (wide surface)")
    if score >= 60:
        level, label = "HIGH", "عالية"
    elif score >= 30:
        level, label = "MEDIUM", "متوسطة"
    elif score > 0:
        level, label = "LOW", "منخفضة"
    else:
        level, label = "MINIMAL", "ضئيلة"
    return {"score": min(score, 100), "level": level, "label": label,
            "reasons": reasons, "risky_ports": risky}


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@ToolRegistry.register("osint_shodan")
class OsintShodanTool(BaseTool):
    """Shodan internet-device intelligence (key-less fallback + full API)."""

    tool_id = "osint_shodan"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="osint_shodan",
            description=(
                "Internet-connected device intelligence via Shodan. "
                "action=lookup (default): a full report for any public IP — "
                "organisation, ASN, ISP, country/city, coordinates, "
                "hostnames, open ports, service banners, OS, known CVE "
                "vulnerabilities and an automatic risk score. Works with "
                "NO API key (free InternetDB + key-less host API with "
                "automatic failover); saving shodan_api_key upgrades data "
                "quality. action=search: the real Shodan query engine "
                "('apache country:EG', 'port:3389 has_screenshot:true'…) — "
                "needs the key. action=profile: key account credits/plan. "
                "Use for attack-surface review of your own assets, "
                "verifying what a firewall exposes, or attributing an IP "
                "from public scan data."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "default": "lookup",
                        "enum": ["lookup", "search", "profile"],
                    },
                    "ip": {
                        "type": "string",
                        "description": "lookup: one IPv4 address, or a "
                                       "comma-separated list (max 10).",
                    },
                    "query": {
                        "type": "string",
                        "description": "search: Shodan query language "
                                       "string (e.g. 'apache "
                                       "country:EG port:80').",
                    },
                    "page": {
                        "type": "integer", "default": 1,
                        "description": "search: results page.",
                    },
                    "limit": {
                        "type": "integer", "default": 10,
                        "description": "search: rows to return (max 50).",
                    },
                },
            },
            category="geoint",
            timeout_seconds=120.0,
            required_capabilities=["network:fetch"],
        )

    # -- report rendering ---------------------------------------------------

    @staticmethod
    def _render_lookup(report: Dict[str, Any]) -> str:
        if report.get("no_data"):
            return (f"## Shodan — {report['ip']}\n"
                    "No indexed data for this IP (never scanned, or it is "
                    "not internet-facing).")
        lines = [f"## Shodan — {report['ip']}",
                 f"**Source:** {' + '.join(report.get('sources', []))}"
                 f"{' · Last seen: ' + report['last_update'][:10] if report.get('last_update') else ''}",
                 ""]
        geo_bits = [b for b in (
            f"{report.get('city')}, " if report.get("city") else "",
            str(report.get("country") or ""),) if b]
        if geo_bits or report.get("lat") is not None:
            coords = (f" · {report['lat']:.4f}, {report['lon']:.4f}"
                      if report.get("lat") is not None and
                      report.get("lon") is not None else "")
            lines.append(f"الموقع: {''.join(geo_bits) or '—'}{coords}")
        for label, key in (("المنظمة/ISP", "org"), ("ASN", "asn"),
                           ("نظام التشغيل", "os")):
            if report.get(key):
                lines.append(f"{label}: {report[key]}")
        hostnames = report.get("hostnames") or []
        if hostnames:
            lines.append(f"Hostnames: {', '.join(hostnames[:8])}"
                         + (f" (+{len(hostnames) - 8} more)" if len(hostnames) > 8 else ""))
        tags = report.get("tags") or []
        if tags:
            lines.append(f"Tags: {', '.join(tags[:8])}")

        ports = report.get("ports") or []
        lines.append(f"\n### المنافذ المفتوحة ({len(ports)})\n")
        if ports:
            flagged = {p for p, _ in (report.get("risk", {})
                                      .get("risky_ports") or [])}
            lines.append(", ".join(
                f"**{p}**" if p in flagged else str(p) for p in ports))
        else:
            lines.append("—")

        banners = report.get("banners") or []
        if banners:
            cfg_max = int(_shodan_settings()["max_banners"])
            lines.append(f"\n### الخدمات المكتشفة (banners)\n")
            lines.append("| Port | Transport | Product | Banner |")
            lines.append("|---|---|---|---|")
            for b in banners[:cfg_max]:
                product = " ".join(x for x in (b.get("product"),
                                               b.get("version")) if x) or "—"
                lines.append(f"| {b.get('port')} | {b.get('transport') or '—'} "
                             f"| {product} | {(b.get('text') or '—')[:90]} |")

        vulns = report.get("vulns") or []
        if vulns:
            lines.append(f"\n### ⚠ الاستغلالات المعروفة ({len(vulns)})\n")
            lines.append(", ".join(f"`{v}`" for v in vulns[:12])
                         + (f" (+{len(vulns) - 12} more)" if len(vulns) > 12 else ""))

        risk = report.get("risk") or assess_risk(report)
        lines.append(f"\n**تقييم المخاطر: {risk['level']} ({risk['label']})"
                     f" — score {risk['score']}/100**")
        for reason in risk["reasons"]:
            lines.append(f"- {reason}")
        return "\n".join(lines)

    # -- actions ------------------------------------------------------------

    def _lookup(self, **params: Any) -> ToolResult:
        cfg = _shodan_settings()
        raw = str(params.get("ip") or "").strip()
        if not raw:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content="lookup needs ip (one address or a comma list).")
        api_key = resolve_shodan_key()
        try:
            ips = [_validate_ip(part)
                   for part in raw.split(",") if part.strip()]
        except ValueError as exc:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content=f"invalid ip: {exc}")
        ips = ips[: int(cfg["max_lookup_ips"])]

        reports = [lookup_ip(ip, api_key, enrich=len(ips) <= 3)
                   for ip in ips]
        if len(reports) == 1:
            report = reports[0]
            if report.get("no_data"):
                return ToolResult(
                    tool_name="osint_shodan", success=False,
                    content=(f"No Shodan data for {report['ip']} — the IP "
                             f"was never scanned or is not internet-facing."))
            return ToolResult(
                tool_name="osint_shodan", success=True,
                content=self._render_lookup(report),
                metadata={"report": report})
        # Multi-IP: summary table + full detail for the riskiest.
        reports.sort(key=lambda r: (r.get("risk", {}) or {}).get("score", 0),
                     reverse=True)
        lines = [f"## Shodan — {len(reports)} hosts",
                 "| IP | Risk | Ports | CVEs | Org | Country |",
                 "|---|---|---|---|---|---|"]
        for r in reports:
            risk = (r.get("risk") or {}).get("level", "—")
            lines.append(
                f"| {r['ip']} | {risk} | {len(r.get('ports') or [])} "
                f"| {len(r.get('vulns') or [])} "
                f"| {(r.get('org') or '—')[:28]} "
                f"| {(r.get('country') or '—')[:18]} |")
        lines.append("\n### أعلى مخاطرة\n")
        lines.append(self._render_lookup(reports[0]))
        return ToolResult(
            tool_name="osint_shodan", success=True,
            content="\n".join(lines),
            metadata={"reports": reports})

    def _search(self, **params: Any) -> ToolResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content="search needs a query (Shodan syntax: "
                        "'apache country:EG port:80').")
        api_key = resolve_shodan_key()
        if not api_key:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content=("Shodan search requires a free API key — save it "
                         "once: POST /api/creative/keys/shodan_api_key "
                         "(register at https://shodan.io) or export "
                         "SHODAN_API_KEY. Key-less lookups still work via "
                         "action=lookup."))
        cfg = _shodan_settings()
        limit = min(max(int(params.get("limit") or 10), 1), 50)
        page = max(int(params.get("page") or 1), 1)
        resp = httpx.get(
            f"{cfg['api_base'].rstrip('/')}/shodan/host/search",
            params={"query": query, "key": api_key, "page": page},
            headers=_HEADERS, timeout=float(cfg["timeout_s"]) + 10)
        if resp.status_code in (401, 403):
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content="Shodan rejected the API key (401/403) — check it "
                        "in the key store.")
        try:
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content=f"search failed: {exc}")
        matches = (payload.get("matches") or [])[:limit]
        total = payload.get("total", "?")
        lines = [f"## Shodan search — `{query}`",
                 f"**total ≈ {total:,}** · page {page} · showing "
                 f"{len(matches)}\n"]
        if not matches:
            lines.append("No matches on this page — try page 1 or a "
                         "broader query.")
        else:
            lines.append("| IP | Port | Product | Org | Country |")
            lines.append("|---|---|---|---|---|")
            for m in matches:
                loc = m.get("location") or {}
                product = " ".join(x for x in (m.get("product"),
                                               m.get("version")) if x) or "—"
                lines.append(
                    f"| {m.get('ip_str') or m.get('ip')} | {m.get('port')} "
                    f"| {product[:30]} | {(m.get('org') or '—')[:28]} "
                    f"| {(loc.get('country_name') or '—')[:18]} |")
            lines.append("\n*Note: search consumes 1 query credit per page*")
        return ToolResult(
            tool_name="osint_shodan", success=True,
            content="\n".join(lines),
            metadata={"query": query, "total": total,
                      "results": [
                          {"ip": m.get("ip_str"), "port": m.get("port"),
                           "org": m.get("org")} for m in matches]})

    def _profile(self, **params: Any) -> ToolResult:
        api_key = resolve_shodan_key()
        if not api_key:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content=("profile requires shodan_api_key — save it via "
                         "POST /api/creative/keys/shodan_api_key or "
                         "export SHODAN_API_KEY."))
        cfg = _shodan_settings()
        resp = httpx.get(
            f"{cfg['api_base'].rstrip('/')}/account/profile",
            params={"key": api_key}, headers=_HEADERS,
            timeout=float(cfg["timeout_s"]))
        if resp.status_code in (401, 403):
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content="Shodan rejected the API key (401/403).")
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return ToolResult(
                tool_name="osint_shodan", success=False,
                content=f"profile failed: {exc}")
        credits = data.get("credits") or {}
        lines = ["## Shodan account",
                 f"**Member:** {data.get('member', '—')} · "
                 f"**Plan:** {data.get('plan', '—')}",
                 f"Credits — query: {credits.get('query', '—')} · "
                 f"scan: {credits.get('scan', '—')} · "
                 f"monitor: {credits.get('monitor', '—')}",
                 f"Created: {str(data.get('created', '—'))[:10]}"]
        return ToolResult(
            tool_name="osint_shodan", success=True,
            content="\n".join(lines), metadata={"profile": data})

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "lookup").strip().lower()
        try:
            if action == "search":
                return self._search(**params)
            if action == "profile":
                return self._profile(**params)
            return self._lookup(**params)
        except Exception as exc:
            logger.warning("osint_shodan failed: %s", exc)
            return ToolResult(tool_name="osint_shodan", success=False,
                              content=f"osint_shodan error: {exc}"[:500])


__all__ = ["OsintShodanTool", "lookup_ip", "internetdb_lookup",
           "full_api_host", "assess_risk", "resolve_shodan_key",
           "_validate_ip", "_shodan_settings"]

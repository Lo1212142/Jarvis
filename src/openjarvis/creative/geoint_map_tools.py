"""GEOINT tools 2/3 + 3/3 — infrastructure mapping + satellite imagery.

* ``osint_map`` — queries OpenStreetMap through the **Overpass API**
  (no key, free) for the physical infrastructure around any point or
  bbox: telecom towers/masts, fuel stations, power plants/substations,
  surveillance cameras, transport, water, emergency, health, military,
  aviation and industrial assets — with distance/bearing from the target
  point, multiple endpoint failover and a small response cache.

* ``osint_satellite`` — free satellite imagery with zero API keys:

  - ``action=search`` — Sentinel-2 scene catalogue via the Copernicus
    Data Space **OData API** (public, no auth): dates, cloud cover,
    tile, product id/size/online status for any bbox + date range.
  - ``action=image`` — a true-colour snapshot of any bbox on any date
    via **NASA GIBS** (MODIS Terra/Aqua, VIIRS S2/NOAA-20; 2000→now).
  - ``action=compare`` — **before/after** composite (side-by-side or an
    animated flicker GIF) of the same bbox on two dates — flood, fire,
    construction, land-use change evidence.
  - ``action=download`` — optional full Sentinel-2 product download when
    the user saved free Copernicus credentials in the creative key store
    (provider keys ``copernicus_username`` / ``copernicus_password``).
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths, media_settings

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "openjarvis-geoint/1.0"}

# ---------------------------------------------------------------------------
# Settings (section "geoint" in media-settings.json — optional overrides)
# ---------------------------------------------------------------------------

_GEOINT_DEFAULTS: Dict[str, Any] = {
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


def _geoint_settings() -> Dict[str, Any]:
    try:
        stored = media_settings.load_settings().get("geoint", {}) or {}
    except Exception:
        stored = {}
    merged = dict(_GEOINT_DEFAULTS)
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if v})
    return merged


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x))) % 360


def _compass(bearing: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((bearing % 360) + 22.5) // 45) % 8]


def _bbox_from_point(lat: float, lon: float, size_km: float) -> List[float]:
    dlat = size_km / 111.32
    dlon = size_km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return [round(lon - dlon / 2, 5), round(lat - dlat / 2, 5),
            round(lon + dlon / 2, 5), round(lat + dlat / 2, 5)]


# ---------------------------------------------------------------------------
# osint_map — OpenStreetMap Overpass
# ---------------------------------------------------------------------------

# category -> (Arabic label, [Overpass tag selectors])
CATEGORY_FILTERS: Dict[str, Tuple[str, List[str]]] = {
    "telecom": ("اتصالات — أبراج ومقسمات", [
        '["man_made"="mast"]', '["man_made"="communications_tower"]',
        '["man_made"="tower"]["tower:type"~"commun"]', '["telecom"="exchange"]',
    ]),
    "fuel": ("محطات ومخازن وقود", [
        '["amenity"="fuel"]', '["man_made"="storage_tank"]["content"~"oil|fuel"]',
        '["landuse"="industrial"]["industrial"="oil"]',
    ]),
    "surveillance": ("كاميرات مراقبة", [
        '["man_made"="surveillance"]', '["highway"="street_lamp"]["camera"]',
    ]),
    "power": ("كهرباء — محطات ومحولات", [
        '["power"="plant"]', '["power"="substation"]',
        '["power"="generator"]', '["power"="transformer"]',
    ]),
    "transport": ("محطات نقل", [
        '["railway"="station"]', '["railway"="halt"]', '["railway"="tram_stop"]',
        '["amenity"="bus_station"]', '["amenity"="ferry_terminal"]',
        '["railway"="yard"]',
    ]),
    "water": ("مياه — خزانات ومحطات", [
        '["man_made"="water_tower"]', '["man_made"="water_works"]',
        '["man_made"="reservoir_covered"]', '["amenity"="water_point"]',
    ]),
    "emergency": ("خدمات طوارئ", [
        '["amenity"="fire_station"]', '["amenity"="police"]',
        '["amenity"="emergency_service"]',
    ]),
    "health": ("مرافق صحية", [
        '["amenity"="hospital"]', '["amenity"="clinic"]',
        '["amenity"="doctors"]',
    ]),
    "military": ("منشآت عسكرية", [
        '["military"]', '["landuse"="military"]', '["building"="military"]',
    ]),
    "aviation": ("طيران", [
        '["aeroway"="aerodrome"]', '["aeroway"="helipad"]',
        '["aeroway"="terminal"]', '["aeroway"="runway"]',
    ]),
    "industrial": ("مناطق صناعية", [
        '["landuse"="industrial"]', '["man_made"="works"]',
        '["man_made"="chimney"]', '["industrial"]',
    ]),
}

_OVERPASS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def _build_overpass_query(
    lat: Optional[float], lon: Optional[float], radius_m: int,
    bbox: Optional[List[float]], categories: List[str],
) -> str:
    selectors: List[str] = []
    for cat in categories:
        for tag_filter in CATEGORY_FILTERS[cat][1]:
            if bbox:
                box = f"({bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]})"
                selectors.append(f"nwr{tag_filter}{box};")
            else:
                selectors.append(
                    f"nwr{tag_filter}(around:{radius_m},{lat},{lon});")
    timeout = _geoint_settings()["overpass_timeout_s"]
    return (f"[out:json][timeout:{timeout}];(" + "".join(selectors) +
            ");out center tags;")


def _overpass_fetch(query: str) -> List[Dict[str, Any]]:
    cfg = _geoint_settings()
    key = query
    now = time.time()
    hit = _OVERPASS_CACHE.get(key)
    if hit and now - hit[0] < cfg["cache_ttl_s"]:
        return hit[1]
    last_error: Optional[str] = None
    for endpoint in cfg["overpass_endpoints"]:
        try:
            resp = httpx.post(
                endpoint, data={"data": query}, headers=_HEADERS,
                timeout=cfg["overpass_timeout_s"] + 15.0,
                follow_redirects=True)
            resp.raise_for_status()
            elements = resp.json().get("elements", []) or []
            _OVERPASS_CACHE[key] = (now, elements)
            return elements
        except Exception as exc:
            last_error = f"{endpoint}: {exc}"
            logger.debug("overpass failover: %s", last_error)
            continue
    raise RuntimeError(f"all Overpass endpoints failed (last: {last_error})")


def _element_coords(element: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    if element.get("type") == "node":
        return element.get("lat"), element.get("lon")
    center = element.get("center") or {}
    return center.get("lat"), center.get("lon")


@ToolRegistry.register("osint_map")
class OsintMapTool(BaseTool):
    """OpenStreetMap infrastructure intelligence (Overpass, key-free)."""

    tool_id = "osint_map"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="osint_map",
            description=(
                "Infrastructure intelligence from OpenStreetMap (Overpass "
                "API — free, no key). Given a point+radius (or a bbox), it "
                "maps the physical assets around it: telecom towers/masts, "
                "fuel stations & depots, surveillance cameras, power "
                "plants/substations, transport hubs, water works, emergency "
                "services, health facilities, military sites, aviation and "
                "industrial zones — each with name, type, distance and "
                "compass bearing from the target, plus key tags (operator, "
                "height, voltage, frequency…). Great for site assessment, "
                "attack-surface mapping of one's own assets, or area "
                "reconnaissance from public data."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Target latitude."},
                    "lon": {"type": "number", "description": "Target longitude."},
                    "radius_km": {
                        "type": "number", "default": 3, "maximum": 50,
                        "description": "Search radius in km (default 3, max 50).",
                    },
                    "bbox": {
                        "type": "array", "items": {"type": "number"},
                        "description": "Alternative to lat/lon/radius: "
                                       "[lon_min, lat_min, lon_max, lat_max].",
                    },
                    "categories": {
                        "type": "string",
                        "default": "all",
                        "description": "Comma list from: telecom, fuel, "
                                       "surveillance, power, transport, water, "
                                       "emergency, health, military, aviation, "
                                       "industrial — or 'all'.",
                    },
                },
            },
            category="geoint",
            timeout_seconds=180.0,
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            cfg = _geoint_settings()
            lat = params.get("lat")
            lon = params.get("lon")
            bbox = params.get("bbox")
            if bbox:
                try:
                    bbox = [float(v) for v in bbox][:4]
                    if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                        raise ValueError
                except (TypeError, ValueError):
                    return ToolResult(
                        tool_name="osint_map", success=False,
                        content="bbox must be [lon_min, lat_min, lon_max, "
                                "lat_max] with valid bounds.")
                lat = lon = None
            else:
                try:
                    lat = float(lat)
                    lon = float(lon)
                except (TypeError, ValueError):
                    return ToolResult(
                        tool_name="osint_map", success=False,
                        content="Provide lat/lon (+radius_km) or a bbox.")
            radius_km = float(params.get("radius_km") or 3)
            radius_km = min(max(radius_km, 0.2), 50)
            radius_m = int(radius_km * 1000)

            wanted = str(params.get("categories") or "all").strip().lower()
            if wanted in ("all", "*", ""):
                categories = list(CATEGORY_FILTERS)
            else:
                categories = []
                for name in wanted.split(","):
                    name = name.strip()
                    if name in CATEGORY_FILTERS:
                        categories.append(name)
                if not categories:
                    return ToolResult(
                        tool_name="osint_map", success=False,
                        content=f"Unknown categories '{wanted}'. Valid: "
                                + ", ".join(CATEGORY_FILTERS))

            query = _build_overpass_query(lat, lon, radius_m, bbox, categories)
            elements = _overpass_fetch(query)

            # classify
            per_category: Dict[str, List[Dict[str, Any]]] = {c: [] for c in categories}
            for element in elements:
                tags = element.get("tags") or {}
                elat, elon = _element_coords(element)
                if elat is None or elon is None:
                    continue
                entry = {
                    "type": element.get("type"),
                    "name": tags.get("name"),
                    "tags": tags,
                    "lat": elat,
                    "lon": elon,
                }
                if lat is not None:
                    entry["distance_km"] = round(
                        haversine_km(lat, lon, elat, elon), 2)
                    b = bearing_deg(lat, lon, elat, elon)
                    entry["bearing"] = round(b, 0)
                    entry["direction"] = _compass(b)
                blob = " ".join(f"{k}={v}" for k, v in tags.items())
                raw = element.get("tags", {})
                matched = False
                for cat in categories:
                    for tag_filter in CATEGORY_FILTERS[cat][1]:
                        m = re.match(r'\["([^"]+)"(?:="([^"]+)")?\]', tag_filter)
                        if not m:
                            continue
                        key, value = m.group(1), m.group(2)
                        if key in raw and (value is None or raw[key] == value):
                            per_category[cat].append(entry)
                            matched = True
                            break
                    if matched:
                        break

            # report
            centre = f"{lat:.5f}, {lon:.5f}" if lat is not None else (
                f"bbox {bbox}")
            lines = [f"## Infrastructure map — {centre}",
                     f"Radius: {radius_km} km · Source: OpenStreetMap/Overpass "
                     f"({len(elements)} raw objects)"]
            total = sum(len(v) for v in per_category.values())
            lines.append(f"**Matched features: {total}**\n")
            lines.append("| Category | Count |")
            lines.append("|---|---|")
            for cat in categories:
                lines.append(f"| {cat} ({CATEGORY_FILTERS[cat][0]}) "
                             f"| {len(per_category[cat])} |")
            max_per = int(cfg["max_features_per_category"])
            for cat in categories:
                items = per_category[cat]
                if not items:
                    continue
                items.sort(key=lambda it: it.get("distance_km", 1e9))
                lines.append(f"\n### {CATEGORY_FILTERS[cat][0]} ({cat})")
                if lat is not None:
                    lines.append("| Feature | Distance | Direction | Tags |")
                    lines.append("|---|---|---|---|")
                    for it in items[:max_per]:
                        name = it["name"] or f"({it['type']})"
                        dist = f"{it['distance_km']} km"
                        direction = f"{it['bearing']}° {it['direction']}"
                        keys = ("operator", "height", "voltage", "frequency",
                                "operator:type", "tower:type", "content",
                                "power", "industrial", "military", "usage",
                                "surveillance", "ref")
                        extra = "; ".join(f"{k}={it['tags'][k]}"
                                          for k in keys if k in it["tags"])
                        lines.append(f"| {name} | {dist} | {direction} "
                                     f"| {extra[:120]} |")
                else:
                    lines.append("| Feature | Coordinates | Tags |")
                    lines.append("|---|---|---|")
                    for it in items[:max_per]:
                        name = it["name"] or f"({it['type']})"
                        coords = f"{it['lat']:.5f}, {it['lon']:.5f}"
                        keys = ("operator", "height", "voltage", "military",
                                "tower:type", "power")
                        extra = "; ".join(f"{k}={it['tags'][k]}"
                                          for k in keys if k in it["tags"])
                        lines.append(f"| {name} | {coords} | {extra[:120]} |")
                if len(items) > max_per:
                    lines.append(f"\n… and {len(items) - max_per} more "
                                 f"(raise max_features_per_category in "
                                 f"settings/geoint).")
            return ToolResult(
                tool_name="osint_map", content="\n".join(lines), success=True,
                metadata={"query": query, "centre": centre,
                          "radius_km": radius_km,
                          "categories": {c: len(v)
                                         for c, v in per_category.items()},
                          "total": total},
            )
        except Exception as exc:
            logger.warning("osint_map failed: %s", exc)
            return ToolResult(tool_name="osint_map", success=False,
                              content=f"osint_map error: {exc}"[:500])


__all__ = ["OsintMapTool", "haversine_km", "bearing_deg",
           "CATEGORY_FILTERS", "_build_overpass_query"]

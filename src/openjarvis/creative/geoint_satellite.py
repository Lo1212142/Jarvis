"""GEOINT tool 3/3 — satellite imagery: ``osint_satellite``.

Zero-key pipeline (both endpoints are public):

* **Search** — Copernicus Data Space OData catalogue: Sentinel-2
  L1C/L2A scenes over any bbox + date range + max cloud cover
  (metadata only — no auth needed).
* **Image** — NASA GIBS WMS true-colour snapshot of any bbox on any
  date (MODIS Terra 2000→, Aqua 2002→, VIIRS SNPP 2015→, NOAA-20
  2018→; ~250m/px).
* **Compare** — before/after evidence: side-by-side composite or an
  animated flicker GIF of two dates over the same bbox.
* **Download** — full Sentinel-2 product ZIP; optional, requires the
  user's free Copernicus credentials saved in the creative key store
  (``POST /api/creative/keys/copernicus_username`` and
  ``.../copernicus_password``) — the token flow is implemented
  (openid-connect, CDSE public client).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths, media_settings
from openjarvis.creative.geoint_map_tools import (
    _bbox_from_point,
    _geoint_settings,
)

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "openjarvis-geoint/1.0"}

GIBS_LAYERS: Dict[str, str] = {
    "modis_terra": "MODIS_Terra_CorrectedReflectance_TrueColor",
    "modis_aqua": "MODIS_Aqua_CorrectedReflectance_TrueColor",
    "viirs_snpp": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "viirs_noaa20": "VIIRS_NOAA20_CorrectedReflectance_TrueColor",
    "modis_721": "MODIS_Terra_CorrectedReflectance_Bands721",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Copernicus OData catalogue
# ---------------------------------------------------------------------------


def _od(s: str) -> str:
    """Percent-encode an OData filter value fully (spaces, slashes …)."""
    from urllib.parse import quote

    return quote(s, safe="()=':,;/")


def search_sentinel2(
    bbox: List[float],
    date_from: str,
    date_to: str,
    *,
    product_type: str = "L2A",
    max_cloud: int = 40,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Query the public Copernicus OData catalogue (no auth).

    Cloud cover is filtered client-side from the expanded attributes
    (the server-side attribute lambda was retired by the API).
    """
    cfg = _geoint_settings()
    poly = (f"POLYGON(({bbox[0]} {bbox[1]},{bbox[2]} {bbox[1]},"
            f"{bbox[2]} {bbox[3]},{bbox[0]} {bbox[3]},{bbox[0]} {bbox[1]}))")
    filters = [
        "Collection/Name eq 'SENTINEL-2'",
        "OData.CSC.Intersects(area=geography'SRID=4326;" + poly + "')",
        f"ContentDate/Start gt {date_from}T00:00:00.000Z",
        f"ContentDate/Start lt {date_to}T23:59:59.999Z",
    ]
    if product_type.upper() in ("L1C", "L2A"):
        filters.append(f"contains(Name,'MSI{product_type.upper()}')")
    flt = " and ".join(filters)
    fetch_n = min(max(int(limit) * 4, 40), 100)
    url = (f"{cfg['odata_base']}?$top={fetch_n}"
           f"&$expand=Attributes"
           f"&$orderby={_od('ContentDate/Start desc')}&$filter={_od(flt)}")
    resp = httpx.get(url, headers=_HEADERS, timeout=60.0)
    resp.raise_for_status()
    products = resp.json().get("value", []) or []

    scenes: List[Dict[str, Any]] = []
    for product in products:
        attrs = {a.get("Name"): a.get("Value")
                 for a in product.get("Attributes", []) or []}
        try:
            cloud = float(attrs.get("cloudCover", "100"))
        except (TypeError, ValueError):
            cloud = 100.0
        if max_cloud is not None and cloud > max_cloud:
            continue
        name = product.get("Name", "")
        tile = attrs.get("tileId")
        tile_m = re.search(r"_T(\d{2}[A-Z]{3})_", name)
        date_m = re.search(r"_MSIL\d[AC]_(\d{8})T\d{6}", name)
        scenes.append({
            "product_id": product.get("Id"),
            "name": name,
            "tile": tile or (tile_m.group(1) if tile_m else None),
            "cloud_cover": round(cloud, 1),
            "sensing_date": (
                datetime.strptime(date_m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
                if date_m else product.get("ContentDate", {}).get("Start", "")[:10]),
            "content_date": product.get("ContentDate", {}).get("Start", "")[:19],
            "size_mb": round((product.get("ContentLength") or 0) / 1e6, 1),
            "online": product.get("Online", False),
        })
        if len(scenes) >= int(limit):
            break
    return scenes


# ---------------------------------------------------------------------------
# NASA GIBS WMS
# ---------------------------------------------------------------------------


def fetch_gibs_image(
    bbox: List[float], date: str, layer: str = "modis_terra",
    width: int = 1024,
) -> tuple[Path, int, int]:
    """Fetch a GIBS WMS GetMap frame; returns (path, width, height)."""
    cfg = _geoint_settings()
    if layer not in GIBS_LAYERS:
        raise ValueError(f"unknown layer '{layer}' — choose from: "
                         + ", ".join(GIBS_LAYERS))
    if not _DATE_RE.match(date):
        raise ValueError(f"date must be YYYY-MM-DD, got '{date}'")
    width = min(max(int(width), 256), 1024)
    lon_span = bbox[2] - bbox[0]
    lat_span = bbox[3] - bbox[1]
    height = max(256, min(1024, int(round(width * lat_span / max(1e-9, lon_span)))))
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": GIBS_LAYERS[layer], "SRS": "EPSG:4326",
        "BBOX": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
        "WIDTH": str(width), "HEIGHT": str(height),
        "FORMAT": "image/jpeg", "TIME": date,
    }
    resp = httpx.get(cfg["gibs_wms"], params=params, headers=_HEADERS,
                     timeout=90.0)
    resp.raise_for_status()
    content = resp.content
    if not content.startswith(b"\xff\xd8"):
        snippet = content[:200].decode("utf-8", "replace")
        raise RuntimeError(f"GIBS returned a non-image response (bad date or "
                           f"layer?): {snippet}")
    stem = f"satellite-{layer}-{date}-"
    stem += f"{bbox[0]:.2f}-{bbox[1]:.2f}-{bbox[2]:.2f}-{bbox[3]:.2f}"
    out = _paths.new_media_path("image", "jpg", stem=stem.replace(".", "-"))
    out.write_bytes(content)
    return out, width, height


def _label_composite(img_before: "Any", img_after: "Any",
                     date_before: str, date_after: str) -> "Any":
    """Side-by-side composite with date labels (Pillow)."""
    from PIL import Image, ImageDraw

    from openjarvis.creative import text_render

    gap = 12
    bar = 44
    width = img_before.width + img_after.width + gap
    height = max(img_before.height, img_after.height) + bar
    canvas = Image.new("RGB", (width, height), (12, 12, 14))
    canvas.paste(img_before, (0, bar))
    canvas.paste(img_after, (img_before.width + gap, bar))
    draw = ImageDraw.Draw(canvas)
    try:
        font = text_render._load_font(22, bold=True)
    except Exception:
        font = None
    for text, x in ((f"BEFORE {date_before}", 10),
                    (f"AFTER {date_after}", img_before.width + gap + 10)):
        draw.rectangle([x, 8, x + 320, bar - 6], fill=(20, 20, 24))
        draw.text((x + 8, 12), text, fill=(240, 240, 240), font=font)
    return canvas


def _flicker_gif(img_before: "Any", img_after: "Any",
                 date_before: str, date_after: str) -> Path:
    """Animated before/after flicker GIF."""
    from PIL import Image, ImageDraw

    from openjarvis.creative import text_render

    bar = 40
    frames = []
    for label, img in ((f"BEFORE  {date_before}", img_before),
                       (f"AFTER   {date_after}", img_after)):
        canvas = Image.new("RGB", (img.width, img.height + bar), (12, 12, 14))
        canvas.paste(img, (0, bar))
        draw = ImageDraw.Draw(canvas)
        try:
            font = text_render._load_font(22, bold=True)
        except Exception:
            font = None
        draw.text((10, 8), label, fill=(240, 240, 240), font=font)
        frames.append(canvas)
    out = _paths.new_media_path("image", "gif",
                                stem=f"satellite-flicker-{date_before}-vs-{date_after}")
    frames[0].save(out, save_all=True, append_images=frames[1:],
                   duration=1100, loop=0)
    return out


# ---------------------------------------------------------------------------
# Copernicus download (optional, needs free credentials)
# ---------------------------------------------------------------------------


def _copernicus_token() -> str:
    keys = {}
    try:
        for name in ("copernicus_username", "copernicus_password"):
            keys[name] = media_settings.resolve_api_key(
                {"api_key_env": ""}, name)
    except Exception:
        pass
    username, password = (keys.get("copernicus_username") or "",
                          keys.get("copernicus_password") or "")
    if not username or not password:
        raise ValueError(
            "Copernicus download needs free CDSE credentials — save them "
            "once: POST /api/creative/keys/copernicus_username and "
            "/api/creative/keys/copernicus_password (register free at "
            "https://dataspace.copernicus.eu)")
    cfg = _geoint_settings()
    resp = httpx.post(
        cfg["identity_token_url"],
        data={"username": username, "password": password,
              "grant_type": "password", "client_id": "CDSE-public"},
        timeout=30.0)
    resp.raise_for_status()
    token = resp.json().get("access_token", "")
    if not token:
        raise RuntimeError("Copernicus auth succeeded but no access token")
    return token


def download_product(product_id: str, max_bytes: int = 2_000_000_000) -> Path:
    """Stream-download a full product ZIP (requires credentials)."""
    cfg = _geoint_settings()
    token = _copernicus_token()
    url = f"{cfg['odata_base']}({product_id})/$value"
    out = _paths.projects_dir() / f"s2-product-{product_id[:8]}.zip"
    downloaded = 0
    with httpx.stream("GET", url, headers={
            "Authorization": f"Bearer {token}", **_HEADERS},
            timeout=1800.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in resp.iter_bytes(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    fh.close()
                    out.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"product exceeds max_bytes ({max_bytes / 1e6:.0f} MB)")
                fh.write(chunk)
    return out


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@ToolRegistry.register("osint_satellite")
class OsintSatelliteTool(BaseTool):
    """Free satellite imagery: S2 catalogue + GIBS visuals + before/after."""

    tool_id = "osint_satellite"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="osint_satellite",
            description=(
                "Satellite intelligence, free and key-free. action=search "
                "lists Sentinel-2 scenes over an area/date range (Copernicus "
                "catalogue — cloud cover, tile, product id). action=image "
                "returns a true-colour satellite snapshot of any place on "
                "any date (NASA GIBS: MODIS/VIIRS, 2000→today, ~250m/px). "
                "action=compare builds a before/after composite (or "
                "flicker GIF) of two dates — evidence for floods, fires, "
                "construction, land-use change. action=download fetches a "
                "full Sentinel-2 product (needs free Copernicus credentials "
                "saved in the key store)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "default": "search",
                        "enum": ["search", "image", "compare", "download"],
                    },
                    "lat": {"type": "number", "description": "Centre latitude."},
                    "lon": {"type": "number", "description": "Centre longitude."},
                    "size_km": {
                        "type": "number", "default": 20,
                        "description": "Area size (km) when using lat/lon "
                                       "instead of bbox (default 20).",
                    },
                    "bbox": {
                        "type": "array", "items": {"type": "number"},
                        "description": "[lon_min, lat_min, lon_max, lat_max] "
                                       "override.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "search: range start 'YYYY-MM-DD'.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "search: range end 'YYYY-MM-DD'.",
                    },
                    "max_cloud": {
                        "type": "integer", "default": 40,
                        "description": "search: max cloud cover %.",
                    },
                    "product_type": {
                        "type": "string", "enum": ["L2A", "L1C"],
                        "default": "L2A",
                    },
                    "limit": {"type": "integer", "default": 10},
                    "date": {
                        "type": "string",
                        "description": "image: acquisition date 'YYYY-MM-DD'.",
                    },
                    "layer": {
                        "type": "string", "default": "modis_terra",
                        "description": "image/compare: modis_terra | "
                                       "modis_aqua | viirs_snpp | "
                                       "viirs_noaa20 | modis_721.",
                    },
                    "width": {"type": "integer", "default": 1024},
                    "date_before": {
                        "type": "string",
                        "description": "compare: first date 'YYYY-MM-DD'.",
                    },
                    "date_after": {
                        "type": "string",
                        "description": "compare: second date 'YYYY-MM-DD'.",
                    },
                    "animated": {
                        "type": "boolean", "default": False,
                        "description": "compare: flicker GIF instead of "
                                       "side-by-side.",
                    },
                    "product_id": {
                        "type": "string",
                        "description": "download: product Id from search.",
                    },
                    "max_bytes": {
                        "type": "integer", "default": 2000000000,
                        "description": "download: size cap in bytes.",
                    },
                },
            },
            category="geoint",
            timeout_seconds=600.0,
            required_capabilities=["network:fetch"],
        )

    def _resolve_bbox(self, **params: Any) -> List[float]:
        bbox = params.get("bbox")
        if bbox:
            values = [float(v) for v in bbox][:4]
            if values[0] < values[2] and values[1] < values[3]:
                return values
        lat, lon = params.get("lat"), params.get("lon")
        if lat is None or lon is None:
            raise ValueError("Provide lat/lon (+size_km) or a bbox "
                             "[lon_min, lat_min, lon_max, lat_max]")
        size_km = float(params.get("size_km") or 20)
        return _bbox_from_point(float(lat), float(lon), size_km)

    # -- actions ------------------------------------------------------------

    def _search(self, **params: Any) -> ToolResult:
        bbox = self._resolve_bbox(**params)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = str(params.get("date_from") or today)
        date_to = str(params.get("date_to") or today)
        for name, value in (("date_from", date_from), ("date_to", date_to)):
            if not _DATE_RE.match(value):
                return ToolResult(
                    tool_name="osint_satellite", success=False,
                    content=f"{name} must be YYYY-MM-DD, got '{value}'")
        scenes = search_sentinel2(
            bbox, date_from, date_to,
            product_type=str(params.get("product_type") or "L2A"),
            max_cloud=int(params.get("max_cloud") or 40),
            limit=int(params.get("limit") or 10))
        lines = [f"## Sentinel-2 scenes — bbox {bbox}",
                 f"{date_from} → {date_to} · cloud < "
                 f"{int(params.get('max_cloud') or 40)}% · "
                 f"level {str(params.get('product_type') or 'L2A').upper()}",
                 f"**{len(scenes)} scenes**\n"]
        if scenes:
            lines.append("| Date | Tile | Cloud % | Size MB | Online | Product ID |")
            lines.append("|---|---|---|---|---|---|")
            for scene in scenes:
                lines.append(
                    f"| {scene['sensing_date']} | {scene['tile']} | "
                    f"{scene.get('cloud_cover', '—')} | "
                    f"{scene['size_mb']} | {'✓' if scene['online'] else '—'} "
                    f"| `{str(scene['product_id'])[:13]}…` |")
            lines.append(
                "\n*Full product ids are in metadata.scenes — pass one to "
                "action=download (needs free Copernicus credentials).*")
        else:
            lines.append("No scenes matched — widen the date range, raise "
                         "max_cloud or enlarge the bbox.")
        return ToolResult(
            tool_name="osint_satellite", content="\n".join(lines),
            success=True, metadata={"scenes": scenes, "bbox": bbox})

    def _image(self, **params: Any) -> ToolResult:
        bbox = self._resolve_bbox(**params)
        date = str(params.get("date") or "")
        if not _DATE_RE.match(date):
            return ToolResult(
                tool_name="osint_satellite", success=False,
                content=f"image action needs date='YYYY-MM-DD', got '{date}'")
        layer = str(params.get("layer") or "modis_terra")
        path, width, height = fetch_gibs_image(
            bbox, date, layer=layer, width=int(params.get("width") or 1024))
        url = _paths.media_url(path)
        return ToolResult(
            tool_name="osint_satellite",
            content=(f"## Satellite snapshot\n![{layer} {date}]({url})\n"
                     f"**Layer:** {layer} ({GIBS_LAYERS[layer]}) · "
                     f"**Date:** {date} · **BBox:** {bbox} · "
                     f"{width}×{height}px"),
            success=True,
            metadata={"path": str(path), "url": url, "bbox": bbox,
                      "layer": layer, "date": date})

    def _compare(self, **params: Any) -> ToolResult:
        bbox = self._resolve_bbox(**params)
        date_before = str(params.get("date_before") or "")
        date_after = str(params.get("date_after") or "")
        for name, value in (("date_before", date_before),
                            ("date_after", date_after)):
            if not _DATE_RE.match(value):
                return ToolResult(
                    tool_name="osint_satellite", success=False,
                    content=f"compare needs {name}='YYYY-MM-DD', got "
                            f"'{value}'")
        layer = str(params.get("layer") or "modis_terra")
        width = int(params.get("width") or 900)
        animated = bool(params.get("animated"))
        path_b, _, _ = fetch_gibs_image(bbox, date_before, layer=layer,
                                        width=width)
        path_a, _, _ = fetch_gibs_image(bbox, date_after, layer=layer,
                                        width=width)
        from PIL import Image

        img_b = Image.open(path_b).convert("RGB")
        img_a = Image.open(path_a).convert("RGB")
        if animated:
            out = _flicker_gif(img_b, img_a, date_before, date_after)
            url = _paths.media_url(out)
            return ToolResult(
                tool_name="osint_satellite",
                content=(f"## Before / After (animated)\n![flicker]({url})\n"
                         f"**{date_before} → {date_after}** · {layer} · "
                         f"bbox {bbox}"),
                success=True,
                metadata={"path": str(out), "url": url, "bbox": bbox,
                          "dates": [date_before, date_after]})
        composite = _label_composite(img_b, img_a, date_before, date_after)
        out = _paths.new_media_path(
            "image", "png", stem=f"satellite-compare-{date_before}-vs-{date_after}")
        from openjarvis.creative.text_render import save_png

        save_png(composite, out)
        url = _paths.media_url(out)
        return ToolResult(
            tool_name="osint_satellite",
            content=(f"## Before / After\n![compare]({url})\n"
                     f"**{date_before} → {date_after}** · {layer} · bbox "
                     f"{bbox} — pass animated=true for a flicker GIF"),
            success=True,
            metadata={"path": str(out), "url": url, "bbox": bbox,
                      "dates": [date_before, date_after]})

    def _download(self, **params: Any) -> ToolResult:
        product_id = str(params.get("product_id") or "").strip()
        if not product_id:
            return ToolResult(
                tool_name="osint_satellite", success=False,
                content="download needs product_id — run action=search first.")
        out = download_product(product_id,
                               max_bytes=int(params.get("max_bytes")
                                             or 2_000_000_000))
        return ToolResult(
            tool_name="osint_satellite",
            content=f"Product downloaded: `{out}` "
                    f"({out.stat().st_size / 1e6:.0f} MB)",
            success=True, metadata={"path": str(out)})

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "search").strip().lower()
        try:
            if action == "image":
                return self._image(**params)
            if action == "compare":
                return self._compare(**params)
            if action == "download":
                return self._download(**params)
            return self._search(**params)
        except Exception as exc:
            logger.warning("osint_satellite failed: %s", exc)
            return ToolResult(tool_name="osint_satellite", success=False,
                              content=f"osint_satellite error: {exc}"[:600])


__all__ = ["OsintSatelliteTool", "search_sentinel2", "fetch_gibs_image",
           "GIBS_LAYERS", "download_product"]

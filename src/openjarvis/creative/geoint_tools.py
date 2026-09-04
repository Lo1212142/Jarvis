"""GEOINT tool 1/3 — image forensics: ``osint_image``.

Extracts full photo metadata (GPS / camera / serials / timestamps) via
the real **ExifTool** binary when available (richest: images, videos,
PDFs, MakerNotes) with a pure-Pillow fallback (JPEG/TIFF), then adds the
**SunCalc cross-check**: computes the sun position for the claimed
time+place and verifies it against the shadow geometry visible in the
photo (shadow length ratio + compass bearing), exposing timestamps that
are physically impossible — the classic technique for spotting fake or
re-stamped photos.

No API keys, fully local (metadata) + pure math (shadows).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from openjarvis.creative import _paths
from openjarvis.creative._sun_calc import (
    compass_name,
    match_shadow,
    shadow_geometry,
    sun_events,
    sun_position,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExifTool binary discovery
# ---------------------------------------------------------------------------


def exiftool_command() -> Optional[List[str]]:
    """Return the exiftool invocation (argv) or None.

    Resolution: ``shutil.which('exiftool')`` → ``JARVIS_EXIFTOOL`` env
    (full path, or e.g. ``perl /path/to/exiftool``).
    """
    found = shutil.which("exiftool")
    if found:
        return [found]
    env = os.environ.get("JARVIS_EXIFTOOL", "").strip()
    if env:
        parts = shlex.split(env)
        if parts:
            return parts
    return None


def exiftool_backend() -> str:
    return "exiftool" if exiftool_command() else "pillow"


def _run_exiftool(path: Path) -> Optional[Dict[str, Any]]:
    """Run ``exiftool -j -n -G1`` and return the first record (or None)."""
    cmd = exiftool_command()
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            cmd + ["-j", "-n", "-G1", "-q", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    import json

    try:
        records = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return records[0] if records else None


# Curated fields: exiftool (-G1) key -> canonical name.
# Group names follow exiftool's -G1 families (GPS / ExifIFD / IFD0 / …).
_EXIFTOOL_FIELDS = {
    "ExifIFD:DateTimeOriginal": "date_taken",
    "ExifIFD:DateTimeDigitized": "date_digitized",
    "ExifIFD:CreateDate": "date_digitized",
    "ExifIFD:ModifyDate": "date_modified_exif",
    "IFD0:ModifyDate": "date_modified_ifd0",
    "File:FileModifyDate": "date_file_modified",
    "IFD0:Make": "make",
    "IFD0:Model": "model",
    "IFD0:Software": "software",
    "ExifIFD:BodySerialNumber": "serial_number",
    "ExifIFD:SerialNumber": "serial_number",
    "MakerNotes:SerialNumber": "serial_number_makernotes",
    "MakerNotes:InternalSerialNumber": "serial_internal",
    "ExifIFD:LensMake": "lens_make",
    "ExifIFD:LensModel": "lens_model",
    "ExifIFD:LensSerialNumber": "lens_serial",
    "ExifIFD:FocalLength": "focal_length_mm",
    "ExifIFD:FNumber": "f_number",
    "ExifIFD:ExposureTime": "exposure_time_s",
    "ExifIFD:ISO": "iso",
    "ExifIFD:Flash": "flash",
    "IFD0:Orientation": "orientation",
    "GPS:GPSLatitude": "gps_lat",
    "GPS:GPSLongitude": "gps_lon",
    "GPS:GPSAltitude": "gps_altitude_m",
    "GPS:GPSTimeStamp": "gps_time",
    "Composite:GPSPosition": "_gps_position",
    "QuickTime:CreateDate": "date_taken",
    "QuickTime:ModifyDate": "date_modified_exif",
    "PDF:CreateDate": "date_taken",
}


# ---------------------------------------------------------------------------
# Pillow fallback (JPEG/TIFF)
# ---------------------------------------------------------------------------

_GPS_LAT = 2
_GPS_LAT_REF = 1
_GPS_LON = 4
_GPS_LON_REF = 3
_GPS_ALT = 6


def _rational_to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gps_to_decimal(value: Any, ref: Any) -> Optional[float]:
    """DMS rational tuple + N/S/E/W reference -> signed decimal degrees."""
    try:
        parts = list(value)
    except TypeError:
        return _rational_to_float(value)
    if not parts:
        return None
    deg = _rational_to_float(parts[0]) or 0.0
    minutes = _rational_to_float(parts[1]) if len(parts) > 1 else 0.0
    seconds = _rational_to_float(parts[2]) if len(parts) > 2 else 0.0
    decimal = deg + (minutes or 0.0) / 60.0 + (seconds or 0.0) / 3600.0
    if str(ref or "").strip().upper().startswith(("S", "W")):
        decimal = -decimal
    return round(decimal, 6)


def _pillow_exif(path: Path) -> Dict[str, Any]:
    """Extract a curated metadata dict with Pillow (JPEG/TIFF/WebP)."""
    out: Dict[str, Any] = {"backend": "pillow"}
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        with Image.open(path) as img:
            exif = img.getexif()
            exif_ifd = exif.get_ifd(0x8769)
            gps_ifd = exif.get_ifd(0x8825)

            def put(tag_id: int, name: str, source: Any) -> None:
                value = source.get(tag_id)
                if value is not None and value != "":
                    out[name] = value

            put(0x010F, "make", exif)
            put(0x0110, "model", exif)
            put(0x0131, "software", exif)
            put(0x9003, "date_taken", exif_ifd)
            put(0x9004, "date_digitized", exif_ifd)
            put(0x0132, "date_modified_ifd0", exif)
            put(0xA431, "serial_number", exif_ifd)
            put(0xA433, "lens_make", exif_ifd)
            put(0xA434, "lens_model", exif_ifd)
            put(0xA435, "lens_serial", exif_ifd)
            put(0x920A, "focal_length_mm", exif_ifd)
            put(0x829D, "f_number", exif_ifd)
            put(0x8827, "iso", exif_ifd)

            lat = _gps_to_decimal(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF))
            lon = _gps_to_decimal(gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF))
            if lat is not None and lon is not None:
                out["gps_lat"], out["gps_lon"] = lat, lon
            alt = _rational_to_float(gps_ifd.get(_GPS_ALT))
            if alt is not None:
                out["gps_altitude_m"] = round(alt, 1)
    except Exception as exc:  # unreadable / no EXIF
        out["extraction_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


# ---------------------------------------------------------------------------
# Unified extraction + tamper heuristics
# ---------------------------------------------------------------------------


def _parse_exif_date(value: Any) -> Optional[datetime]:
    """'2024:06:21 12:30:00' / '2024-06-21 12:30:00' (± TZ) → naive dt."""
    text = str(value or "").strip()
    text = text.split("+")[0].split("Z")[0].strip()
    match = re.match(
        r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", text)
    if not match:
        return None
    return datetime(*map(int, match.groups()))


def extract_metadata(path: Path) -> Dict[str, Any]:
    """Unified metadata extraction (exiftool preferred, Pillow fallback)."""
    if not path.is_file():
        raise ValueError(f"file not found: {path}")

    record = _run_exiftool(path)
    if record is not None:
        data: Dict[str, Any] = {"backend": "exiftool"}
        for key, value in record.items():
            if key in _EXIFTOOL_FIELDS:
                canonical = _EXIFTOOL_FIELDS[key]
                if canonical.startswith("_"):
                    continue
                if data.get(canonical) in (None, ""):
                    data[canonical] = value
    else:
        data = _pillow_exif(path)

    # Normalise GPS: exiftool -n already gives signed decimals.
    lat, lon = data.get("gps_lat"), data.get("gps_lon")
    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lat_f = lon_f = None
    if lat_f is not None and lon_f is not None:
        data["gps"] = {"lat": round(lat_f, 6), "lon": round(lon_f, 6)}
        data["gps_map_url"] = (
            f"https://www.openstreetmap.org/?mlat={lat_f:.6f}"
            f"&mlon={lon_f:.6f}#map=15/{lat_f:.6f}/{lon_f:.6f}"
        )
    else:
        data["gps"] = None
    data.pop("gps_lat", None)
    data.pop("gps_lon", None)

    # Normalise dates to ISO.
    for key in ("date_taken", "date_digitized", "date_modified_exif",
                "date_modified_ifd0", "date_file_modified"):
        parsed = _parse_exif_date(data.get(key))
        if parsed:
            data[key] = parsed.strftime("%Y-%m-%d %H:%M:%S")
            data[f"{key}_dt"] = parsed

    # --- Tamper heuristics -------------------------------------------------
    flags: List[str] = []
    taken = data.get("date_taken_dt")
    digitized = data.get("date_digitized_dt")
    modified = data.get("date_modified_exif_dt") or data.get("date_modified_ifd0_dt")
    if taken and digitized and abs((taken - digitized).total_seconds()) > 120:
        flags.append("DateTimeOriginal differs from DateTimeDigitized by more "
                     "than 2 minutes — timestamps were edited or the file was "
                     "re-stamped")
    if taken and modified and modified > taken + timedelta(minutes=5):
        flags.append("EXIF ModifyDate is later than DateTimeOriginal — the "
                     "file was modified after capture (re-export/edit)")
    software = str(data.get("software") or "")
    if software:
        flags.append(f"Software tag present: '{software}' — image passed "
                     "through an editor (original camera files rarely have it)")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if taken and taken > now + timedelta(days=1):
        flags.append("DateTimeOriginal is in the future — impossible "
                     "timestamp (wrong clock or fabricated EXIF)")
    make = str(data.get("make") or "")
    if data.get("gps") is None and make:
        flags.append(f"no GPS in a phone photo ('{make}') — GPS was stripped, "
                     "never captured, or the file was re-saved")
    data["tamper_flags"] = flags
    data["tamper_risk"] = "high" if len(flags) >= 2 else (
        "medium" if flags else "low")
    return data


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


_MD_FEILD_LIMIT = 26


def _resolve_tz(tz_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo((tz_name or "UTC").strip())
    except Exception:
        return None


@ToolRegistry.register("osint_image")
class OsintImageTool(BaseTool):
    """Photo forensics: EXIF metadata + shadow-based time/place verification."""

    tool_id = "osint_image"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="osint_image",
            description=(
                "Image forensics and verification. action=metadata extracts "
                "full EXIF (GPS coordinates, camera model, serial numbers, "
                "timestamps) with tamper heuristics (edited timestamps, "
                "editor software, impossible dates). action=verify runs the "
                "SunCalc cross-check: computes the sun's exact position for "
                "the claimed time+place, derives the expected shadow length "
                "ratio and compass bearing, and — when the user supplies "
                "observations from the photo (shadow ratio/bearing) — flags "
                "physically impossible timestamps and finds the times of day "
                "that WOULD produce those shadows. Works from a local image "
                "path/URL or from explicit coordinates+datetime."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "enum": ["metadata", "verify"],
                        "default": "metadata",
                        "description": "metadata = full EXIF dump + tamper "
                                       "flags; verify = SunCalc shadow "
                                       "cross-check (includes metadata).",
                    },
                    "image": {
                        "type": "string",
                        "description": "Path or URL of the image to analyse "
                                       "(JPEG/TIFF/HEIC/PNG; videos & PDFs "
                                       "with exiftool installed).",
                    },
                    "lat": {"type": "number",
                            "description": "Explicit latitude (decimal "
                                           "degrees) when no image."},
                    "lon": {"type": "number",
                            "description": "Explicit longitude."},
                    "datetime": {
                        "type": "string",
                        "description": "Claimed capture moment, "
                                       "'YYYY-MM-DD HH:MM:SS' (interpretation "
                                       "controlled by tz).",
                    },
                    "tz": {
                        "type": "string", "default": "UTC",
                        "description": "IANA timezone of the datetime — "
                                       "camera timestamps are usually LOCAL "
                                       "time, e.g. 'Africa/Cairo'.",
                    },
                    "observed_shadow_ratio": {
                        "type": "number",
                        "description": "Shadow length / object height as "
                                       "measured in the photo (e.g. a person "
                                       "is 3 shadow-lengths tall → 3.0).",
                    },
                    "observed_shadow_bearing": {
                        "type": "number",
                        "description": "Compass bearing the shadows point "
                                       "to in the photo (0=N, 90=E, 180=S, "
                                       "270=W).",
                    },
                    "download_dir": {
                        "type": "string",
                        "description": "Directory allowed for URL downloads "
                                       "(default: creative media tmp).",
                    },
                },
            },
            category="geoint",
            timeout_seconds=120.0,
        )

    # -- helpers ------------------------------------------------------------

    def _download(self, src: str) -> Path:
        import httpx

        try:
            resp = httpx.get(src, follow_redirects=True, timeout=60.0,
                             headers={"User-Agent": "openjarvis-geoint/1.0"})
            resp.raise_for_status()
        except Exception as exc:
            raise ValueError(f"failed to download {src}: {exc}") from exc
        local = _paths.tmp_dir() / ("geoint-input" + os.path.splitext(src)[-1][:6])
        local.write_bytes(resp.content)
        return local

    def _resolve_path(self, src: str) -> Path:
        if src.startswith(("http://", "https://")):
            return self._download(src)
        local = Path(src).expanduser()
        if not local.is_absolute():
            for base in (Path.cwd(), _paths.creative_root()):
                candidate = base / local
                if candidate.exists():
                    local = candidate
                    break
        if not local.exists():
            raise ValueError(f"image not found: {src}")
        return local

    # -- actions ------------------------------------------------------------

    def _metadata_markdown(self, data: Dict[str, Any], path: Path) -> str:
        lines = [f"## Image forensics — `{path.name}`",
                 f"**Backend:** {data.get('backend', '?')} | "
                 f"**Tamper risk:** {data.get('tamper_risk', '?').upper()}"]
        gps = data.get("gps")
        if gps:
            lines.append(
                f"\n**Location:** {gps['lat']:.6f}, {gps['lon']:.6f} "
                f"([map]({data.get('gps_map_url', '')}))")
            if data.get("gps_altitude_m") is not None:
                lines.append(f"**Altitude:** {data['gps_altitude_m']} m")
        else:
            lines.append("\n**Location:** no GPS data in the file")
        rows = [
            ("Date taken", data.get("date_taken", "—")),
            ("Date digitized", data.get("date_digitized", "—")),
            ("Modified (EXIF)", data.get("date_modified_exif",
                                         data.get("date_modified_ifd0", "—"))),
            ("Camera", f"{data.get('make', '?')} {data.get('model', '?')}"),
            ("Serial number", data.get("serial_number",
                                       data.get("serial_number_makernotes",
                                                data.get("serial_internal", "—")))),
            ("Lens", f"{data.get('lens_make', '')} {data.get('lens_model', '')}"
                     .strip() or "—"),
            ("Software", data.get("software", "—")),
            ("Focal length", data.get("focal_length_mm", "—")),
            ("F-number", data.get("f_number", "—")),
            ("ISO", data.get("iso", "—")),
        ]
        lines.append("\n| Field | Value |\n|---|---|")
        for name, value in rows:
            lines.append(f"| {name} | {value} |")
        flags = data.get("tamper_flags") or []
        if flags:
            lines.append("\n**⚠ Tamper indicators:**")
            lines.extend(f"- {flag}" for flag in flags)
        else:
            lines.append("\n**No tamper indicators detected.**")
        return "\n".join(lines)

    def _verify(self, **params: Any) -> ToolResult:
        image = str(params.get("image") or "").strip()
        data: Dict[str, Any] = {}
        path = None
        lat = params.get("lat")
        lon = params.get("lon")
        naive_dt: Optional[datetime] = None
        tz_name = str(params.get("tz") or "UTC").strip()

        if image:
            path = self._resolve_path(image)
            data = extract_metadata(path)
            gps = data.get("gps") or {}
            if lat is None and gps.get("lat") is not None:
                lat, lon = gps["lat"], gps["lon"]
            naive_dt = data.get("date_taken_dt")
            if naive_dt is None:
                naive_dt = data.get("date_digitized_dt")
            if params.get("datetime"):
                naive_dt = _parse_exif_date(params["datetime"]) or naive_dt
        else:
            naive_dt = _parse_exif_date(params.get("datetime"))
            try:
                lat = float(lat) if lat is not None else None
                lon = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                lat = lon = None

        if lat is None or lon is None or naive_dt is None:
            missing = []
            if lat is None:
                missing.append("GPS (image EXIF or lat/lon params)")
            if naive_dt is None:
                missing.append("timestamp (EXIF DateTimeOriginal or datetime param)")
            return ToolResult(
                tool_name="osint_image",
                success=False,
                content=("Cannot run SunCalc verification — missing: "
                         + ", ".join(missing) + ". Provide the image or "
                         "explicit lat/lon/datetime."),
            )

        tzinfo = _resolve_tz(tz_name)
        if tzinfo is None:
            return ToolResult(
                tool_name="osint_image", success=False,
                content=f"Unknown timezone '{tz_name}' — use an IANA name "
                        "like 'Africa/Cairo' or 'UTC'.")
        local_dt = naive_dt.replace(tzinfo=tzinfo)
        utc_dt = local_dt.astimezone(timezone.utc)

        pos = sun_position(utc_dt, float(lat), float(lon))
        geo = shadow_geometry(pos["elevation"], pos["azimuth"])
        events = sun_events(utc_dt, float(lat), float(lon))

        lines = ["## SunCalc verification",
                 f"**Claimed moment:** {naive_dt} ({tz_name}) = "
                 f"{utc_dt.strftime('%Y-%m-%d %H:%M UTC')}",
                 f"**Location:** {float(lat):.6f}, {float(lon):.6f}"]
        if path is not None:
            lines.append(f"**Source:** `{path.name}` "
                         f"(EXIF backend: {data.get('backend')})")
        lines.append(
            "\n| Quantity | Value |\n|---|---|"
            f"\n| Sun elevation | {pos['elevation']}° "
            f"({'above' if pos['elevation'] > 0 else 'BELOW'} horizon) |"
            f"\n| Sun azimuth (toward) | {pos['azimuth']}° "
            f"{compass_name(pos['azimuth'])} |")
        if geo.get("ratio") is not None:
            lines.append(
                f"| Expected shadow ratio (length/height) | {geo['ratio']} |"
                f"\n| Expected shadow bearing (points to) | {geo['bearing']}° "
                f"{geo['bearing_label']} |")
        else:
            lines.append(f"| Shadows | {geo.get('note')} |")
        lines.append(
            f"\n**Day events (UTC):** sunrise {events['sunrise_utc']} · "
            f"solar noon {events['solar_noon_utc']} "
            f"(elevation {events['solar_noon_elevation_deg']}°) · "
            f"sunset {events['sunset_utc']}")

        flags: List[str] = []
        observed_ratio = params.get("observed_shadow_ratio")
        observed_bearing = params.get("observed_shadow_bearing")
        candidates: List[Dict[str, Any]] = []

        if pos["elevation"] <= 0:
            flags.append(f"IMPOSSIBLE for daytime: the sun was "
                         f"{abs(pos['elevation']):.1f}° BELOW the horizon at "
                         "the claimed moment — a photo showing daylight/"
                         "shadows cannot have been taken then")
        elif observed_ratio is not None:
            try:
                observed = float(observed_ratio)
            except (TypeError, ValueError):
                observed = None
            if observed and observed > 0 and geo.get("ratio"):
                rel_err = abs(geo["ratio"] - observed) / observed
                if rel_err > 0.35:
                    flags.append(
                        f"Shadow length mismatch: claimed moment implies a "
                        f"ratio of {geo['ratio']}, the photo shows ~{observed} "
                        f"(±35% tolerance) — timestamp does not fit the "
                        "shadows")
                    candidates = match_shadow(
                        utc_dt, float(lat), float(lon), observed,
                        params.get("observed_shadow_bearing")
                        and float(observed_bearing))
                else:
                    lines.append(
                        f"\n✓ Shadow length consistent with the claimed "
                        f"time (expected {geo['ratio']}, observed ~{observed}).")
        if observed_bearing is not None and geo.get("bearing") is not None:
            try:
                obs_b = float(observed_bearing)
            except (TypeError, ValueError):
                obs_b = None
            if obs_b is not None:
                diff = abs((geo["bearing"] - obs_b + 180) % 360 - 180)
                if diff > 30:
                    flags.append(
                        f"Shadow direction mismatch: claimed moment puts "
                        f"shadows at {geo['bearing']}°, photo shows ~{obs_b}° "
                        f"({diff:.0f}° apart) — either the time or the place "
                        "is wrong")
                else:
                    lines.append(f"\n✓ Shadow direction consistent "
                                 f"({diff:.0f}° off claimed time).")

        if candidates:
            lines.append("\n**Moments of this day that WOULD produce the "
                         "observed shadows (UTC):**")
            lines.append("| Time UTC | Elevation | Ratio | Shadow bearing |")
            lines.append("|---|---|---|---|")
            for cand in candidates[:6]:
                lines.append(f"| {cand['time_utc']} | {cand['elevation']}° | "
                             f"{cand['ratio']} | {cand['bearing']}° |")
        if data.get("tamper_flags"):
            flags.extend(data["tamper_flags"])
        if flags:
            lines.append("\n**⚠ Verification flags:**")
            lines.extend(f"- {flag}" for flag in flags)
        else:
            lines.append("\n**No physical inconsistencies found** between the "
                         "claimed time/place and the sun geometry.")
        lines.append(
            "\n*Note: camera timestamps are usually LOCAL time — pass "
            "tz='Africa/Cairo' (etc.) for the shooting location.*")

        return ToolResult(
            tool_name="osint_image", content="\n".join(lines), success=True,
            metadata={
                "sun": pos, "shadow": geo, "events": events,
                "tamper_flags": flags,
                "candidates": candidates,
                "gps": data.get("gps"),
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        action = str(params.get("action") or "metadata").strip().lower()
        try:
            if action == "verify":
                return self._verify(**params)
            image = str(params.get("image") or "").strip()
            if not image:
                return ToolResult(
                    tool_name="osint_image", success=False,
                    content="metadata action requires an 'image' (path or URL).")
            path = self._resolve_path(image)
            data = extract_metadata(path)
            return ToolResult(
                tool_name="osint_image",
                content=self._metadata_markdown(data, path),
                success=True,
                metadata={"exif": {k: v for k, v in data.items()
                                   if not k.endswith("_dt")},
                          "tamper_risk": data.get("tamper_risk"),
                          "tamper_flags": data.get("tamper_flags")},
            )
        except Exception as exc:
            logger.warning("osint_image failed: %s", exc)
            return ToolResult(tool_name="osint_image", success=False,
                              content=f"osint_image error: {exc}"[:500])


__all__ = ["OsintImageTool", "extract_metadata", "exiftool_backend",
           "exiftool_command"]

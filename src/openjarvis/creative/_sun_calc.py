"""Solar position + shadow mathematics (NOAA algorithm, pure Python).

Used by ``osint_image`` (image forensics) to verify the *time and place*
of a photo from its EXIF GPS/timestamp and the geometry of the shadows
in the scene:

* Sun elevation/azimuth for any lat/lon/UTC-moment (±0.5° accuracy —
  plenty for shadow analysis).
* Shadow length ratio (length / object height) and shadow compass
  bearing.
* Sunrise / sunset / solar noon for the day.
* Reverse matching: given an *observed* shadow ratio (and optionally the
  shadow bearing), find the moments of the day that could produce it —
  the classic SunCalc cross-check used to catch mismatched EXIF times.

No third-party dependencies.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "sun_position",
    "shadow_geometry",
    "sun_events",
    "match_shadow",
    "compass_name",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
_COMPASS_16_AR = [
    "شمال", "شمال شمال شرق", "شمال شرق", "شرق شمال شرق",
    "شرق", "شرق جنوب شرق", "جنوب شرق", "جنوب جنوب شرق",
    "جنوب", "جنوب جنوب غرب", "جنوب غرب", "غرب جنوب غرب",
    "غرب", "غرب شمال غرب", "شمال غرب", "شمال شمال غرب",
]


def compass_name(bearing_deg: float) -> str:
    """16-wind compass label for a bearing (e.g. 292 -> 'WNW')."""
    idx = int(((bearing_deg % 360) + 11.25) // 22.5) % 16
    return f"{_COMPASS_16[idx]} ({_COMPASS_16_AR[idx]})"


def _deg(rad: float) -> float:
    return math.degrees(rad)


def _rad(deg: float) -> float:
    return math.radians(deg)


def _julian_day(dt: datetime) -> float:
    """Julian day for a UTC datetime (fractional)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    day = dt.day + (dt.hour + dt.minute / 60 + dt.second / 3600) / 24
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + day + b - 1524.5)


# ---------------------------------------------------------------------------
# Solar position (NOAA)
# ---------------------------------------------------------------------------


def sun_position(dt_utc: datetime, lat: float, lon: float) -> Dict[str, float]:
    """Sun position for a UTC instant.

    Returns degrees: ``elevation`` (above horizon, negative at night),
    ``azimuth`` (from true north, clockwise, direction *toward* the sun),
    plus ``declination`` and ``hour_angle`` for transparency.
    """
    jd = _julian_day(dt_utc)
    t = (jd - 2451545.0) / 36525.0

    # Orbital elements (NOAA).
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(_rad(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(_rad(2 * m)) * (0.019993 - 0.000101 * t)
         + math.sin(_rad(3 * m)) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    lam = true_long - 0.00569 - 0.00478 * math.sin(_rad(omega))  # apparent long

    eps0 = 23 + (26 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))))
                 / 60) / 60
    eps = eps0 + 0.00256 * math.cos(_rad(omega))

    declination = _deg(math.asin(math.sin(_rad(eps)) * math.sin(_rad(lam))))

    # Equation of time (minutes).
    y = math.tan(_rad(eps / 2)) ** 2
    eot = 4 * _deg(
        y * math.sin(_rad(2 * l0))
        - 2 * e * math.sin(_rad(m))
        + 4 * e * y * math.sin(_rad(m)) * math.cos(_rad(2 * l0))
        - 0.5 * y * y * math.sin(_rad(4 * l0))
        - 1.25 * e * e * math.sin(_rad(2 * m))
    )

    # True solar time -> hour angle.
    utc_minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60
    true_solar = (utc_minutes + 4 * lon + eot) % 1440
    hour_angle = true_solar / 4 - 180
    if hour_angle < -180:
        hour_angle += 360

    lat_r, dec_r, ha_r = _rad(lat), _rad(declination), _rad(hour_angle)
    zenith = _deg(math.acos(min(1.0, max(-1.0,
        math.sin(lat_r) * math.sin(dec_r)
        + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha_r)))))
    elevation = 90 - zenith

    # Azimuth clockwise from north. Horizontal-coordinate relation:
    #   sin(elev) = sin(φ)·sin(δ) + cos(φ)·cos(δ)·cos(H)   (used above)
    #   cos(A)    = (sin(δ) − sin(elev)·sin(φ)) / (cos(elev)·cos(φ))
    zenith_r = _rad(zenith)
    sin_elev = math.cos(zenith_r)   # sin(90 − zenith)
    cos_elev = math.sin(zenith_r)   # cos(90 − zenith)
    denom = cos_elev * math.cos(lat_r)
    if denom < 1e-9:
        # Sun at/near the zenith — compass direction ill-defined.
        azimuth = 180.0
    else:
        cos_az = (math.sin(dec_r) - sin_elev * math.sin(lat_r)) / denom
        cos_az = min(1.0, max(-1.0, cos_az))
        az = _deg(math.acos(cos_az))
        azimuth = az if hour_angle <= 0 else (360 - az)

    return {
        "elevation": round(elevation, 2),
        "azimuth": round(azimuth % 360, 2),
        "declination": round(declination, 2),
        "hour_angle": round(hour_angle, 2),
        "equation_of_time_min": round(eot, 2),
    }


# ---------------------------------------------------------------------------
# Shadow geometry
# ---------------------------------------------------------------------------


def shadow_geometry(elevation_deg: float, azimuth_deg: float) -> Dict[str, Any]:
    """Shadow ratio + bearing from sun position.

    * ``ratio``: shadow length / object height = 1 / tan(elevation).
    * ``bearing``: compass direction the shadow points to
      (sun azimuth + 180).
    """
    elevation = elevation_deg
    azimuth = azimuth_deg % 360
    if elevation <= 0:
        return {
            "ratio": None,
            "bearing": None,
            "note": "sun below horizon — no shadows possible",
        }
    ratio = 1.0 / math.tan(_rad(elevation))
    bearing = (azimuth + 180) % 360
    return {
        "ratio": round(ratio, 3),
        "bearing": round(bearing, 1),
        "bearing_label": compass_name(bearing),
    }


# ---------------------------------------------------------------------------
# Day events (scan-based; robust, no special cases)
# ---------------------------------------------------------------------------


def sun_events(
    date, lat: float, lon: float
) -> Dict[str, Any]:
    """Sunrise/sunset/solar-noon times (UTC) for the UTC date of *date*.

    ``date`` may be a ``datetime`` (tz-aware recommended) — only its UTC
    calendar day is used.
    """
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    day = date.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)

    prev_elev = None
    sunrise = sunset = None
    best = -90.0
    best_at = None
    cur = day
    while cur < day + timedelta(days=1):
        pos = sun_position(cur, lat, lon)
        elev = pos["elevation"]
        if elev > best:
            best = elev
            best_at = cur
        if prev_elev is not None:
            if prev_elev < 0 <= elev and sunrise is None:
                sunrise = _interp(cur, prev_elev, elev)
            if prev_elev >= 0 > elev and sunset is None:
                sunset = _interp(cur, prev_elev, elev)
        prev_elev = elev
        cur += timedelta(minutes=4)

    # Solar noon: refine the global maximum with a 1-minute scan.
    noon = None
    if best_at is not None:
        refined_best = best
        refined_at = best_at
        cur = best_at - timedelta(minutes=40)
        while cur <= best_at + timedelta(minutes=40):
            pos = sun_position(cur, lat, lon)
            if pos["elevation"] > refined_best:
                refined_best = pos["elevation"]
                refined_at = cur
            cur += timedelta(minutes=1)
        noon = (refined_at, {"elevation": round(refined_best, 2)})

    return {
        "sunrise_utc": sunrise.strftime("%H:%M") if sunrise else "—",
        "sunset_utc": sunset.strftime("%H:%M") if sunset else "—",
        "solar_noon_utc": noon[0].strftime("%H:%M") if noon else "—",
        "solar_noon_elevation_deg": noon[1].get("elevation") if noon else None,
    }


def _interp(cur: datetime, prev: float, cur_v: float) -> datetime:
    """Linear crossing time for elevation == 0."""
    span = 4  # scan step minutes
    if cur_v == prev:
        return cur
    frac = (0 - prev) / (cur_v - prev)
    return cur - timedelta(minutes=span * (1 - frac))


# ---------------------------------------------------------------------------
# Reverse shadow matching
# ---------------------------------------------------------------------------


def match_shadow(
    date,
    lat: float,
    lon: float,
    target_ratio: float,
    target_bearing: Optional[float] = None,
    *,
    step_minutes: int = 5,
    ratio_tolerance: float = 0.25,
    bearing_tolerance_deg: float = 25.0,
) -> List[Dict[str, Any]]:
    """Find moments on *date* whose shadow geometry matches observations.

    Returns ranked candidates: ``time_utc``, ``elevation``, ``azimuth``,
    ``ratio``, ``ratio_error`` (relative), ``bearing``, ``bearing_error``.
    ``date`` is a tz-aware or naive UTC datetime (only the calendar day
    is used).
    """
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    day = date.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    results: List[Dict[str, Any]] = []
    cur = day + timedelta(hours=4)
    end = day + timedelta(hours=21)
    while cur <= end:
        pos = sun_position(cur, lat, lon)
        if pos["elevation"] > 5:  # shadows only meaningful in daylight
            geo = shadow_geometry(pos["elevation"], pos["azimuth"])
            ratio_err = abs(geo["ratio"] - target_ratio) / target_ratio
            entry: Dict[str, Any] = {
                "time_utc": cur.strftime("%H:%M"),
                "elevation": pos["elevation"],
                "azimuth": pos["azimuth"],
                "ratio": geo["ratio"],
                "ratio_error": round(ratio_err, 3),
                "bearing": geo["bearing"],
            }
            if target_bearing is not None:
                berr = abs((geo["bearing"] - target_bearing + 180) % 360 - 180)
                entry["bearing_error"] = round(berr, 1)
                if berr > bearing_tolerance_deg:
                    cur += timedelta(minutes=step_minutes)
                    continue
            if ratio_err <= ratio_tolerance:
                results.append(entry)
        cur += timedelta(minutes=step_minutes)

    results.sort(key=lambda r: r["ratio_error"] + (
        r.get("bearing_error", 0) / 180))
    return results[:8]

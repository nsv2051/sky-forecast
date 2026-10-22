"""Solar geometry primitives (pure Python standard library).

Computes topocentric solar elevation/azimuth for a latitude, longitude and
UTC instant, plus sunrise/sunset and golden-hour (light-penetration) windows.

The implementation follows the NOAA solar position algorithm and introduces
no third-party dependency, so it is safe for GitHub Actions / Vercel
Serverless (cold start, no wheels).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

__all__ = [
    "solar_position",
    "sunrise_sunset",
    "golden_hour_windows",
    "solar_noon",
    "julian_day",
    "refraction_correction",
]

RAD = math.pi / 180.0
DEG = 180.0 / math.pi

# Geometric altitude of the sun centre at apparent sunrise/sunset.
SUNRISE_ALTITUDE = -0.833


def _as_utc(dt: datetime) -> datetime:
    """Return *dt* as an aware UTC datetime (naive input is treated as UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def julian_day(dt: datetime) -> float:
    """Julian Day for the given instant (UTC)."""
    dt = _as_utc(dt)
    year, month = dt.year, dt.month
    day = (
        dt.day
        + (dt.hour + dt.minute / 60.0 + dt.second / 3600.0 + dt.microsecond / 3.6e9)
        / 24.0
    )
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        int(365.25 * (year + 4716))
        + int(30.6001 * (month + 1))
        + day
        + b
        - 1524.5
    )


def _solar_terms(jd: float) -> dict:
    """Return declination (rad) and equation of time (minutes) for a JD."""
    t = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = m * RAD
    c = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )
    true_longitude = l0 + c

    omega = 125.04 - 1934.136 * t
    apparent_longitude = true_longitude - 0.00569 - 0.00478 * math.sin(omega * RAD)

    epsilon0 = 23.0 + (
        26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0
    ) / 60.0
    epsilon = epsilon0 + 0.00256 * math.cos(omega * RAD)

    lam = apparent_longitude * RAD
    eps = epsilon * RAD
    declination = math.asin(math.sin(eps) * math.sin(lam))

    y = math.tan(eps / 2.0) ** 2
    l0_rad = l0 * RAD
    eot = 4.0 * DEG * (
        y * math.sin(2 * l0_rad)
        - 2 * eccentricity * math.sin(m_rad)
        + 4 * eccentricity * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * m_rad)
    )

    return {
        "declination": declination,
        "equation_of_time": eot,
        "true_longitude": true_longitude,
    }


def refraction_correction(elevation_deg: float) -> float:
    """Atmospheric refraction (degrees) to add to the geometric altitude."""
    e = elevation_deg
    if e > 85.0:
        return 0.0
    te = math.tan(e * RAD)
    if e > 5.0:
        r = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
    elif e > -0.575:
        r = (
            1735.0
            + e
            * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
        )
    else:
        r = -20.772 / te
    return r / 3600.0


def solar_position(lat: float, lon: float, dt: datetime) -> dict:
    """Topocentric solar position for *lat*/*lon* (degrees) at UTC *dt*.

    Returns a dict with ``elevation`` and ``azimuth`` in degrees (azimuth
    measured clockwise from true north), plus supporting angles. ``elevation``
    includes the atmospheric refraction correction.
    """
    dt = _as_utc(dt)
    terms = _solar_terms(julian_day(dt))
    declination = terms["declination"]
    eot = terms["equation_of_time"]

    minutes = dt.hour * 60.0 + dt.minute + dt.second / 60.0 + dt.microsecond / 6e7
    true_solar_time = (minutes + eot + 4.0 * lon) % 1440.0
    hour_angle = true_solar_time / 4.0 - 180.0
    hour_angle = (hour_angle + 180.0) % 360.0 - 180.0

    lat_rad = lat * RAD
    ha_rad = hour_angle * RAD

    cos_zenith = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(ha_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    geometric_elevation = 90.0 - zenith * DEG
    elevation = geometric_elevation + refraction_correction(geometric_elevation)

    denom = math.cos(lat_rad) * math.sin(zenith)
    if abs(denom) < 1e-12:
        azimuth = 180.0 if lat >= 0 else 0.0
    else:
        cos_az = (
            math.sin(lat_rad) * cos_zenith - math.sin(declination)
        ) / denom
        cos_az = max(-1.0, min(1.0, cos_az))
        az = math.acos(cos_az) * DEG
        if hour_angle > 0:
            azimuth = (az + 180.0) % 360.0
        else:
            azimuth = (540.0 - az) % 360.0

    return {
        "elevation": elevation,
        "geometric_elevation": geometric_elevation,
        "zenith": zenith * DEG,
        "azimuth": azimuth,
        "declination": declination * DEG,
        "hour_angle": hour_angle,
        "equation_of_time": eot,
        "true_solar_time": true_solar_time,
        "is_daytime": elevation > 0.0,
    }


def _day_start(day) -> datetime:
    if isinstance(day, datetime):
        day = day.date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def solar_noon(lat: float, lon: float, day) -> datetime:
    """UTC instant of local solar noon for the given date."""
    start = _day_start(day)
    # Equation of time varies slowly; one refinement iteration is plenty.
    minutes = 720.0 - 4.0 * lon
    for _ in range(3):
        jd = julian_day(start + timedelta(minutes=minutes))
        eot = _solar_terms(jd)["equation_of_time"]
        minutes = 720.0 - 4.0 * lon - eot
    return start + timedelta(minutes=minutes)


def sunrise_sunset(lat: float, lon: float, day) -> dict:
    """Sunrise/sunset (UTC) for *day*.

    Returns ``{"sunrise", "sunset", "solar_noon", "polar_day", "polar_night"}``.
    ``sunrise``/``sunset`` are ``None`` during polar day/night.
    """
    start = _day_start(day)
    noon = solar_noon(lat, lon, day)
    jd = julian_day(noon)
    declination = _solar_terms(jd)["declination"]

    lat_rad = lat * RAD
    cos_ha = (
        math.cos((90.0 - SUNRISE_ALTITUDE) * RAD)
        - math.sin(lat_rad) * math.sin(declination)
    ) / (math.cos(lat_rad) * math.cos(declination))

    if cos_ha > 1.0:
        return {
            "sunrise": None,
            "sunset": None,
            "solar_noon": noon,
            "polar_day": False,
            "polar_night": True,
        }
    if cos_ha < -1.0:
        return {
            "sunrise": None,
            "sunset": None,
            "solar_noon": noon,
            "polar_day": True,
            "polar_night": False,
        }

    hour_angle = math.acos(max(-1.0, min(1.0, cos_ha))) * DEG
    delta = timedelta(hours=hour_angle / 15.0)
    return {
        "sunrise": noon - delta,
        "sunset": noon + delta,
        "solar_noon": noon,
        "polar_day": False,
        "polar_night": False,
    }


def golden_hour_windows(
    lat: float,
    lon: float,
    day,
    low_elevation: float = -4.0,
    high_elevation: float = 6.0,
) -> dict:
    """Contiguous windows where the sun sits in the golden/penetration band.

    The default band ``[-4deg, +6deg]`` corresponds to the grazing light that
    produces burning clouds. Returns ``{"morning", "evening", "all"}`` where
    windows are ``(start, end)`` UTC tuples (or ``None``).
    """
    start = _day_start(day)
    rst = sunrise_sunset(lat, lon, day)
    noon = rst["solar_noon"]

    if rst["polar_day"]:
        return {"morning": None, "evening": None, "all": [(start, start + timedelta(days=1))]}

    if rst["polar_night"]:
        # Sun may never reach the band; still scan the natural day.
        scan_start = start
        scan_end = start + timedelta(days=1)
    else:
        scan_start = rst["sunrise"] - timedelta(minutes=120)
        scan_end = rst["sunset"] + timedelta(minutes=120)

    windows = []
    current = None
    minute = int((scan_start - start).total_seconds() // 60)
    end_minute = int((scan_end - start).total_seconds() // 60) + 1
    while minute <= end_minute:
        instant = start + timedelta(minutes=minute)
        elev = solar_position(lat, lon, instant)["elevation"]
        inside = low_elevation <= elev <= high_elevation
        if inside and current is None:
            current = instant
        elif not inside and current is not None:
            windows.append((current, instant))
            current = None
        minute += 1
    if current is not None:
        windows.append((current, scan_end))

    morning = None
    evening = None
    for window in windows:
        if window[1] <= noon:
            morning = window
        elif window[0] >= noon:
            evening = window
    return {"morning": morning, "evening": evening, "all": windows}

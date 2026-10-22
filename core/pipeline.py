"""Synthetic forecast pipeline for sky-forecast.

Combines the Open-Meteo sounding/cloud data and QWeather ground observations
with the physics models to produce, for every configured spot:

* a 72-hour hourly series of the Burning Cloud Index (BCI) and the Sea of
  Clouds Index (SCI),
* the best sunset / sunrise hours,
* a status and orientation recommendation,

plus a burning-cloud grid over the greater Zhengzhou area for heat-map
rendering. The whole pipeline runs offline and deterministically, so it is
safe for CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.fetcher.open_meteo import (
    GRID_VARIABLES,
    PRESSURE_LEVELS,
    HourlySeries,
    OpenMeteoClient,
    OpenMeteoError,
    pressure_height,
)
from core.fetcher.qweather import GroundWeather, QWeatherClient
from core.physics import burning_cloud, sea_of_clouds, solar

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_SPOTS_PATH",
    "DEFAULT_OUTPUT_PATH",
    "FORECAST_HOURS",
    "LOCAL_UTC_OFFSET_HOURS",
    "SNAPSHOT_DAY_COUNT",
    "SEA_OBSERVABLE_HOURS",
    "GRID_BOUNDS",
    "GRID_STEP",
    "load_config",
    "load_spots",
    "build_forecast",
    "build_snapshot_grid",
    "golden_snapshot_windows",
    "save_forecast",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPOTS_PATH = PROJECT_ROOT / "config" / "spots.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "public" / "data" / "forecast.json"

FORECAST_HOURS = 96
LOCAL_UTC_OFFSET_HOURS = 8
SUMMARY_DAYS = 3

# 黄金时次快照：未来三天的主要日出/日落窗口，每个窗口输出该统一时刻的
# 全区域连续云场（而不是旧版"每个格点各取 72h 内自己的最大"那种时空错乱）。
SNAPSHOT_DAY_COUNT = 3
_DAY_TOKENS = ("today", "tomorrow", "day3")
_SNAPSHOT_LABELS = {
    "today_sunrise": "今日日出",
    "today_sunset": "今日日落",
    "tomorrow_sunrise": "明晨日出",
    "tomorrow_sunset": "明晚日落",
    "day3_sunrise": "后天日出",
    "day3_sunset": "后天日落",
}
_EVENT_ICON = {"sunrise": "🌅", "sunset": "🌇"}

# Cover the central-western Henan Funiu mountains and surroundings.
GRID_BOUNDS = {
    "lat_min": 32.0,
    "lat_max": 35.5,
    "lon_min": 111.0,
    "lon_max": 114.5,
}
GRID_STEP = 0.1

_MORNING_ELEV = (-5.0, 8.0)
_EVENING_ELEV = (-5.0, 8.0)
_CLOUD_FIELDS = ("cloud_low", "cloud_mid", "cloud_high")

# 云海可观测窗口：取自青龙 sunset_monitor 的印证时段（清晨 4-9 时、傍晚 16-19 时）。
SEA_OBSERVABLE_HOURS = ((4, 9), (16, 19))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _floor_hour(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _parse_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)


def _local_iso(dt: datetime) -> str:
    return _local(dt).strftime("%Y-%m-%d %H:%M")


def _local_hhmm(dt: datetime) -> str:
    return _local(dt).strftime("%H:%M")


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_or_none(value, digits: int = 0):
    if value is None:
        return None
    return round(float(value), digits) if digits else round(float(value))


def _value(series: HourlySeries, name: str, index: int, default=None):
    value = series.value(name, index)
    return default if value is None else value


def load_config(path=None) -> dict:
    """Load the spots configuration JSON."""
    target = Path(path) if path else DEFAULT_SPOTS_PATH
    return json.loads(Path(target).read_text(encoding="utf-8"))


def load_spots(path=None) -> list:
    """Return just the list of spot dictionaries."""
    return load_config(path).get("spots", [])


# ---------------------------------------------------------------------------
# fetch orchestration
# ---------------------------------------------------------------------------
def _fetch_series(client, coordinates, *, hours, start, variables, force_mock):
    """Return ``(series_list, source_label)``.

    Live ECMWF data from Open-Meteo is the default and is labelled
    ``open-meteo-live``. ``--mock`` forces the deterministic offline generator
    (``mock``); a genuine network failure degrades to ``mock_fallback`` so the
    static build never hard-fails, but that state is always visible in
    ``meta.data_source`` / ``meta.mock`` rather than being hidden.
    """
    if force_mock:
        return client.mock(coordinates, hours=hours, start=start, variables=variables), "mock"
    try:
        series = client.fetch_hourly(
            coordinates, hours=hours, start=start, variables=variables
        )
        if not series or all(len(item) == 0 for item in series):
            raise OpenMeteoError("empty open-meteo response")
        return series, "open-meteo-live"
    except Exception:  # noqa: BLE001 - degrade to deterministic mock
        return client.mock(coordinates, hours=hours, start=start, variables=variables), "mock_fallback"


# ---------------------------------------------------------------------------
# per-spot physics
# ---------------------------------------------------------------------------
def _pressure_profile(series: HourlySeries, index: int) -> list:
    """Build a pressure-level sounding (height / temperature / RH)."""
    profile = []
    for level in PRESSURE_LEVELS:
        height = _value(series, "geopotential_height_%dhPa" % level, index)
        temperature = _value(series, "temperature_%dhPa" % level, index)
        if temperature is None:
            continue
        humidity = _value(series, "relative_humidity_%dhPa" % level, index, 60.0)
        profile.append(
            {
                "pressure": level,
                "height": float(height if height is not None else pressure_height(level)),
                "temperature": float(temperature),
                "relative_humidity": float(humidity),
            }
        )
    return profile


def _ground_obs(ground, series, index):
    """返回 (visibility_km, aqi, source)。

    和风地面观测可用时以其为准；降级为 neutral 时：
    - visibility 改用 Open-Meteo 逐时 visibility（米 -> 公里），
      因为它同样是实测/模式地面能见度，且逐时变化；
    - aqi 无从获得，返回 None（不施加气溶胶惩罚），
      绝不用兜底常数 50 伪装成观测。
    """
    if ground.source != "neutral":
        return float(ground.visibility_km), ground.aqi, "qweather"
    raw = series.value("visibility", index)
    if raw is None:
        return float(ground.visibility_km), None, "open-meteo"
    return max(0.1, float(raw) / 1000.0), None, "open-meteo"


def _hourly_entry(spot: dict, series: HourlySeries, ground: GroundWeather, index: int) -> dict:
    instant = _parse_time(series.time[index])
    local_hour = _local(instant).hour
    sci_observable = any(
        start <= local_hour <= end for start, end in SEA_OBSERVABLE_HOURS
    )
    position = solar.solar_position(spot["lat"], spot["lon"], instant)
    visibility_km, aqi, ground_source = _ground_obs(ground, series, index)

    cloud_low = float(_value(series, "cloud_cover_low", index, 0.0))
    cloud_mid = float(_value(series, "cloud_cover_mid", index, 0.0))
    cloud_high = float(_value(series, "cloud_cover_high", index, 0.0))
    wind = _value(series, "wind_speed_10m", index)

    bci = burning_cloud.compute_bci(
        cloud_low=cloud_low,
        cloud_mid=cloud_mid,
        cloud_high=cloud_high,
        visibility_km=visibility_km,
        aqi=aqi,
        sun_elevation=position["elevation"],
        sun_azimuth=position["azimuth"],
        horizon_low_cloud=cloud_low,
    )
    sci = sea_of_clouds.compute_sci(
        _pressure_profile(series, index),
        viewpoint_elevation=float(spot["elevation_m"]),
        wind_speed=wind,
    )

    return {
        "time": _iso_utc(instant),
        "time_local": _local_iso(instant),
        "bci": bci["bci"],
        "bci_grade": bci["grade"],
        "bci_components": bci["components"],
        "sci": sci["sci"],
        "sci_status": sci["status"],
        "sci_state": sci["state"],
        "sci_observable": sci_observable,
        "sci_components": sci["components"],
        "inversion_height_m": _round_or_none(sci.get("inversion_height")),
        "inversion_strength": sci.get("inversion_strength"),
        "cloud_base_m": _round_or_none(sci.get("cloud_base")),
        "cloud_top_m": _round_or_none(sci.get("cloud_top")),
        "sun_elevation": round(position["elevation"], 2),
        "sun_azimuth": round(position["azimuth"], 1),
        "sun_hour_angle": round(position["hour_angle"], 2),
        "is_daytime": bool(position["is_daytime"]),
        "cloud_low": round(cloud_low, 1),
        "cloud_mid": round(cloud_mid, 1),
        "cloud_high": round(cloud_high, 1),
        "visibility_km": round(float(visibility_km), 1),
        "aqi": None if aqi is None else round(float(aqi), 1),
        "ground_source": ground_source,
        "wind_speed_10m": None if wind is None else round(float(wind), 1),
    }


def _event_clock(spot, day_local):
    """返回该地该日真实日出/日落的本地 HH:MM 与分钟数。

    采样链路只保留整点小时，直接用 time_local 会把 17:51 的日落写成
    18:00。这里查询 solar.sunrise_sunset 的真实事件时刻并换算到项目
    使用的 UTC+8，供 API 字段如实展示；极昼/极夜没有事件，返回两个
    None，避免伪造时刻。
    """
    rst = solar.sunrise_sunset(spot["lat"], spot["lon"], day_local)
    if rst.get("polar_day") or rst.get("polar_night"):
        return None, None
    sunrise = rst.get("sunrise")
    sunset = rst.get("sunset")
    sunrise_hhmm = _local_hhmm(sunrise) if sunrise is not None else None
    sunset_hhmm = _local_hhmm(sunset) if sunset is not None else None
    return sunrise_hhmm, sunset_hhmm


def _event_value_at(hourly: list, best_index: int, event_instant, field: str):
    """按事件时刻在相邻整点之间线性插值某一指标。

    既有 bci / sci 是整点采样值，前端仍在使用；事件时刻通常落在两个
    整点之间（如 17:51），单看 18:00 会错估光照窗口内的实际评分。
    事件恰好等于整点或相邻小时缺失时直接返回采样值，绝不外推。
    """
    if event_instant is None:
        return None
    best = hourly[best_index]
    best_time = _parse_time(best["time"])
    if event_instant == best_time:
        return round(float(best[field]), 1)
    other_index = best_index - 1 if event_instant < best_time else best_index + 1
    if other_index < 0 or other_index >= len(hourly):
        return round(float(best[field]), 1)
    other = hourly[other_index]
    other_time = _parse_time(other["time"])
    start, end = sorted((best_time, other_time))
    if end == start or not (start <= event_instant <= end):
        return round(float(best[field]), 1)
    ratio = (event_instant - start).total_seconds() / (end - start).total_seconds()
    if best_time == start:
        first, second = float(best[field]), float(other[field])
    else:
        first, second = float(other[field]), float(best[field])
    return round(first + (second - first) * ratio, 1)


def _select_best(hourly: list, evening: bool, spot: dict | None = None):
    low, high = _EVENING_ELEV if evening else _MORNING_ELEV
    candidates = [
        entry
        for entry in hourly
        if low <= entry["sun_elevation"] <= high
        and (
            entry["sun_hour_angle"] > 0
            if evening
            else entry["sun_hour_angle"] < 0
        )
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (item["bci"], item["sci"]))
    keys = (
        "time",
        "time_local",
        "bci",
        "bci_grade",
        "sci",
        "sci_status",
        "sci_state",
        "inversion_height_m",
        "cloud_base_m",
        "cloud_top_m",
        "sun_elevation",
        "sun_azimuth",
        "cloud_low",
        "cloud_mid",
        "cloud_high",
        "visibility_km",
        "aqi",
    )
    result = {key: best[key] for key in keys}
    result["sampled_time_local"] = best["time_local"]
    result["event_time_local"] = None
    result["event_offset_minutes"] = None
    result["bci_at_event"] = None
    result["sci_at_event"] = None
    if spot is None:
        return result

    sampled_local = _local(_parse_time(best["time"]))
    day_local = sampled_local.date()
    sunrise_hhmm, sunset_hhmm = _event_clock(spot, day_local)
    result["event_time_local"] = sunset_hhmm if evening else sunrise_hhmm

    rst = solar.sunrise_sunset(spot["lat"], spot["lon"], day_local)
    event_instant = rst.get("sunset") if evening else rst.get("sunrise")
    if event_instant is None:
        return result
    event_local = _local(event_instant)
    result["event_offset_minutes"] = int(
        round((sampled_local - event_local).total_seconds() / 60.0)
    )
    best_index = next(
        index for index, entry in enumerate(hourly) if entry is best
    )
    result["bci_at_event"] = _event_value_at(
        hourly, best_index, event_instant, "bci"
    )
    result["sci_at_event"] = _event_value_at(
        hourly, best_index, event_instant, "sci"
    )
    return result


def _orientation_advice(spot: dict, best_evening: dict | None, best_morning: dict | None) -> str:
    orientation = spot.get("orientation") or {}
    primary = orientation.get("primary", "")
    azimuth = orientation.get("azimuth_deg")
    focal = spot.get("recommended_focal_length_mm") or []
    lens = "%d-%dmm" % (focal[0], focal[1]) if len(focal) >= 2 else "任意焦段"
    direction = "%s向" % primary if primary else ""
    if azimuth is not None:
        direction = "%s%s" % (direction, " %.0f°" % azimuth)
    when = "日落"
    if best_evening and best_morning:
        when = "日落" if best_evening["bci"] >= best_morning["bci"] else "日出"
    elif best_morning and not best_evening:
        when = "日出"
    return "建议%s时段朝%s取景，推荐%s" % (when, direction.strip(), lens)


def _spot_status(spot: dict, hourly: list, best_evening, best_morning, ground) -> dict:
    peak_bci = max(hourly, key=lambda item: item["bci"]) if hourly else None
    peak_sci_any = max(hourly, key=lambda item: item["sci"]) if hourly else None
    observable = [entry for entry in hourly if entry.get("sci_observable")]
    peak_sci = max(observable, key=lambda item: item["sci"]) if observable else None
    preferences = spot.get("preferences") or {}
    return {
        "burning_cloud_applicable": bool(preferences.get("burning_cloud", True)),
        "sea_of_clouds_applicable": bool(preferences.get("sea_of_clouds", False)),
        "peak_bci": peak_bci["bci"] if peak_bci else None,
        "peak_bci_grade": peak_bci["bci_grade"] if peak_bci else None,
        "peak_bci_time_local": peak_bci["time_local"] if peak_bci else None,
        "peak_sci": peak_sci["sci"] if peak_sci else None,
        "peak_sci_status": peak_sci["sci_status"] if peak_sci else None,
        "peak_sci_state": peak_sci["sci_state"] if peak_sci else None,
        "peak_sci_time_local": peak_sci["time_local"] if peak_sci else None,
        "peak_sci_cloud_top_m": peak_sci["cloud_top_m"] if peak_sci else None,
        "peak_sci_inversion_height_m": peak_sci["inversion_height_m"] if peak_sci else None,
        "peak_sci_any_time": peak_sci_any["sci"] if peak_sci_any else None,
        "peak_sci_any_time_local": peak_sci_any["time_local"] if peak_sci_any else None,
        "best_evening_grade": best_evening["bci_grade"] if best_evening else None,
        "best_morning_grade": best_morning["bci_grade"] if best_morning else None,
        "ground_source": ground.source,
        "weather_text": ground.text,
        "weather_icon": ground.icon,
        "orientation_advice": _orientation_advice(spot, best_evening, best_morning),
    }


def _select_best_sea(hourly: list):
    candidates = [
        entry
        for entry in hourly
        if entry.get("sci") is not None and entry.get("sci_observable")
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item["sci"])
    keys = (
        "time",
        "time_local",
        "sci",
        "sci_status",
        "sci_state",
        "inversion_height_m",
        "cloud_base_m",
        "cloud_top_m",
    )
    return {key: best[key] for key in keys}


def _build_spot(spot: dict, series: HourlySeries, ground: GroundWeather, hours: int) -> dict:
    count = min(hours, len(series))
    hourly = [_hourly_entry(spot, series, ground, i) for i in range(count)]
    best_evening = _select_best(hourly, evening=True, spot=spot)
    best_morning = _select_best(hourly, evening=False, spot=spot)
    best_sea = _select_best_sea(hourly)
    return {
        "id": spot["id"],
        "name": spot["name"],
        "lat": spot["lat"],
        "lon": spot["lon"],
        "elevation_m": spot["elevation_m"],
        "region": spot.get("region"),
        "preferences": spot.get("preferences", {}),
        "orientation": spot.get("orientation", {}),
        "recommended_focal_length_mm": spot.get("recommended_focal_length_mm", []),
        "ground": ground.to_dict(),
        "best_evening": best_evening,
        "best_morning": best_morning,
        "best_sea": best_sea,
        "best_sunset": best_evening,
        "best_sunrise": best_morning,
        "status": _spot_status(spot, hourly, best_evening, best_morning, ground),
        "hourly": hourly,
    }


# ---------------------------------------------------------------------------
# grid · unified-time golden-window snapshots
# ---------------------------------------------------------------------------
def grid_coordinates(bounds=None, step=GRID_STEP):
    """Yield ``(lat, lon)`` grid points covering *bounds*."""
    bounds = bounds or GRID_BOUNDS
    lat_count = int(round((bounds["lat_max"] - bounds["lat_min"]) / step)) + 1
    lon_count = int(round((bounds["lon_max"] - bounds["lon_min"]) / step)) + 1
    for i in range(lat_count):
        lat = round(bounds["lat_min"] + i * step, 4)
        for j in range(lon_count):
            lon = round(bounds["lon_min"] + j * step, 4)
            yield lat, lon


def _local_date_offset(start: datetime, offset: int):
    """Local calendar date *offset* days after *start*."""
    return _local(start).date() + timedelta(days=offset)


def _nearest_index(times, instant: datetime):
    """Index of the hour in *times* closest to *instant* (``None`` if empty)."""
    best_index = None
    best_delta = None
    for i, raw in enumerate(times):
        delta = abs((_parse_time(raw) - instant).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_index = i
    return best_index


def golden_snapshot_windows(center_lat: float, center_lon: float, start: datetime,
                            days: int = SNAPSHOT_DAY_COUNT) -> list:
    """Enumerate the golden sunrise/sunset windows for the next *days* days.

    Each window carries a single representative UTC ``instant`` (the midpoint of
    the grazing-light band) so that every grid cell can be sampled at the *same
    time* — the fix for the "different hours stitched onto one map" bug.
    """
    windows = []
    for offset in range(days):
        local_day = _local_date_offset(start, offset)
        day_windows = solar.golden_hour_windows(center_lat, center_lon, local_day)
        for event in ("sunrise", "sunset"):
            window = day_windows.get("morning" if event == "sunrise" else "evening")
            if not window:
                continue
            t0, t1 = window
            instant = t0 + (t1 - t0) / 2
            token = _DAY_TOKENS[offset] if offset < len(_DAY_TOKENS) else "day%d" % (offset + 1)
            key = "%s_%s" % (token, event)
            windows.append(
                {
                    "key": key,
                    "day_offset": offset,
                    "event": event,
                    "label": _SNAPSHOT_LABELS.get(key, key),
                    "icon": _EVENT_ICON.get(event, "🕐"),
                    "local_date": local_day.isoformat(),
                    "weekday": _WEEKDAYS[local_day.weekday()],
                    "instant": instant,
                }
            )
    return windows


def _grid_level(value: float) -> int:
    return int(_clamp(value // 20.0, 0.0, 4.0))


def _snapshot_point(series: HourlySeries, index: int, lat: float, lon: float,
                    visibility_km, aqi) -> dict:
    """One grid cell's unified-time cloud / BCI / SCI sample."""
    instant = _parse_time(series.time[index])
    position = solar.solar_position(lat, lon, instant)
    cloud_low = float(_value(series, "cloud_cover_low", index, 0.0))
    cloud_mid = float(_value(series, "cloud_cover_mid", index, 0.0))
    cloud_high = float(_value(series, "cloud_cover_high", index, 0.0))
    wind = _value(series, "wind_speed_10m", index)

    bci = burning_cloud.compute_bci(
        cloud_low=cloud_low,
        cloud_mid=cloud_mid,
        cloud_high=cloud_high,
        visibility_km=visibility_km,
        aqi=aqi,
        sun_elevation=position["elevation"],
        sun_azimuth=position["azimuth"],
        horizon_low_cloud=cloud_low,
    )
    elevation = series.elevation if series.elevation is not None else 0.0
    sci = sea_of_clouds.compute_sci(
        _pressure_profile(series, index),
        viewpoint_elevation=float(elevation),
        wind_speed=wind,
    )
    return {
        "lat": lat,
        "lon": lon,
        "elevation_m": round(float(elevation), 1),
        "cloud_low": round(cloud_low, 1),
        "cloud_mid": round(cloud_mid, 1),
        "cloud_high": round(cloud_high, 1),
        "cloud_midhigh": round(min(100.0, cloud_mid + cloud_high), 1),
        "bci": bci["bci"],
        "grade": bci["grade"],
        "level": _grid_level(bci["bci"]),
        "flags": bci.get("flags") or [],
        "sci": sci["sci"],
        "sci_level": _grid_level(sci["sci"]),
        "sci_state": sci["state"],
        "inversion_height_m": _round_or_none(sci.get("inversion_height")),
        "cloud_top_m": _round_or_none(sci.get("cloud_top")),
    }


def build_snapshot_grid(
    coordinates,
    series_list,
    *,
    bounds=None,
    step: float = GRID_STEP,
    start: datetime,
    visibility_km: float = 15.0,
    aqi: float | None = 50.0,
) -> dict:
    """Build the unified-time golden-window snapshot grid.

    Unlike the old peak-in-72h field this evaluates **every** grid cell at the
    exact same instant for each golden window, preserving the spatial
    continuity of the meteorological field so the rendered cloud deck is a
    single coherent system instead of a mosaic of unrelated hours.
    """
    bounds = bounds or GRID_BOUNDS
    coords = list(coordinates)
    series_by_coord = {
        (round(s.latitude, 4), round(s.longitude, 4)): s for s in series_list
    }
    center = (
        (bounds["lat_min"] + bounds["lat_max"]) / 2.0,
        (bounds["lon_min"] + bounds["lon_max"]) / 2.0,
    )

    reference = next((s for s in series_list if len(s) > 0), None)
    times = list(reference.time) if reference is not None else []

    snapshots = []
    if times:
        first_time = _parse_time(times[0])
        last_time = _parse_time(times[-1])
        for window in golden_snapshot_windows(center[0], center[1], start):
            t0 = window["instant"] - timedelta(minutes=45)
            t1 = window["instant"] + timedelta(minutes=45)
            if t1 <= first_time + timedelta(minutes=1):
                continue  # window already in the past
            if t0 >= last_time:
                continue  # beyond the forecast horizon
            index = _nearest_index(times, window["instant"])
            if index is None:
                continue
            reference_time = _parse_time(times[index])
            position = solar.solar_position(center[0], center[1], reference_time)

            points = []
            for lat, lon in coords:
                series = series_by_coord.get((round(lat, 4), round(lon, 4)))
                if series is None or len(series) == 0 or index >= len(series):
                    continue
                raw_visibility = series.value("visibility", index)
                if raw_visibility is None:
                    cell_visibility = (
                        float(visibility_km) if visibility_km is not None else 15.0
                    )
                else:
                    cell_visibility = max(0.1, float(raw_visibility) / 1000.0)
                points.append(
                    _snapshot_point(
                        series, index, lat, lon, cell_visibility, None
                    )
                )

            snapshots.append(
                {
                    "key": window["key"],
                    "label": window["label"],
                    "icon": window["icon"],
                    "event": window["event"],
                    "day_offset": window["day_offset"],
                    "date": window["local_date"],
                    "weekday": window["weekday"],
                    "time": _iso_utc(reference_time),
                    "time_local": _local_iso(reference_time),
                    "local_time": _local_hhmm(reference_time),
                    "sun_elevation": round(position["elevation"], 2),
                    "sun_azimuth": round(position["azimuth"], 1),
                    "bounds": dict(bounds),
                    "step": step,
                    "count": len(points),
                    "points": points,
                }
            )

    default_key = snapshots[0]["key"] if snapshots else None
    return {
        "bounds": dict(bounds),
        "step": step,
        "metric": "bci",
        "metrics": ["bci", "sci"],
        "count": snapshots[0]["count"] if snapshots else 0,
        "snapshot_count": len(snapshots),
        "default_snapshot": default_key,
        "snapshots": snapshots,
        "note": "统一时次快照：每个黄金窗口取同一时刻的全区域连续云场",
    }


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
def _highlight(best: dict, spot_name: str) -> str:
    return (
        "%s %s，火烧云指数 %s（%s），云量低/中/高 %s/%s/%s%%，能见度 %skm"
        % (
            _local_hhmm(_parse_time(best["time"])),
            spot_name,
            best["bci"],
            best["bci_grade"],
            best["cloud_low"],
            best["cloud_mid"],
            best["cloud_high"],
            best["visibility_km"],
        )
    )


def build_summary(spots_out: list, start: datetime, days: int = SUMMARY_DAYS) -> list:
    """Aggregate the next few days' best viewing windows."""
    start = _floor_hour(start)
    summary = []
    for day_offset in range(days):
        day_start = start + timedelta(days=day_offset)
        day_local = _local(day_start)
        day_key = day_local.strftime("%Y-%m-%d")

        day_entries = []
        for spot in spots_out:
            for entry in spot["hourly"]:
                if entry["time_local"].startswith(day_key):
                    day_entries.append((spot, entry))
        if not day_entries:
            continue

        evening_pool = [
            (spot, entry)
            for spot, entry in day_entries
            if entry["sun_hour_angle"] > 0 and -5.0 <= entry["sun_elevation"] <= 8.0
        ]
        morning_pool = [
            (spot, entry)
            for spot, entry in day_entries
            if entry["sun_hour_angle"] < 0 and -5.0 <= entry["sun_elevation"] <= 8.0
        ]

        evening = _best_of(evening_pool) if evening_pool else None
        morning = _best_of(morning_pool) if morning_pool else None
        sea = _best_sea_of(day_entries) if day_entries else None
        summary.append(
            {
                "date": day_key,
                "weekday": _WEEKDAYS[day_local.weekday() % 7],
                "evening": evening,
                "morning": morning,
                "sea": sea,
            }
        )
    return summary


def _best_of(pool):
    spot, entry = max(pool, key=lambda item: item[1]["bci"])
    return {
        "spot_id": spot["id"],
        "spot_name": spot["name"],
        "region": spot.get("region"),
        "time": entry["time"],
        "time_local": entry["time_local"],
        "bci": entry["bci"],
        "bci_grade": entry["bci_grade"],
        "sci": entry["sci"],
        "sci_status": entry["sci_status"],
        "highlight": _highlight(entry, spot["name"]),
    }


def _sea_highlight(entry: dict, spot_name: str) -> str:
    top = entry.get("cloud_top_m")
    inv = entry.get("inversion_height_m")
    top_text = "%sm" % top if top is not None else "—"
    inv_text = "%sm" % inv if inv is not None else "—"
    return (
        "%s %s，云海指数 %s（%s），云顶高度 %s、逆温高度 %s"
        % (
            _local_hhmm(_parse_time(entry["time"])),
            spot_name,
            entry["sci"],
            entry.get("sci_status", ""),
            top_text,
            inv_text,
        )
    )


def _best_sea_of(pool):
    candidates = [item for item in pool if item[1].get("sci_observable")]
    if not candidates:
        return None
    spot, entry = max(candidates, key=lambda item: item[1]["sci"])
    return {
        "spot_id": spot["id"],
        "spot_name": spot["name"],
        "region": spot.get("region"),
        "time": entry["time"],
        "time_local": entry["time_local"],
        "sci": entry["sci"],
        "sci_status": entry.get("sci_status") or "",
        "sci_state": entry.get("sci_state") or "clear_sky",
        "cloud_top_m": entry.get("cloud_top_m"),
        "inversion_height_m": entry.get("inversion_height_m"),
        "highlight": _sea_highlight(entry, spot["name"]),
    }


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------
def build_forecast(
    config: dict,
    *,
    mock: bool = False,
    hours: int = FORECAST_HOURS,
    include_grid: bool = True,
    start: datetime | None = None,
    open_meteo: OpenMeteoClient | None = None,
    qweather: QWeatherClient | None = None,
    grid_step: float = GRID_STEP,
    grid_bounds: dict | None = None,
) -> dict:
    """Build the complete forecast document."""
    start = _floor_hour(start or datetime.now(timezone.utc))
    bounds = dict(grid_bounds or GRID_BOUNDS)
    spots = config.get("spots", [])
    meta = config.get("meta", {})

    om = open_meteo or OpenMeteoClient()
    qw = qweather or QWeatherClient()

    if not spots:
        return {
            "updated_at": _iso_utc(start),
            "meta": {"spot_count": 0, "hours": hours, "mock": mock},
            "spots": [],
            "grid": None,
            "summary": [],
        }

    spot_coords = [(spot["lat"], spot["lon"]) for spot in spots]
    series_list, data_source = _fetch_series(
        om,
        spot_coords,
        hours=hours,
        start=start,
        variables=None,
        force_mock=mock,
    )
    series_by_index = {i: series for i, series in enumerate(series_list)}

    grounds = {}
    for spot in spots:
        grounds[spot["id"]] = qw.fetch(spot["lat"], spot["lon"], spot.get("name", ""))
    ground_source = "qweather" if qw.enabled else "neutral"

    spots_out = []
    for index, spot in enumerate(spots):
        series = series_by_index.get(index)
        if series is None:
            series = HourlySeries(latitude=spot["lat"], longitude=spot["lon"])
        spots_out.append(_build_spot(spot, series, grounds[spot["id"]], hours))

    grid = None
    if include_grid:
        coordinates = list(grid_coordinates(bounds, grid_step))
        grid_series, grid_source = _fetch_series(
            om,
            coordinates,
            hours=hours,
            start=start,
            variables=GRID_VARIABLES,
            force_mock=mock,
        )
        grid = build_snapshot_grid(
            coordinates,
            grid_series,
            bounds=bounds,
            step=grid_step,
            start=start,
        )
        grid["source"] = grid_source

    summary = build_summary(spots_out, start)

    grid_live = grid is None or grid.get("source") == "open-meteo-live"
    live = data_source == "open-meteo-live" and grid_live

    return {
        "updated_at": _iso_utc(start),
        "meta": {
            "name": meta.get("name", "sky-forecast"),
            "region": meta.get("region", "郑州及周边"),
            "timezone": meta.get("timezone", "Asia/Shanghai"),
            "utc_offset_hours": meta.get("utc_offset_hours", LOCAL_UTC_OFFSET_HOURS),
            "hours": hours,
            "spot_count": len(spots_out),
            "mock": not live,
            "data_source": "open-meteo-live" if live else data_source,
            "ground_source": ground_source,
            "ground_visibility_source": (
                "qweather" if ground_source == "qweather" else "open-meteo"
            ),
            "generated_at": _iso_utc(datetime.now(timezone.utc)),
        },
        "spots": spots_out,
        "grid": grid,
        "summary": summary,
    }


def save_forecast(forecast: dict, path=None) -> Path:
    """Write the forecast JSON (pretty, UTF-8) and return the path."""
    target = Path(path) if path else DEFAULT_OUTPUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(forecast, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target

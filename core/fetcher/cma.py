"""中国气象局（CMA）小时预报，作为 Open-Meteo 之外的第二源印证。

接口：``https://weather.cma.cn/api/hourly/<station_id>``

返回结构（实测 2026-11-10）::

    {"msg": "success", "code": 0,
     "data": [{"date": "2026/11/10",
               "list": [{"stationid": "57083",
                         "publishTime": "2026/11/10 12:00",
                         "forecastTime": "2026/11/10 17:00",
                         "hour": 9,          # 相对"今天 08:00"的小时序号，不是钟点！
                         "temperature": 17.7, "humidity": 24.2,
                         "totalCloudCover": 0.0, "lowCloudCover": 0.0,
                         "visibility": 16.4, "windSpeed": 2.6,
                         "text": "晴"}, ...]}, ...]}

两个必须注意的坑：

1. ``data`` 是「按日期分组」的：每项有 ``date`` 和 ``list``。必须先把所有分组
   的 ``list`` 拍平，才能按整点查找。
2. ``hour`` 字段**不是钟点**，而是从「发布日 08:00」起算的序号
   （实测 forecastTime 17:00 对应 hour=9，即 8+9=17）。因此取整点只能用
   ``forecastTime``，绝不能用 ``hour``。

本模块只做「取数 + 按整点查找」，不参与任何评分；任何异常一律降级为
``None``，绝不抛异常打断整份预报。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request

BASE_URL = "https://weather.cma.cn/api/hourly/{station_id}"
REFERER = "https://weather.cma.cn/"

# 省份内地级市 -> CMA 站点号。来自青龙 2051/sunset_monitor.py 的既有映射。
CMA_STATION_IDS = {
    "郑州": "57083",
    "登封": "57083",
    "新密": "57083",
    "洛阳": "57074",
    "伊川": "57074",
    "栾川": "57077",
    "嵩县": "57162",
    "南阳": "57178",
    "西峡": "57178",
    "桐柏": "57178",
    "南召": "57178",
    "内乡": "57178",
    "新乡": "53986",
    "焦作": "53982",
    "信阳": "57297",
    "济源": "53983",
}

_CACHE: dict[str, list] = {}
_CACHE_LOCK = threading.Lock()

_FORECAST_TIME_RE = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})\D+(\d{1,2})")


def _normalize_place(value: str) -> str:
    text = str(value or "").strip()
    for suffix in ("特别行政区", "自治州", "自治县", "地区", "新区", "市", "县", "区", "盟"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def station_for_spot(spot) -> str | None:
    """按 spot 的 ``city`` 精确解析 CMA 站号；解析不到返回 ``None``。

    只按 ``city`` 一级匹配，不用 ``region`` —— region 里的县级段在映射表
    中并不齐全，模糊匹配会把山区点位错配到别的站点。
    """
    if isinstance(spot, dict):
        city = spot.get("city") or ""
    else:
        city = getattr(spot, "city", "") or ""
    key = _normalize_place(city)
    return CMA_STATION_IDS.get(key)


def _flatten(payload) -> list:
    """把 CMA 的「按日期分组」结构拍平成单一逐时行列表。"""
    if not isinstance(payload, dict):
        return []
    groups = payload.get("data")
    if not isinstance(groups, list):
        return []
    rows: list = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        block = group.get("list")
        if isinstance(block, list):
            rows.extend(item for item in block if isinstance(item, dict))
    return rows


def fetch_hourly(station_id, timeout: int = 8) -> list | None:
    """拉取并拍平某站点逐时预报；失败一律返回 ``None``（不抛异常）。

    进程内按 station_id 缓存，同站点只请求一次。
    """
    sid = str(station_id)
    with _CACHE_LOCK:
        cached = _CACHE.get(sid)
    if cached is not None:
        return cached

    request = urllib.request.Request(
        BASE_URL.format(station_id=sid),
        headers={
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                "Mobile/15E148 Safari/604.1"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
        rows = _flatten(json.loads(text))
    except Exception:  # noqa: BLE001 - 第二源是可选的，绝不能致命
        return None
    if not rows:
        return None
    with _CACHE_LOCK:
        _CACHE[sid] = rows
    return rows


def _parse_forecast_time(row: dict):
    """从 ``forecastTime``（如 ``2026/11/10 17:00``）解析出 (年,月,日,时)。

    绝不用 ``row["hour"]``：那是相对发布日 08:00 的序号，不是钟点。
    """
    raw = row.get("forecastTime")
    if not raw:
        return None
    match = _FORECAST_TIME_RE.search(str(raw))
    if not match:
        return None
    year, month, day, hour = (int(g) for g in match.groups())
    if not (0 <= hour <= 23):
        return None
    return year, month, day, hour


def _parse_local_date(value):
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return int(value.year), int(value.month), int(value.day)
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return int(value[0]), int(value[1]), int(value[2])
    match = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def hourly_at(rows, local_date, hour) -> dict | None:
    """在拍平后的逐时列表中取「某本地日期某整点」的那一行；取不到返回 ``None``。"""
    if not rows:
        return None
    date_parts = _parse_local_date(local_date)
    target_hour = None
    try:
        target_hour = int(hour)
    except (TypeError, ValueError):
        return None
    if date_parts is None or not (0 <= target_hour <= 23):
        return None
    target = (date_parts[0], date_parts[1], date_parts[2], target_hour)
    for row in rows:
        if _parse_forecast_time(row) == target:
            return row
    return None


def nearest_at(rows, local_date, hour, max_offset_hours: float = 1.5):
    """就近取整点：先精确匹配，再在 ±``max_offset_hours`` 内找最近的一条。

    CMA 只发布三小时间隔的预报，精确匹配会大面积落空。返回
    ``(row, offset_hours)``，``offset_hours`` 带符号（早于目标为负）；
    越界或找不到返回 ``(None, None)``。
    """
    if not rows:
        return None, None
    row = hourly_at(rows, local_date, hour)
    if row is not None:
        return row, 0.0

    date_parts = _parse_local_date(local_date)
    try:
        target_hour = int(hour)
    except (TypeError, ValueError):
        return None, None
    if date_parts is None or not (0 <= target_hour <= 23):
        return None, None

    from datetime import datetime as _dt

    try:
        target = _dt(date_parts[0], date_parts[1], date_parts[2], target_hour)
    except ValueError:
        return None, None

    best_row = None
    best_offset = None
    for candidate in rows:
        parts = _parse_forecast_time(candidate)
        if parts is None:
            continue
        try:
            moment = _dt(*parts)
        except ValueError:
            continue
        offset = (moment - target).total_seconds() / 3600.0
        if abs(offset) > max_offset_hours:
            continue
        if best_offset is None or abs(offset) < abs(best_offset):
            best_row = candidate
            best_offset = offset
    if best_row is None:
        return None, None
    return best_row, round(best_offset, 2)


def _nearest_value(rows, local_date, hour, field: str, max_offset_hours: float = 1.5):
    row, offset = nearest_at(rows, local_date, hour, max_offset_hours)
    if row is None:
        return None, None
    raw = row.get(field)
    if raw is None:
        return None, offset
    try:
        return float(raw), offset
    except (TypeError, ValueError):
        return None, offset


def nearest_low_cloud(rows, local_date, hour, max_offset_hours: float = 1.5):
    """就近取低云量（%），返回 ``(value, offset_hours)``。"""
    return _nearest_value(rows, local_date, hour, "lowCloudCover", max_offset_hours)


def nearest_visibility(rows, local_date, hour, max_offset_hours: float = 1.5):
    """就近取能见度（km），返回 ``(value, offset_hours)``。"""
    return _nearest_value(rows, local_date, hour, "visibility", max_offset_hours)


def nearest_precipitation(rows, local_date, hour, max_offset_hours: float = 1.5):
    """就近取降水量（mm），返回 ``(value, offset_hours)``。"""
    return _nearest_value(rows, local_date, hour, "precipitation", max_offset_hours)


def low_cloud_at(rows, local_date, hour):
    """取该整点的低云量（%）；缺值时返回 ``None``。"""
    row = hourly_at(rows, local_date, hour)
    if not row:
        return None
    raw = row.get("lowCloudCover")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def visibility_at(rows, local_date, hour):
    """取该整点的能见度（km）；缺值时返回 ``None``。"""
    row = hourly_at(rows, local_date, hour)
    if not row:
        return None
    raw = row.get("visibility")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

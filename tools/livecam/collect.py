#!/usr/bin/env python3
"""按 15 分钟时段采集 B 站慢直播画面与 Open-Meteo 预报，写入预测/实况对照台账。

每个机位在同一个 15 分钟时段（slot）内只抓取一张 JPEG 画面，同时拉取机位所在地
的逐时预报，用仓库现有的海云指数物理模型算出预测值，并把两者写入同一行 JSONL。
日出日落窗口（05-08、17-20 点）可借助更密集的定时任务实现加密采集。

所有 HTTP 请求与 ffmpeg 抓帧均强制直连，忽略 HTTP_PROXY / HTTPS_PROXY。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ: timezone | ZoneInfo = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 缺少 tzdata 时退化为固定东八区
    LOCAL_TZ = timezone(timedelta(hours=8))

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "data" / "livecam"
DEFAULT_CAMS = Path(__file__).resolve().with_name("cams.json")

HTTP_TIMEOUT = 20.0
FFMPEG_TIMEOUT = 25.0
FFMPEG_RETRY_SLEEP = 3.0
MAX_STREAM_FAILURES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://live.bilibili.com/"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
PLAY_API = (
    "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
)
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

GROUND_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "visibility",
    "wind_speed_10m",
    "precipitation",
)
FORECAST_KEYS = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "visibility_m",
    "wind_speed_10m",
    "precipitation",
)
FALLBACK_PRESSURE_LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_BUV_COOKIE: str | None = None


def _load_pressure_levels() -> tuple[int, ...]:
    """读取仓库现有的 PRESSURE_LEVELS，失败时使用同一组兜底值。"""
    path = REPO_ROOT / "core" / "fetcher" / "open_meteo.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "livecam_open_meteo", path
        )
        if spec is None or spec.loader is None:
            raise ImportError("无法创建 open_meteo 模块规格")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return tuple(int(level) for level in module.PRESSURE_LEVELS)
    except Exception:  # noqa: BLE001 - 常量兜底，不影响帧采集
        return FALLBACK_PRESSURE_LEVELS


PRESSURE_LEVELS = _load_pressure_levels()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 15 分钟时段采集 B 站慢直播画面与 Open-Meteo 云海预测。"
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="输出根目录（默认: %(default)s）",
    )
    parser.add_argument(
        "--cams",
        default=str(DEFAULT_CAMS),
        help="机位配置 JSON（默认: %(default)s）",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="只处理指定 room_id，可重复使用",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖同一 15 分钟时段已有记录",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做取流与单帧连通性检查，不写台账",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印帧统计与预测细节",
    )
    return parser


def _load_cams(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("cams") if isinstance(payload, dict) else payload
    if not isinstance(raw, list) or not raw:
        raise ValueError("机位配置必须是非空数组")
    cams = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("第 %d 个机位不是对象" % (index + 1))
        try:
            room_id = int(item["room_id"])
            name = str(item["name"])
            lat = float(item["lat"])
            lon = float(item["lon"])
            elev_m = float(item["elev_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("第 %d 个机位字段不完整: %s" % (index + 1, exc))
        cams.append(
            {
                "room_id": room_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "elev_m": elev_m,
                "coord_note": str(item.get("coord_note", "")),
            }
        )
    return cams


def _request_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("接口返回的不是 JSON 对象")
    return payload


def _buvid_cookie() -> str:
    global _BUV_COOKIE
    if _BUV_COOKIE:
        return _BUV_COOKIE
    payload = _request_json(
        SPI_URL,
        {"User-Agent": USER_AGENT, "Referer": REFERER},
    )
    if payload.get("code") != 0:
        raise RuntimeError(
            "buvid 接口失败: %s" % payload.get("message", payload.get("code"))
        )
    data = payload.get("data") or {}
    b3 = data.get("b_3")
    b4 = data.get("b_4")
    if not b3 or not b4:
        raise RuntimeError("buvid 接口未返回 b_3 / b_4")
    _BUV_COOKIE = "buvid3=%s; buvid4=%s" % (b3, b4)
    return _BUV_COOKIE


def _join_stream_url(host: str, base_url: str, extra: Any) -> str:
    """拼接 host + base_url + extra，避免 base_url 带 ? 时产生 ??。"""
    base = base_url if base_url.startswith("/") else "/" + base_url
    url = host.rstrip("/") + base
    query = "" if extra is None else str(extra).lstrip("?")
    if not query:
        return url
    if url.endswith("?") or url.endswith("&"):
        return url + query
    separator = "&" if "?" in url else "?"
    return url + separator + query


def _resolve_stream_url(room_id: int) -> str:
    params = {
        "room_id": str(room_id),
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0",
        "qn": "10000",
        "platform": "web",
        "ptype": "8",
    }
    url = "%s?%s" % (PLAY_API, urllib.parse.urlencode(params))
    payload = _request_json(
        url,
        {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Cookie": _buvid_cookie(),
        },
    )
    if payload.get("code") != 0:
        raise RuntimeError(
            "取流接口失败: %s" % payload.get("message", payload.get("code"))
        )
    playurl_info = (payload.get("data") or {}).get("playurl_info")
    if not playurl_info:
        raise RuntimeError("房间未开播或未返回 playurl_info")
    streams = (playurl_info.get("playurl") or {}).get("stream") or []
    for stream in streams:
        if stream.get("protocol_name") != "http_hls":
            continue
        for fmt in stream.get("format") or []:
            if fmt.get("format_name") != "fmp4":
                continue
            for codec in fmt.get("codec") or []:
                host = codec.get("host")
                extra = codec.get("extra")
                info = (codec.get("url_info") or [{}])[0]
                if not host:
                    host = info.get("host")
                if extra is None:
                    extra = info.get("extra")
                base_url = codec.get("base_url") or ""
                if host and base_url:
                    return _join_stream_url(str(host), str(base_url), extra)
                if info.get("host") and base_url:
                    return _join_stream_url(
                        str(info["host"]), str(base_url), info.get("extra")
                    )
    raise RuntimeError("未找到 http_hls / fmp4 播放地址")


def _ffmpeg_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        env.pop(key, None)
    return env


def _ffmpeg_input_args(stream_url: str) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-headers",
        "Referer: %s\r\n" % REFERER,
        "-ss",
        "1",
        "-i",
        stream_url,
    ]


def _capture_frame(stream_url: str, target: Path) -> str | None:
    """抓取一帧到 target；成功返回 None，失败返回错误字符串。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    env = _ffmpeg_env()
    last_error = ""
    for attempt in range(2):
        temp = target.with_name(
            ".%s.%d.%d.tmp.jpg" % (target.stem, os.getpid(), attempt)
        )
        command = _ffmpeg_input_args(stream_url) + [
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            str(temp),
        ]
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = "ffmpeg 抓帧超时"
        except OSError as exc:
            last_error = "ffmpeg 启动失败: %s" % exc
        else:
            if (
                completed.returncode == 0
                and temp.exists()
                and temp.stat().st_size > 0
            ):
                os.replace(temp, target)
                return None
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            last_error = "ffmpeg 返回 %d%s" % (
                completed.returncode,
                (": " + stderr[:400]) if stderr else "",
            )
        temp.unlink(missing_ok=True)
        if attempt == 0:
            time.sleep(FFMPEG_RETRY_SLEEP)
    return last_error


def _capture_frame_bytes(stream_url: str) -> bytes:
    """抓取一帧到内存，供 --check 使用，不落盘。"""
    env = _ffmpeg_env()
    last_error = "ffmpeg 抓帧失败"
    for attempt in range(2):
        command = _ffmpeg_input_args(stream_url) + [
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=FFMPEG_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = "ffmpeg 抓帧超时"
        except OSError as exc:
            last_error = "ffmpeg 启动失败: %s" % exc
        else:
            if completed.returncode == 0 and completed.stdout:
                return completed.stdout
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            last_error = "ffmpeg 返回 %d%s" % (
                completed.returncode,
                (": " + stderr[:400]) if stderr else "",
            )
        if attempt == 0:
            time.sleep(FFMPEG_RETRY_SLEEP)
    raise RuntimeError(last_error)


def _percentile(histogram: list[int], total: int, percent: float) -> float:
    target = max(1, math.ceil(total * percent / 100.0))
    cumulative = 0
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            return float(value)
    return 255.0


def _image_stats(path: Path) -> dict[str, Any]:
    file_bytes = path.read_bytes()
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        total = width * height
        gray = image.convert("L")
        histogram = gray.histogram()
        mean = sum(value * count for value, count in enumerate(histogram)) / total
        variance = (
            sum(
                (value - mean) ** 2 * count
                for value, count in enumerate(histogram)
            )
            / total
        )
        dark_frac = sum(histogram[:32]) / total
        bright_frac = sum(histogram[225:]) / total

        band_height = max(1, int(round(height * 0.4)))
        top_mean = ImageStat.Stat(
            gray.crop((0, 0, width, band_height))
        ).mean[0]
        bottom_mean = ImageStat.Stat(
            gray.crop((0, height - band_height, width, height))
        ).mean[0]

        horizontal = ImageChops.difference(
            gray, ImageChops.offset(gray, 1, 0)
        )
        vertical = ImageChops.difference(
            gray, ImageChops.offset(gray, 0, 1)
        )
        mask = ImageChops.lighter(
            horizontal.point(lambda value: 255 if value > 24 else 0),
            vertical.point(lambda value: 255 if value > 24 else 0),
        )
        edge_frac = mask.histogram()[255] / total

        red, green, blue = image.convert("RGB").split()
        red_bytes = red.tobytes()
        green_bytes = green.tobytes()
        blue_bytes = blue.tobytes()
        sum_color = 0.0
        sum_color_sq = 0.0
        for r_value, g_value, b_value in zip(
            red_bytes, green_bytes, blue_bytes
        ):
            difference = (
                abs(r_value - g_value)
                + abs(r_value - b_value)
                + abs(g_value - b_value)
            ) / 3.0
            sum_color += difference
            sum_color_sq += difference * difference
        color_mean = sum_color / total
        color_variance = max(0.0, sum_color_sq / total - color_mean**2)

    return {
        "bytes": len(file_bytes),
        "md5": md5(file_bytes).hexdigest(),
        "width": width,
        "height": height,
        "mean_luma": round(mean, 2),
        "p10_luma": round(_percentile(histogram, total, 10.0), 2),
        "p90_luma": round(_percentile(histogram, total, 90.0), 2),
        "std_luma": round(math.sqrt(max(0.0, variance)), 2),
        "dark_frac": round(dark_frac, 6),
        "bright_frac": round(bright_frac, 6),
        "edge_frac": round(edge_frac, 6),
        "top_mean_luma": round(top_mean, 2),
        "bottom_mean_luma": round(bottom_mean, 2),
        "colorfulness": round(math.sqrt(color_variance), 4),
    }


def _frame_failure(path: Path, out_dir: Path, error: str) -> dict[str, Any]:
    relative = path.relative_to(out_dir) if path.is_relative_to(out_dir) else path
    return {
        "path": str(relative),
        "ok": False,
        "bytes": None,
        "md5": None,
        "width": None,
        "height": None,
        "mean_luma": None,
        "p10_luma": None,
        "p90_luma": None,
        "std_luma": None,
        "dark_frac": None,
        "bright_frac": None,
        "edge_frac": None,
        "top_mean_luma": None,
        "bottom_mean_luma": None,
        "colorfulness": None,
        "error": error,
    }


def _frame_record(path: Path, out_dir: Path) -> dict[str, Any]:
    stats = _image_stats(path)
    relative = path.relative_to(out_dir) if path.is_relative_to(out_dir) else path
    return {
        "path": str(relative),
        "ok": True,
        "bytes": stats["bytes"],
        "md5": stats["md5"],
        "width": stats["width"],
        "height": stats["height"],
        "mean_luma": stats["mean_luma"],
        "p10_luma": stats["p10_luma"],
        "p90_luma": stats["p90_luma"],
        "std_luma": stats["std_luma"],
        "dark_frac": stats["dark_frac"],
        "bright_frac": stats["bright_frac"],
        "edge_frac": stats["edge_frac"],
        "top_mean_luma": stats["top_mean_luma"],
        "bottom_mean_luma": stats["bottom_mean_luma"],
        "colorfulness": stats["colorfulness"],
        "error": None,
    }


def _forecast_variables() -> list[str]:
    names = list(GROUND_VARIABLES)
    for level in PRESSURE_LEVELS:
        names.extend(
            [
                "geopotential_height_%dhPa" % level,
                "temperature_%dhPa" % level,
                "relative_humidity_%dhPa" % level,
            ]
        )
    return names


def _value_at(hourly: dict[str, Any], name: str, index: int) -> Any:
    series = hourly.get(name)
    if not isinstance(series, list) or index < 0 or index >= len(series):
        return None
    return series[index]


def _match_hour(times: list[str], hour_local: str) -> int | None:
    if hour_local in times:
        return times.index(hour_local)
    try:
        target = datetime.strptime(hour_local, "%Y-%m-%dT%H:%M").replace(
            tzinfo=LOCAL_TZ
        )
    except ValueError:
        return None
    best: tuple[datetime, int] | None = None
    for index, raw in enumerate(times):
        try:
            instant = datetime.strptime(
                str(raw), "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
        if instant >= target and (best is None or instant < best[0]):
            best = (instant, index)
    return best[1] if best else None


def _number(value: Any, digits: int) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _empty_forecast() -> dict[str, Any]:
    return {key: None for key in FORECAST_KEYS}


def _fetch_forecast(
    cam: dict[str, Any], hour_local: str
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    params = {
        "latitude": "%.4f" % cam["lat"],
        "longitude": "%.4f" % cam["lon"],
        "elevation": "%d" % round(cam["elev_m"]),
        "hourly": ",".join(_forecast_variables()),
        "forecast_days": "2",
        "timezone": "Asia/Shanghai",
    }
    url = "%s?%s" % (FORECAST_API, urllib.parse.urlencode(params))
    payload = _request_json(url, {"User-Agent": "sky-forecast-livecam/1.0"})
    if payload.get("error"):
        raise RuntimeError(str(payload.get("reason", "open-meteo error")))
    hourly = payload.get("hourly") or {}
    times = [str(item) for item in (hourly.get("time") or [])]
    index = _match_hour(times, hour_local)
    if index is None:
        raise RuntimeError("预报未覆盖当前小时 %s" % hour_local)

    visibility = _value_at(hourly, "visibility", index)
    forecast = {
        "temperature_2m": _number(
            _value_at(hourly, "temperature_2m", index), 2
        ),
        "relative_humidity_2m": _number(
            _value_at(hourly, "relative_humidity_2m", index), 1
        ),
        "dew_point_2m": _number(
            _value_at(hourly, "dew_point_2m", index), 2
        ),
        "cloud_cover_low": _number(
            _value_at(hourly, "cloud_cover_low", index), 1
        ),
        "cloud_cover_mid": _number(
            _value_at(hourly, "cloud_cover_mid", index), 1
        ),
        "cloud_cover_high": _number(
            _value_at(hourly, "cloud_cover_high", index), 1
        ),
        "visibility_m": _number(visibility, 0),
        "wind_speed_10m": _number(
            _value_at(hourly, "wind_speed_10m", index), 2
        ),
        "precipitation": _number(
            _value_at(hourly, "precipitation", index), 2
        ),
    }

    profile: list[dict[str, Any]] = []
    for level in PRESSURE_LEVELS:
        temperature = _value_at(
            hourly, "temperature_%dhPa" % level, index
        )
        if temperature is None:
            continue
        height = _value_at(
            hourly, "geopotential_height_%dhPa" % level, index
        )
        if height is None:
            continue
        humidity = _value_at(
            hourly, "relative_humidity_%dhPa" % level, index
        )
        profile.append(
            {
                "pressure": level,
                "height": float(height),
                "temperature": float(temperature),
                "relative_humidity": (
                    None if humidity is None else float(humidity)
                ),
            }
        )
    if not profile:
        raise RuntimeError("探空廓线为空")
    return times[index], forecast, profile


def _load_physics() -> Any:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    path = REPO_ROOT / "core" / "physics" / "sea_of_clouds.py"
    spec = importlib.util.spec_from_file_location(
        "livecam_sea_of_clouds", path
    )
    if spec is None or spec.loader is None:
        raise ImportError("无法创建 sea_of_clouds 模块规格")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _predict(
    physics_module: Any,
    cam: dict[str, Any],
    forecast: dict[str, Any],
    profile: list[dict[str, Any]],
) -> dict[str, Any]:
    visibility = forecast.get("visibility_m")
    temperature = forecast.get("temperature_2m")
    dew_point = forecast.get("dew_point_2m")
    spread = (
        float(temperature) - float(dew_point)
        if temperature is not None and dew_point is not None
        else None
    )
    result = physics_module.compute_sci(
        profile,
        float(cam["elev_m"]),
        wind_speed=forecast.get("wind_speed_10m"),
        visibility_km=(
            float(visibility) / 1000.0 if visibility is not None else None
        ),
        dewpoint_spread=spread,
    )
    return {
        "sci": result.get("sci"),
        "status": result.get("status"),
        "state": result.get("state"),
        "inversion_height_m": result.get("inversion_height"),
        "components": result.get("components") or {},
    }


def _slot_local(local: datetime) -> str:
    """把本地时刻向下取整到 15 分钟，返回 YYYY-MM-DDTHH:MM。"""
    slot = local.replace(
        minute=(local.minute // 15) * 15, second=0, microsecond=0
    )
    return slot.strftime("%Y-%m-%dT%H:%M")


def _mode_for(local: datetime) -> str:
    """按 slot 的分钟位判定采样节奏：整点为 hourly，15/30/45 为 dense。"""
    minute = (local.minute // 15) * 15
    return "hourly" if minute == 0 else "dense"


def _in_window(local: datetime) -> bool:
    """该本地时刻是否落在日出日落窗口（05-08、17-20，含端点）。"""
    hour = local.hour
    return 5 <= hour <= 8 or 17 <= hour <= 20


def _process_cam(
    cam: dict[str, Any],
    out_dir: Path,
    slot_local: str,
    hour_local: str,
    mode: str,
    in_window: bool,
    started_utc: datetime,
    physics_module: Any,
    physics_error: str | None,
) -> tuple[dict[str, Any], bool, bool]:
    errors: list[str] = []
    frame_path = (
        out_dir
        / "frames"
        / str(cam["room_id"])
        / (slot_local.replace(":", "") + ".jpg")
    )
    frame: dict[str, Any] | None = None
    stream_ok = True

    try:
        stream_url = _resolve_stream_url(int(cam["room_id"]))
    except Exception as exc:  # noqa: BLE001 - 单机位失败不中断整轮
        stream_ok = False
        message = "取流失败: %s" % exc
        errors.append(message)
        frame = _frame_failure(frame_path, out_dir, message)
        stream_url = None

    if stream_url:
        capture_error = _capture_frame(stream_url, frame_path)
        if capture_error:
            stream_ok = False
            message = "抓帧失败: %s" % capture_error
            errors.append(message)
            frame = _frame_failure(frame_path, out_dir, message)
        else:
            try:
                frame = _frame_record(frame_path, out_dir)
            except Exception as exc:  # noqa: BLE001 - 坏帧按失败记录
                frame_path.unlink(missing_ok=True)
                stream_ok = False
                message = "帧统计失败: %s" % exc
                errors.append(message)
                frame = _frame_failure(frame_path, out_dir, message)

    forecast_at: str | None = None
    forecast = _empty_forecast()
    profile: list[dict[str, Any]] | None = None
    try:
        forecast_at, forecast, profile = _fetch_forecast(cam, hour_local)
    except Exception as exc:  # noqa: BLE001 - 预报失败不影响已抓帧
        errors.append("预报失败: %s" % exc)

    prediction: dict[str, Any] | None = None
    if physics_module is None:
        errors.append("物理模型导入失败: %s" % physics_error)
    elif profile is not None:
        try:
            prediction = _predict(physics_module, cam, forecast, profile)
        except Exception as exc:  # noqa: BLE001 - 预测失败写 null
            errors.append("预测失败: %s" % exc)

    local = started_utc.astimezone(LOCAL_TZ)
    record = {
        "ts_utc": started_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_local": local.isoformat(timespec="seconds"),
        "slot_local": slot_local,
        "hour_local": hour_local,
        "mode": mode,
        "in_window": in_window,
        "room_id": int(cam["room_id"]),
        "cam_name": cam["name"],
        "coord_note": cam["coord_note"],
        "frame": frame,
        "forecast_at": forecast_at,
        "forecast": forecast,
        "prediction": prediction,
        "errors": errors,
    }
    success = bool(frame and frame.get("ok") and prediction is not None)
    return record, stream_ok, success


def _existing_keys(ledger_dir: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not ledger_dir.exists():
        return keys
    for path in sorted(ledger_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            room_id = record.get("room_id")
            # 历史记录没有 slot_local 时，退回用 hour_local 作为时段键。
            slot = record.get("slot_local") or record.get("hour_local")
            if room_id is not None and slot:
                keys.add((str(room_id), str(slot)))
    return keys


def _append_record(out_dir: Path, record: dict[str, Any], force: bool) -> None:
    ledger_dir = out_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    month = str(record["hour_local"])[:7]
    path = ledger_dir / ("%s.jsonl" % month)
    line = json.dumps(record, ensure_ascii=False)

    if force and path.exists():
        kept: list[str] = []
        incoming_slot = record.get("slot_local") or record["hour_local"]
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                kept.append(raw)
                continue
            # 优先按 slot_local 覆盖，历史行没有该字段时退回 hour_local。
            existing_slot = existing.get("slot_local") or existing.get(
                "hour_local"
            )
            if (
                str(existing.get("room_id")) == str(record["room_id"])
                and existing_slot == incoming_slot
            ):
                continue
            kept.append(raw)
        content = "\n".join(kept)
        if content:
            content += "\n"
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _run_check(cams: list[dict[str, Any]]) -> int:
    started = time.monotonic()
    succeeded = 0
    failed = 0
    for index, cam in enumerate(cams, 1):
        label = "[%d/%d] %s %s" % (
            index,
            len(cams),
            cam["room_id"],
            cam["name"],
        )
        try:
            stream_url = _resolve_stream_url(int(cam["room_id"]))
            data = _capture_frame_bytes(stream_url)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
        except Exception as exc:  # noqa: BLE001 - 检查模式逐路报告
            failed += 1
            print("%s: 失败 (%s)" % (label, exc))
        else:
            succeeded += 1
            print(
                "%s: 连通 (%dx%d, %d bytes)"
                % (label, width, height, len(data))
            )
    elapsed = time.monotonic() - started
    print(
        "检查汇总: 连通 %d / 失败 %d / 耗时 %.1fs"
        % (succeeded, failed, elapsed)
    )
    return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out_dir = Path(args.out).expanduser().resolve()
    cams_path = Path(args.cams).expanduser()

    try:
        cams = _load_cams(cams_path)
    except Exception as exc:  # noqa: BLE001 - 配置错误直接报告
        print("机位配置读取失败: %s" % exc, file=sys.stderr)
        return 2

    if args.only:
        wanted = {str(value) for value in args.only}
        cams = [cam for cam in cams if str(cam["room_id"]) in wanted]
        found = {str(cam["room_id"]) for cam in cams}
        missing = sorted(wanted - found)
        if missing:
            print("未找到机位: %s" % ", ".join(missing), file=sys.stderr)
        if not cams:
            return 2

    if args.check:
        return _run_check(cams)

    physics_module: Any = None
    physics_error: str | None = None
    try:
        physics_module = _load_physics()
    except Exception as exc:  # noqa: BLE001 - 预测降级为 null
        physics_error = str(exc)
        print(
            "警告: 物理模型导入失败，prediction 将写 null: %s" % exc,
            file=sys.stderr,
        )

    existing = _existing_keys(out_dir / "ledger")
    started = time.monotonic()
    succeeded = 0
    failed = 0
    skipped = 0
    consecutive_stream_failures = 0

    for index, cam in enumerate(cams, 1):
        started_utc = datetime.now(timezone.utc)
        local = started_utc.astimezone(LOCAL_TZ)
        slot_local = _slot_local(local)
        hour_local = local.strftime("%Y-%m-%dT%H:00")
        mode = _mode_for(local)
        in_window = _in_window(local)
        key = (str(cam["room_id"]), slot_local)
        label = "[%d/%d] %s %s" % (
            index,
            len(cams),
            cam["room_id"],
            cam["name"],
        )

        if key in existing and not args.force:
            skipped += 1
            consecutive_stream_failures = 0
            print("%s: 跳过（本时段已有记录: %s）" % (label, slot_local))
            continue

        try:
            record, stream_ok, cam_success = _process_cam(
                cam,
                out_dir,
                slot_local,
                hour_local,
                mode,
                in_window,
                started_utc,
                physics_module,
                physics_error,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底保证整轮继续
            failed += 1
            consecutive_stream_failures += 1
            print("%s: 失败 (未预期错误: %s)" % (label, exc))
            if consecutive_stream_failures >= MAX_STREAM_FAILURES:
                print(
                    "提前结束: 连续 %d 个机位取流失败"
                    % consecutive_stream_failures
                )
                break
            continue

        try:
            _append_record(out_dir, record, args.force)
        except Exception as exc:  # noqa: BLE001 - 台账写入失败按该机位失败计
            failed += 1
            cam_success = False
            print("%s: 台账写入失败 (%s)" % (label, exc))
        else:
            existing.add(key)
            if cam_success:
                succeeded += 1
            else:
                failed += 1
            print("%s: %s" % (label, "成功" if cam_success else "失败"))

        if args.verbose:
            frame = record.get("frame") or {}
            prediction = record.get("prediction") or {}
            print(
                "    帧: ok=%s bytes=%s mean_luma=%s dark_frac=%s edge_frac=%s"
                % (
                    frame.get("ok"),
                    frame.get("bytes"),
                    frame.get("mean_luma"),
                    frame.get("dark_frac"),
                    frame.get("edge_frac"),
                )
            )
            print(
                "    预报: at=%s 预测: sci=%s status=%s errors=%s"
                % (
                    record.get("forecast_at"),
                    prediction.get("sci"),
                    prediction.get("status"),
                    record.get("errors"),
                )
            )

        if stream_ok:
            consecutive_stream_failures = 0
        else:
            consecutive_stream_failures += 1
            if consecutive_stream_failures >= MAX_STREAM_FAILURES:
                print(
                    "提前结束: 连续 %d 个机位取流失败"
                    % consecutive_stream_failures
                )
                break

    elapsed = time.monotonic() - started
    print(
        "汇总: 成功 %d / 失败 %d / 跳过 %d / 耗时 %.1fs"
        % (succeeded, failed, skipped, elapsed)
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""QWeather (和风天气) ground-observation client.

Provides ground-level visibility, air-quality index, present-weather text and
icon for a coordinate. The API key is read from the ``QWEATHER_KEY``
environment variable **only** -- it is never hardcoded.

When no key is configured, or when any request fails, the client silently
returns a neutral default observation (``source="neutral"``) instead of
raising, so the forecast pipeline never breaks in offline / unauthenticated
environments.  The most recent outcome is exposed through ``last_status`` /
``last_error`` / ``last_http_code`` so callers can report failures honestly.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("sky_forecast.qweather")

__all__ = [
    "QWeatherClient",
    "GroundWeather",
    "NEUTRAL_VISIBILITY_KM",
    "NEUTRAL_AQI",
    "NEUTRAL_TEXT",
    "NEUTRAL_ICON",
]

DEFAULT_BASE_URL = "https://devapi.qweather.com/v7"
DEFAULT_TIMEOUT = 8.0

_V7_SUFFIXES = ("/v7", "/weather", "/airquality")


def _normalize_base_url(raw: str) -> str:
    """Normalize a user-supplied QWeather API host.

    The QWeather console shows only a bare host such as
    ``h2a9cfmhs.xy.qweatherapi.com`` (no scheme), so accept that form and add
    ``https://``.  Trailing slashes and accidentally pasted API path suffixes
    (``/v7``, ``/weather``, ``/airquality``) are stripped so callers can safely
    append endpoint paths without producing duplicate segments or ``//``.
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT_BASE_URL
    if "://" not in value:
        value = "https://" + value
    value = value.rstrip("/")
    for suffix in _V7_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 0.5

# "Standard neutral" baseline used offline: a middling, unremarkable sky.
NEUTRAL_VISIBILITY_KM = 15.0
NEUTRAL_AQI = 50.0
NEUTRAL_TEXT = "多云"
NEUTRAL_ICON = "101"


@dataclass
class GroundWeather:
    """Ground truth used by the Burning Cloud Index."""

    visibility_km: float = NEUTRAL_VISIBILITY_KM
    aqi: float | None = NEUTRAL_AQI
    text: str = NEUTRAL_TEXT
    icon: str = NEUTRAL_ICON
    source: str = "neutral"
    obs_time: str | None = None

    def to_dict(self) -> dict:
        return {
            "visibility_km": self.visibility_km,
            "aqi": self.aqi,
            "text": self.text,
            "icon": self.icon,
            "source": self.source,
            "obs_time": self.obs_time,
        }


def _as_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode_body(raw: bytes) -> dict:
    """Decode a response body that may be gzip-compressed.

    QWeather compresses every API response with gzip, even when the client
    does not send ``Accept-Encoding``.  Detect the magic bytes, decompress
    and fall back to decoding the raw bytes if decompression fails.
    """
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001 - fall back to decoding raw bytes
            pass
    return json.loads(raw.decode("utf-8"))


def _error_title(payload):
    """Extract ``error.title`` / ``error.status`` from a response body."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        title = error.get("title")
        if title:
            return str(title)
        status = error.get("status")
        if status is not None:
            return "HTTP %s" % status
        return None
    if isinstance(error, str) and error:
        return error
    message = payload.get("message")
    if message:
        return str(message)
    return None


def _format_failure(code, title):
    parts = []
    if code is not None:
        parts.append("HTTP %s" % code)
    if title:
        parts.append(str(title))
    return ": ".join(parts) if parts else "request failed"


class QWeatherClient:
    """Best-effort QWeather client with an always-safe fallback."""

    def __init__(
        self,
        key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        transport=None,
    ) -> None:
        self.key = (key if key is not None else os.environ.get("QWEATHER_KEY", "")).strip()
        raw_base_url = (
            base_url
            if base_url is not None
            else os.environ.get("QWEATHER_BASE_URL") or DEFAULT_BASE_URL
        )
        self.base_url = _normalize_base_url(raw_base_url)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self._transport = transport or self._http_get
        self.last_status = "disabled"
        self.last_error: str | None = None
        self.last_http_code: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    @property
    def _use_v1(self) -> bool:
        if "qweatherapi.com" in self.base_url:
            return True
        if "devapi.qweather.com" in self.base_url or "api.qweather.com" in self.base_url:
            return False
        return True

    def _sanitize(self, text):
        if text and self.key:
            text = str(text).replace(self.key, "***")
        return text

    # -- low level ---------------------------------------------------------
    @staticmethod
    def _http_get(url: str, timeout: float, headers: dict | None = None) -> dict:
        request_headers = {"User-Agent": "sky-forecast/1.0 (+offline-capable)"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return _decode_body(raw)

    def _request(self, path: str, params: dict | None = None) -> dict | None:
        query = dict(params or {})
        headers = None
        if self._use_v1:
            headers = {"X-QW-Api-Key": self.key}
        else:
            query["key"] = self.key
        url = "%s/%s" % (self.base_url, path.lstrip("/"))
        if query:
            url += "?" + urllib.parse.urlencode(query)

        last_code = None
        last_failure = None
        for attempt in range(self.retries + 1):
            try:
                if headers is None:
                    payload = self._transport(url, self.timeout)
                else:
                    payload = self._transport(url, self.timeout, headers=headers)
                if isinstance(payload, dict):
                    raw_code = payload.get("code")
                    if self._use_v1 and raw_code is None and not payload.get("error"):
                        self.last_http_code = 200
                        return payload
                    if str(raw_code) == "200" and not payload.get("error"):
                        self.last_http_code = 200
                        return payload
                    last_code = _as_int(raw_code)
                    if last_code is None:
                        last_code = 200
                    last_failure = _format_failure(last_code, _error_title(payload))
                else:
                    last_code = None
                    last_failure = "unexpected response type: %s" % type(payload).__name__
            except urllib.error.HTTPError as exc:
                last_code = exc.code
                body = b""
                try:
                    body = exc.read()
                except Exception:  # noqa: BLE001 - body is optional diagnostics
                    body = b""
                payload = None
                if body:
                    try:
                        payload = _decode_body(body)
                    except (ValueError, UnicodeDecodeError):
                        payload = None
                last_failure = _format_failure(last_code, _error_title(payload))
            except Exception as exc:  # noqa: BLE001 - never raise to caller
                last_code = None
                last_failure = self._sanitize(
                    "%s: %s" % (type(exc).__name__, exc)
                )
            if attempt < self.retries:
                _time.sleep(self.backoff * (2.0 ** attempt))

        self.last_http_code = last_code
        self.last_error = self._sanitize(last_failure or "request failed")
        return None

    # -- public ------------------------------------------------------------
    @staticmethod
    def neutral() -> GroundWeather:
        return GroundWeather()

    def fetch(self, lat: float, lon: float, name: str = "") -> GroundWeather:
        """Return ground weather for ``lat``/``lon`` or a neutral default."""
        self.last_status = "disabled"
        self.last_error = None
        self.last_http_code = None
        if not self.enabled:
            return self.neutral()

        result = self._fetch_v1(lat, lon) if self._use_v1 else self._fetch_v7(lat, lon)
        if result is None:
            self.last_status = "error"
            if not self.last_error:
                self.last_error = "request failed"
            logger.warning("QWeather request failed: %s", self.last_error)
            return self.neutral()

        self.last_status = "ok"
        self.last_error = None
        self.last_http_code = 200
        return result

    def _fetch_v7(self, lat: float, lon: float) -> GroundWeather | None:
        # QWeather expects "lon,lat" ordering.
        location = "%.4f,%.4f" % (float(lon), float(lat))
        now = self._request("weather/now", {"location": location})
        if now is None:
            return None

        observation = now.get("now") or {}
        visibility = _as_float(observation.get("vis"), NEUTRAL_VISIBILITY_KM)
        text = observation.get("text") or NEUTRAL_TEXT
        icon = str(observation.get("icon") or NEUTRAL_ICON)
        obs_time = observation.get("obsTime")

        aqi = None
        for path in ("air/now", "air/current"):
            air = self._request(path, {"location": location})
            if air is not None:
                aqi = _as_float((air.get("now") or {}).get("aqi"), None)
                if aqi is not None:
                    break

        return GroundWeather(
            visibility_km=visibility if visibility is not None else NEUTRAL_VISIBILITY_KM,
            aqi=aqi,
            text=text,
            icon=icon,
            source="qweather",
            obs_time=obs_time,
        )

    def _fetch_v1(self, lat: float, lon: float) -> GroundWeather | None:
        lat_s = "%.2f" % float(lat)
        lon_s = "%.2f" % float(lon)
        now = self._request("weather/v1/current/%s/%s" % (lat_s, lon_s))
        if now is None:
            return None

        observation = now.get("now") or now
        condition = observation.get("condition") or {}
        visibility_obj = observation.get("visibility")
        if isinstance(visibility_obj, dict):
            raw_vis = visibility_obj.get("value")
        else:
            raw_vis = visibility_obj
        vis_m = _as_float(raw_vis, None)
        visibility = vis_m / 1000.0 if vis_m is not None else NEUTRAL_VISIBILITY_KM
        text = condition.get("text") or NEUTRAL_TEXT
        icon = str(condition.get("code") or NEUTRAL_ICON)
        obs_time = observation.get("obsTime") or observation.get("updateTime")

        aqi = None
        air = self._request("airquality/v1/current/%s/%s" % (lat_s, lon_s))
        if air is not None:
            indexes = air.get("indexes") or []
            chosen = None
            for index in indexes:
                if isinstance(index, dict) and index.get("code") == "qaqi":
                    chosen = index
                    break
            if chosen is None and indexes:
                chosen = indexes[0]
            if isinstance(chosen, dict):
                aqi = _as_float(chosen.get("aqi"), None)

        return GroundWeather(
            visibility_km=visibility,
            aqi=aqi,
            text=text,
            icon=icon,
            source="qweather",
            obs_time=obs_time,
        )

    def fetch_many(self, spots) -> dict:
        """Return ``{spot_id: GroundWeather}`` for a sequence of spot dicts."""
        result = {}
        for spot in spots:
            result[spot["id"]] = self.fetch(
                spot.get("lat", 0.0), spot.get("lon", 0.0), spot.get("name", "")
            )
        return result

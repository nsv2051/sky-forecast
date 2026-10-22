"""QWeather (和风天气) ground-observation client.

Provides ground-level visibility, air-quality index, present-weather text and
icon for a coordinate. The API key is read from the ``QWEATHER_KEY``
environment variable **only** -- it is never hardcoded.

When no key is configured, or when any request fails, the client silently
returns a neutral default observation (``source="neutral"``) instead of
raising, so the forecast pipeline never breaks in offline / unauthenticated
environments.
"""

from __future__ import annotations

import json
import os
import time as _time
import urllib.parse
import urllib.request
from dataclasses import dataclass

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
        self.base_url = (
            base_url
            or os.environ.get("QWEATHER_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self._transport = transport or self._http_get

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    # -- low level ---------------------------------------------------------
    @staticmethod
    def _http_get(url: str, timeout: float) -> dict:
        request = urllib.request.Request(
            url, headers={"User-Agent": "sky-forecast/1.0 (+offline-capable)"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def _request(self, path: str, params: dict) -> dict | None:
        query = dict(params)
        query["key"] = self.key
        url = "%s/%s?%s" % (self.base_url, path.lstrip("/"), urllib.parse.urlencode(query))
        for attempt in range(self.retries + 1):
            try:
                payload = self._transport(url, self.timeout)
                if isinstance(payload, dict) and str(payload.get("code")) == "200":
                    return payload
                return None
            except Exception:  # noqa: BLE001 - fall back silently
                if attempt < self.retries:
                    _time.sleep(self.backoff * (2.0 ** attempt))
        return None

    # -- public ------------------------------------------------------------
    @staticmethod
    def neutral() -> GroundWeather:
        return GroundWeather()

    def fetch(self, lat: float, lon: float, name: str = "") -> GroundWeather:
        """Return ground weather for ``lat``/``lon`` or a neutral default."""
        if not self.enabled:
            return self.neutral()

        # QWeather expects "lon,lat" ordering.
        location = "%.4f,%.4f" % (float(lon), float(lat))
        now = self._request("weather/now", {"location": location})
        if now is None:
            return self.neutral()

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

    def fetch_many(self, spots) -> dict:
        """Return ``{spot_id: GroundWeather}`` for a sequence of spot dicts."""
        result = {}
        for spot in spots:
            result[spot["id"]] = self.fetch(
                spot.get("lat", 0.0), spot.get("lon", 0.0), spot.get("name", "")
            )
        return result

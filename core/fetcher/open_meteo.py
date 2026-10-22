"""Open-Meteo hourly client (pure Python standard library).

Fetches batched, per-hour layered meteorology for a list of coordinates:

* low / mid / high cloud cover,
* 2 m temperature and relative humidity,
* a pressure-level sounding (temperature, relative humidity and geopotential
  height for ``1000hPa`` through ``700hPa``),
* 10 m wind.

The client builds a ``urllib`` request, applies a configurable timeout and an
exponential-backoff retry, and exposes :func:`mock_hourly` so the whole
pipeline can run deterministically with no network access (CI / offline).

No API key is required for Open-Meteo and none is ever hardcoded here.
"""

from __future__ import annotations

import json
import math
import os
import random
import time as _time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256

__all__ = [
    "OpenMeteoClient",
    "OpenMeteoError",
    "HourlySeries",
    "DEFAULT_VARIABLES",
    "GRID_VARIABLES",
    "BASE_VARIABLES",
    "PRESSURE_LEVELS",
    "PRESSURE_HEIGHTS",
    "pressure_height",
    "mock_elevation",
    "mock_hourly",
]

DEFAULT_BASE_URL = "https://api.open-meteo.com/v1"
DEFAULT_TIMEOUT = 10.0
DEFAULT_BACKOFF = 0.75
# Open-Meteo rate limits reset per minute; a 429 needs a much longer wait than
# the generic exponential backoff, otherwise every retry lands inside the same
# exhausted window and the whole batch degrades to mock.
DEFAULT_RATE_LIMIT_WAIT = 20.0
DEFAULT_RETRIES = 5
# Batched in groups of 40 coordinates: keeps request URLs comfortably short
# while cutting a 36x36 heat-map fetch from 65 requests down to 33.
DEFAULT_CHUNK_SIZE = 40

# Pressure levels exposed as a vertical sounding (surface ~ 700hPa).
PRESSURE_LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700)

# Approximate geopotential heights (metres, MSL) used only when the API does
# not return a height for a level (e.g. synthetic / partial payloads).
PRESSURE_HEIGHTS = {
    1000: 110.0,
    975: 320.0,
    950: 540.0,
    925: 760.0,
    900: 990.0,
    850: 1450.0,
    800: 1950.0,
    700: 3010.0,
}

BASE_VARIABLES = (
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "relative_humidity_2m",
    "temperature_2m",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "visibility",
)


def _pressure_variables():
    names = []
    for level in PRESSURE_LEVELS:
        names.append("temperature_%dhPa" % level)
        names.append("relative_humidity_%dhPa" % level)
        names.append("geopotential_height_%dhPa" % level)
    return tuple(names)


DEFAULT_VARIABLES = BASE_VARIABLES + _pressure_variables()

# Wide-grid subset: cloud cover drives the burning-cloud (BCI) field while the
# pressure-level sounding drives the cloud-sea potential (SCI) field.
GRID_VARIABLES = (
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "relative_humidity_2m",
    "temperature_2m",
    "wind_speed_10m",
    "visibility",
) + _pressure_variables()


class OpenMeteoError(RuntimeError):
    """Raised when the Open-Meteo endpoint cannot be reached or parsed."""


def pressure_height(level: int) -> float:
    """Fallback geopotential height (m) for a pressure level."""
    if level in PRESSURE_HEIGHTS:
        return PRESSURE_HEIGHTS[level]
    # International standard atmosphere approximation.
    return 44330.0 * (1.0 - (level / 1013.25) ** 0.1903)


def mock_elevation(lat: float, lon: float) -> float:
    """Synthetic DEM (m) approximating the Henan Funiu / Songshan terrain.

    Used only by the offline mock so the cloud-sea (SCI) layer has real
    terrain contrast: gently low plains to the east/north-east and the Funiu
    mountains rising toward the south-west.
    """
    ridge = 1450.0 * math.exp(
        -(((lat - 33.6) / 0.95) ** 2 + ((lon - 111.7) / 0.85) ** 2)
    )
    songshan = 720.0 * math.exp(
        -(((lat - 34.48) / 0.34) ** 2 + ((lon - 112.95) / 0.34) ** 2)
    )
    tongbai = 560.0 * math.exp(
        -(((lat - 32.35) / 0.5) ** 2 + ((lon - 113.33) / 0.5) ** 2)
    )
    base = 110.0 + 60.0 * math.sin(lat * 1.3 + lon * 0.7)
    return round(max(20.0, base + ridge + songshan + tongbai), 1)


def _floor_hour(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def _parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coord_int_seed(lat: float, lon: float) -> int:
    digest = sha256(("%.4f,%.4f" % (lat, lon)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass
class HourlySeries:
    """A single coordinate's hourly time series."""

    latitude: float
    longitude: float
    time: list = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    elevation: float | None = None

    def __len__(self) -> int:
        return len(self.time)

    def __bool__(self) -> bool:
        return bool(self.time)

    def value(self, name: str, index: int):
        series = self.variables.get(name)
        if not series or index < 0 or index >= len(series):
            return None
        return series[index]

    def at(self, index: int) -> dict:
        row = {"time": self.time[index]}
        for name, series in self.variables.items():
            row[name] = series[index] if index < len(series) else None
        return row

    def subset(self, indices) -> "HourlySeries":
        indices = list(indices)
        return HourlySeries(
            latitude=self.latitude,
            longitude=self.longitude,
            time=[self.time[i] for i in indices],
            variables={
                name: [series[i] for i in indices]
                for name, series in self.variables.items()
            },
            elevation=self.elevation,
        )

    def to_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "elevation": self.elevation,
            "time": list(self.time),
            "variables": {k: list(v) for k, v in self.variables.items()},
        }


def _parse_hourly(payload: dict, lat: float, lon: float) -> HourlySeries:
    if not isinstance(payload, dict):
        raise OpenMeteoError("unexpected Open-Meteo payload type")
    if payload.get("error"):
        raise OpenMeteoError(str(payload.get("reason", "open-meteo error")))
    hourly = payload.get("hourly") or {}
    times = list(hourly.get("time") or [])
    variables = {
        key: list(values)
        for key, values in hourly.items()
        if key != "time" and isinstance(values, list)
    }
    # Keep the *requested* coordinate, not the grid-snapped one Open-Meteo
    # echoes back: the pipeline keys its lat/lon lattice on the request and
    # would otherwise silently drop every live grid cell.
    return HourlySeries(
        latitude=float(lat),
        longitude=float(lon),
        time=times,
        variables=variables,
        elevation=payload.get("elevation"),
    )


def _slice(series: HourlySeries, start: datetime | None, hours: int) -> HourlySeries:
    if start is None:
        indices = list(range(min(hours, len(series))))
    else:
        start = _floor_hour(start)
        indices = [
            i
            for i, raw in enumerate(series.time)
            if _parse_iso(raw) >= start
        ][:hours]
    return series.subset(indices)


class OpenMeteoClient:
    """Thin, dependency-free Open-Meteo forecast client."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        rate_limit_wait: float = DEFAULT_RATE_LIMIT_WAIT,
        transport=None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("OPEN_METEO_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = float(backoff)
        self.rate_limit_wait = float(rate_limit_wait)
        self.chunk_size = max(1, int(chunk_size))
        self._transport = transport or self._http_get

    # -- low level ---------------------------------------------------------
    @staticmethod
    def _http_get(url: str, timeout: float) -> dict:
        request = urllib.request.Request(
            url, headers={"User-Agent": "sky-forecast/1.0 (+offline-capable)"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        """True when the failure is an HTTP 429 (quota) rather than a transient error."""
        if getattr(exc, "code", None) == 429:
            return True
        return "429" in str(exc)

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """Honour a Retry-After header when the server sends one."""
        headers = getattr(exc, "headers", None)
        if not headers:
            return None
        try:
            raw = headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - header access is best-effort
            return None
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except (TypeError, ValueError):
            return None

    def _request(self, url: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._transport(url, self.timeout)
            except Exception as exc:  # noqa: BLE001 - network layer is best-effort
                last_error = exc
                if attempt >= self.retries:
                    break
                if self._is_rate_limited(exc):
                    wait = self._retry_after(exc)
                    if wait is None:
                        wait = self.rate_limit_wait * (attempt + 1)
                    _time.sleep(min(wait, 65.0))
                else:
                    _time.sleep(self.backoff * (2.0 ** attempt))
        raise OpenMeteoError("open-meteo request failed: %s" % last_error)

    # -- public ------------------------------------------------------------
    def fetch_hourly(
        self,
        coordinates,
        *,
        hours: int = 72,
        variables=None,
        start: datetime | None = None,
        forecast_days: int | None = None,
    ) -> list:
        """Fetch hourly series for every ``(lat, lon)`` pair.

        Coordinates are batched (``chunk_size`` per request) to keep URLs
        short. Returns one :class:`HourlySeries` per coordinate, trimmed to
        ``hours`` entries beginning at ``start`` (when provided).
        """
        coords = [(float(lat), float(lon)) for lat, lon in coordinates]
        if not coords:
            return []
        names = tuple(variables) if variables else DEFAULT_VARIABLES
        if forecast_days is None:
            forecast_days = max(2, int(math.ceil(max(hours, 1) / 24.0)) + 1)

        results = []
        for chunk in _chunks(coords, self.chunk_size):
            params = {
                "latitude": ",".join("%.4f" % lat for lat, _ in chunk),
                "longitude": ",".join("%.4f" % lon for _, lon in chunk),
                "hourly": ",".join(names),
                "forecast_days": str(forecast_days),
                "timezone": "UTC",
            }
            url = "%s/forecast?%s" % (self.base_url, urllib.parse.urlencode(params))
            payload = self._request(url)
            payloads = payload if isinstance(payload, list) else [payload]
            for (lat, lon), item in zip(chunk, payloads):
                results.append(_slice(_parse_hourly(item, lat, lon), start, hours))
        return results

    def mock(
        self,
        coordinates,
        *,
        hours: int = 72,
        variables=None,
        start: datetime | None = None,
        seed: int = 20261017,
    ) -> list:
        """Deterministic offline replacement for :meth:`fetch_hourly`."""
        return mock_hourly(
            coordinates,
            hours=hours,
            variables=variables,
            start=start,
            seed=seed,
        )


def _mock_one(
    lat: float,
    lon: float,
    start: datetime,
    hours: int,
    variables,
    seed: int,
) -> HourlySeries:
    rng = random.Random((seed * 1000003) ^ _coord_int_seed(lat, lon))
    times = [start + timedelta(hours=i) for i in range(hours)]

    # Smooth spatial fields keep the heat-map grid visually coherent.
    phase_cloud = lat * 0.9 + lon * 1.3
    deck_base = _clamp(450.0 + 180.0 * math.sin(lat * 1.1), 250.0, 700.0)
    deck_top = _clamp(
        deck_base + 350.0 + 180.0 * math.sin(lon * 1.3 + 1.0), 600.0, 1250.0
    )
    inv_strength = 5.0 + 4.0 * (0.5 + 0.5 * math.sin(lat * 1.7 + lon * 0.9))

    need_profile = any("hPa" in name for name in variables)

    data = {name: [] for name in variables}
    for i, instant in enumerate(times):
        hod = instant.hour + instant.minute / 60.0
        diurnal = math.sin((hod - 9.0) * math.pi / 12.0)

        temp2m = (
            16.0
            - 0.6 * (lat - 34.5)
            - 0.3 * (lon - 113.0)
            + 6.5 * diurnal
            + rng.uniform(-0.8, 0.8)
        )
        rh2m = _clamp(70.0 - 16.0 * diurnal + rng.uniform(-5.0, 5.0), 20.0, 100.0)
        wind = _clamp(
            2.0 + 3.0 * abs(math.sin(lat + lon + i * 0.05)) + rng.uniform(0.0, 2.0),
            0.0,
            22.0,
        )
        cloud_low = _clamp(
            45.0
            + 35.0 * math.sin(2 * math.pi * i / 11.0 + phase_cloud)
            + 12.0 * math.sin(2 * math.pi * i / 5.0)
            + rng.uniform(-6.0, 6.0),
            0.0,
            100.0,
        )
        cloud_mid = _clamp(
            52.0
            + 30.0 * math.sin(2 * math.pi * i / 13.0 + phase_cloud * 0.7)
            + rng.uniform(-8.0, 8.0),
            0.0,
            100.0,
        )
        cloud_high = _clamp(
            40.0
            + 28.0 * math.sin(2 * math.pi * i / 17.0 + phase_cloud * 1.3)
            + rng.uniform(-8.0, 8.0),
            0.0,
            100.0,
        )

        values = {
            "temperature_2m": temp2m,
            "relative_humidity_2m": rh2m,
            "cloud_cover_low": cloud_low,
            "cloud_cover_mid": cloud_mid,
            "cloud_cover_high": cloud_high,
            "wind_speed_10m": wind,
            "wind_direction_10m": (200.0 + 40.0 * math.sin(lat + i * 0.08)) % 360.0,
            "precipitation": max(0.0, (cloud_low - 70.0) / 10.0) * rng.uniform(0.0, 0.5),
        }

        if need_profile:
            for level in PRESSURE_LEVELS:
                height = pressure_height(level)
                base = temp2m - 6.4 * (height / 1000.0)
                if height > deck_top:
                    ramp = min(1.0, (height - deck_top) / 300.0)
                    decay = max(
                        0.0, 1.0 - max(0.0, height - (deck_top + 700.0)) / 1500.0
                    )
                    base += inv_strength * ramp * decay
                if deck_base <= height <= deck_top:
                    rh = _clamp(93.0 + rng.uniform(-3.0, 3.0), 85.0, 100.0)
                elif height < deck_base:
                    rh = _clamp(rh2m + rng.uniform(-4.0, 4.0), 25.0, 100.0)
                else:
                    rh = _clamp(
                        52.0 - (height - deck_top) / 50.0 + rng.uniform(-4.0, 4.0),
                        6.0,
                        90.0,
                    )
                values["temperature_%dhPa" % level] = base
                values["relative_humidity_%dhPa" % level] = rh
                values["geopotential_height_%dhPa" % level] = (
                    height + 10.0 * math.sin(lat + lon)
                )

        for name in variables:
            raw = values.get(name)
            data[name].append(None if raw is None else round(float(raw), 2))

    return HourlySeries(
        latitude=lat,
        longitude=lon,
        time=[_format_time(t) for t in times],
        variables=data,
        elevation=mock_elevation(lat, lon),
    )


def mock_hourly(
    coordinates,
    *,
    hours: int = 72,
    variables=None,
    start: datetime | None = None,
    seed: int = 20261017,
) -> list:
    """Generate deterministic mock :class:`HourlySeries` for CI / offline use."""
    start = _floor_hour(start or datetime.now(timezone.utc))
    names = tuple(variables) if variables else DEFAULT_VARIABLES
    return [
        _mock_one(float(lat), float(lon), start, int(hours), names, seed)
        for lat, lon in coordinates
    ]

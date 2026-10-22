#!/usr/bin/env python3
"""Vercel Serverless Function entry point for the sky-forecast API.

This module is intentionally dependency-free (Python standard library only) so
that it runs unchanged on the Vercel Python runtime and locally.

Routes
------
``GET /api/forecast``
    Rebuilds the forecast on demand and returns the JSON document.

Query parameters
----------------
``mock=1``
    Force deterministic offline data (never calls the network).
``hours=24``
    Forecast horizon in hours (default 72, capped at 168).
``spots=songshan_shaoshi,beilonghu``
    Restrict the response to a comma-separated subset of spot ids.
``grid=0``
    Skip the burning-cloud heat-map grid (smaller / faster payload).
``step=0.2``
    Heat-map grid resolution in degrees (default 0.1).

Security
--------
The QWeather API key is read exclusively from the ``QWEATHER_KEY`` environment
variable by :class:`core.fetcher.qweather.QWeatherClient`. No credential is
ever hardcoded in this repository. When the key (or the network) is missing the
pipeline safely degrades to neutral / mock ground observations.
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline import (  # noqa: E402
    FORECAST_HOURS,
    build_forecast,
    load_config,
)

MAX_HOURS = 168


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _first(params: dict, name: str, default=None):
    values = params.get(name)
    if not values:
        return default
    return values[0]


def build_response(query: dict) -> dict:
    """Build the forecast payload described by *query* (already parsed)."""
    mock = _truthy(_first(query, "mock", "0"))

    try:
        hours = int(_first(query, "hours", FORECAST_HOURS))
    except (TypeError, ValueError):
        hours = FORECAST_HOURS
    hours = max(1, min(hours, MAX_HOURS))

    include_grid = not (query.get("grid") and not _truthy(_first(query, "grid", "1")))

    grid_step = 0.1
    raw_step = _first(query, "step")
    if raw_step is not None:
        try:
            grid_step = max(0.05, min(float(raw_step), 1.0))
        except ValueError:
            grid_step = 0.1

    config = load_config()
    forecast = build_forecast(
        config,
        mock=mock,
        hours=hours,
        include_grid=include_grid,
        grid_step=grid_step,
    )

    wanted = _first(query, "spots")
    if wanted:
        ids = {part.strip() for part in wanted.split(",") if part.strip()}
        forecast["spots"] = [s for s in forecast.get("spots", []) if s.get("id") in ids]

    forecast["api"] = {
        "mock": mock,
        "hours": hours,
        "include_grid": include_grid,
        "grid_step": grid_step,
    }
    return forecast


class handler(BaseHTTPRequestHandler):
    """Minimal BaseHTTPRequestHandler compatible with the Vercel Python runtime."""

    server_version = "sky-forecast/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=0, s-maxage=300")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server API
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        try:
            payload = build_response(parse_qs(parsed.query))
            self._send_json(200, payload)
        except Exception:  # noqa: BLE001 - surface a clean JSON error
            self._send_json(
                500,
                {
                    "error": "forecast_generation_failed",
                    "detail": traceback.format_exc(limit=4),
                },
            )


if __name__ == "__main__":
    # Local debugging only: python3 api/forecast.py  ->  http://127.0.0.1:8077/
    import os
    from http.server import ThreadingHTTPServer

    port = int(os.environ.get("PORT", "8077"))
    print("serving sky-forecast API on http://127.0.0.1:%d/api/forecast" % port)
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()

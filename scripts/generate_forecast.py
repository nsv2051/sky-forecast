#!/usr/bin/env python3
"""One-shot static forecast generator for sky-forecast.

Usage::

    python3 scripts/generate_forecast.py            # live ECMWF via Open-Meteo
    python3 scripts/generate_forecast.py --mock     # deterministic offline data

By default this pulls **real, hourly ECMWF meteorology** (low/mid/high cloud
cover, 2 m fields and a pressure-level sounding) from the Open-Meteo API for
every configured spot and every heat-map grid cell, so the written document
always carries a real physical gradient. ``--mock`` is only for offline CI and
is explicitly labelled ``source=mock`` in ``meta``.

Writes ``public/data/forecast.json`` (structure: ``updated_at``, ``spots``,
``grid``, ``summary``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pipeline import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_SPOTS_PATH,
    FORECAST_HOURS,
    build_forecast,
    load_config,
    save_forecast,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the static sky-forecast JSON document."
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="force deterministic offline data (no network calls)",
    )
    parser.add_argument(
        "--spots",
        default=str(DEFAULT_SPOTS_PATH),
        help="path to the spots configuration JSON",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="output JSON path",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=FORECAST_HOURS,
        help="forecast horizon in hours (default: %(default)s)",
    )
    parser.add_argument(
        "--no-grid",
        action="store_true",
        help="skip the Zhengzhou burning-cloud heat-map grid",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.spots)
    forecast = build_forecast(
        config,
        mock=args.mock,
        hours=args.hours,
        include_grid=not args.no_grid,
    )
    target = save_forecast(forecast, args.output)

    meta = forecast["meta"]
    print(
        "[generate_forecast] wrote %s (%d spots, %d grid points, source=%s, mock=%s)"
        % (
            target,
            meta["spot_count"],
            forecast["grid"]["count"] if forecast.get("grid") else 0,
            meta["data_source"],
            meta["mock"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

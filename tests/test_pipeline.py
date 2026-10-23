#!/usr/bin/env python3
"""Offline tests for the sky-forecast forecast pipeline.

Run directly (no network, no third-party dependencies)::

    python3 tests/test_pipeline.py

Builds the full forecast from deterministic mock data and validates the
generated JSON contract, plus the graceful-degradation guarantees of the
fetchers and the ``--mock`` CLI entry point.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.fetcher.open_meteo import OpenMeteoClient, mock_hourly  # noqa: E402
from core.fetcher.qweather import GroundWeather, QWeatherClient  # noqa: E402
from core.pipeline import (  # noqa: E402
    GRID_BOUNDS,
    build_forecast,
    grid_coordinates,
    load_config,
    save_forecast,
)

START = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
HOURS = 72


def _load_script_module():
    path = PROJECT_ROOT / "scripts" / "generate_forecast.py"
    spec = importlib.util.spec_from_file_location("generate_forecast", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPipelineOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.forecast = build_forecast(
            cls.config, mock=True, hours=HOURS, start=START
        )

    def test_top_level_structure(self):
        forecast = self.forecast
        for key in ("updated_at", "spots", "grid", "summary"):
            self.assertIn(key, forecast)
        self.assertIsInstance(forecast["spots"], list)
        self.assertIsInstance(forecast["grid"], dict)
        self.assertIsInstance(forecast["summary"], list)
        self.assertTrue(forecast["updated_at"].endswith("Z"))

    def test_json_serialisable(self):
        text = json.dumps(self.forecast, ensure_ascii=False)
        self.assertGreater(len(text), 1000)
        self.assertEqual(json.loads(text)["meta"]["mock"], True)

    def test_all_spots_present(self):
        ids = {spot["id"] for spot in self.config["spots"]}
        out_ids = {spot["id"] for spot in self.forecast["spots"]}
        self.assertEqual(ids, out_ids)

    def test_spot_hourly_contract(self):
        for spot in self.forecast["spots"]:
            self.assertEqual(len(spot["hourly"]), HOURS)
            for entry in spot["hourly"]:
                for key in (
                    "time",
                    "bci",
                    "bci_grade",
                    "sci",
                    "sci_status",
                    "sci_state",
                    "inversion_height_m",
                    "cloud_top_m",
                    "sun_elevation",
                    "sun_azimuth",
                ):
                    self.assertIn(key, entry)
                self.assertTrue(0.0 <= entry["bci"] <= 100.0)
                self.assertTrue(0.0 <= entry["sci"] <= 100.0)
                self.assertIsInstance(entry["bci_grade"], str)
                self.assertIn(
                    entry["sci_state"],
                    ("overlook_sea", "inside_cloud", "clear_sky"),
                )

    def test_spot_status_and_orientation(self):
        for spot in self.forecast["spots"]:
            status = spot["status"]
            for key in (
                "peak_bci",
                "peak_sci",
                "orientation_advice",
                "burning_cloud_applicable",
                "sea_of_clouds_applicable",
            ):
                self.assertIn(key, status)
            self.assertTrue(status["orientation_advice"])
            self.assertEqual(spot["best_sunset"], spot["best_evening"])
            self.assertEqual(spot["best_sunrise"], spot["best_morning"])

    def test_sci_observable_flag(self):
        found_true = False
        for spot in self.forecast["spots"]:
            for entry in spot["hourly"]:
                self.assertIn("sci_observable", entry)
                self.assertIsInstance(entry["sci_observable"], bool)
                found_true = found_true or entry["sci_observable"]
            self.assertIn("peak_sci_any_time", spot["status"])
        self.assertTrue(found_true)

    def test_best_windows_are_sane(self):
        for spot in self.forecast["spots"]:
            best = spot["best_evening"]
            if best is not None:
                self.assertTrue(-5.0 <= best["sun_elevation"] <= 8.0)
                self.assertTrue(best["sun_azimuth"] >= 0.0)

    def test_grid_contract(self):
        grid = self.forecast["grid"]
        self.assertEqual(grid["metric"], "bci")
        self.assertEqual(grid["metrics"], ["bci", "sci"])
        snapshots = grid["snapshots"]
        self.assertIsInstance(snapshots, list)
        self.assertGreaterEqual(len(snapshots), 1)
        default_key = grid["default_snapshot"]
        self.assertIsNotNone(default_key)
        snap = next(item for item in snapshots if item["key"] == default_key)
        self.assertGreater(snap["count"], 0)
        self.assertEqual(len(snap["points"]), snap["count"])
        for point in snap["points"]:
            for key in ("lat", "lon", "bci", "sci", "level", "sci_level"):
                self.assertIn(key, point)
            self.assertTrue(GRID_BOUNDS["lat_min"] <= point["lat"] <= GRID_BOUNDS["lat_max"])
            self.assertTrue(GRID_BOUNDS["lon_min"] <= point["lon"] <= GRID_BOUNDS["lon_max"])
            self.assertTrue(0.0 <= point["bci"] <= 100.0)
            self.assertTrue(0.0 <= point["sci"] <= 100.0)
            self.assertIn(point["level"], (0, 1, 2, 3, 4))
            self.assertIn(point["sci_level"], (0, 1, 2, 3, 4))

    def test_grid_is_full_and_deterministic(self):
        expected = len(list(grid_coordinates()))
        self.assertEqual(expected, 36 * 36)
        grid = self.forecast["grid"]
        self.assertGreaterEqual(len(grid["snapshots"]), 1)
        for snap in grid["snapshots"]:
            self.assertEqual(len(snap["points"]), expected)
            self.assertEqual(snap["count"], expected)

    def test_summary_contract(self):
        summary = self.forecast["summary"]
        self.assertGreaterEqual(len(summary), 1)
        for day in summary:
            self.assertIn("date", day)
            self.assertIn("weekday", day)
            self.assertIn("evening", day)
            self.assertIn("morning", day)
            if day["evening"] is not None:
                self.assertIn("highlight", day["evening"])
                self.assertIn("spot_name", day["evening"])

    def test_save_forecast_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "forecast.json"
            written = save_forecast(self.forecast, target)
            self.assertTrue(written.exists())
            reloaded = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["meta"]["spot_count"], len(self.config["spots"]))


class TestFetcherFallbacks(unittest.TestCase):
    def test_open_meteo_mock_is_deterministic(self):
        coords = [(34.5, 112.9), (34.8, 113.7)]
        first = mock_hourly(coords, hours=6, start=START)
        second = mock_hourly(coords, hours=6, start=START)
        self.assertEqual(len(first), 2)
        for a, b in zip(first, second):
            self.assertEqual(a.time, b.time)
            self.assertEqual(a.variables, b.variables)
            self.assertIn("cloud_cover_low", a.variables)
            self.assertIn("temperature_850hPa", a.variables)

    def test_open_meteo_transport_failure_raises(self):
        def boom(url, timeout):
            raise RuntimeError("network down")

        client = OpenMeteoClient(transport=boom, retries=0)
        with self.assertRaises(Exception):
            client.fetch_hourly([(34.5, 112.9)], hours=3, start=START)

    def test_qweather_without_key_is_neutral(self):
        client = QWeatherClient(key="")
        self.assertFalse(client.enabled)
        weather = client.fetch(34.5, 112.9)
        self.assertIsInstance(weather, GroundWeather)
        self.assertEqual(weather.source, "neutral")
        self.assertGreater(weather.visibility_km, 0.0)

    def test_qweather_failure_is_silent(self):
        def boom(url, timeout):
            raise RuntimeError("api down")

        client = QWeatherClient(key="dummy", transport=boom, retries=0)
        weather = client.fetch(34.5, 112.9)
        self.assertEqual(weather.source, "neutral")


class TestGenerateScript(unittest.TestCase):
    def test_mock_cli_writes_valid_json(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "forecast.json"
            code = module.main(
                ["--mock", "--output", str(target), "--hours", "24", "--no-grid"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(target.exists())
            data = json.loads(target.read_text(encoding="utf-8"))
            for key in ("updated_at", "spots", "grid", "summary"):
                self.assertIn(key, data)
            self.assertTrue(data["meta"]["mock"])


class TestFetchSeriesFailure(unittest.TestCase):
    """真实抓取失败必须抛错，不能静默降级为 mock 数据。"""

    class _BoomClient:
        def fetch_hourly(self, *args, **kwargs):
            raise RuntimeError("simulated outage")

        def mock(self, *args, **kwargs):
            return ["should-not-be-used"]

    class _EmptyClient:
        def fetch_hourly(self, *args, **kwargs):
            return []

        def mock(self, *args, **kwargs):
            return ["should-not-be-used"]

    class _MockClient:
        def fetch_hourly(self, *args, **kwargs):
            raise RuntimeError("live fetch must not run with --mock")

        def mock(self, coordinates, *, hours, start, variables):
            return mock_hourly(coordinates, hours=hours, start=start, variables=variables)

    def test_live_failure_raises_open_meteo_error(self):
        from core.pipeline import _fetch_series
        from core.fetcher.open_meteo import OpenMeteoError

        with self.assertRaises(OpenMeteoError):
            _fetch_series(
                self._BoomClient(),
                [(34.0, 113.0)],
                hours=4,
                start=None,
                variables=None,
                force_mock=False,
            )

    def test_empty_response_raises_open_meteo_error(self):
        from core.pipeline import _fetch_series
        from core.fetcher.open_meteo import OpenMeteoError

        with self.assertRaises(OpenMeteoError):
            _fetch_series(
                self._EmptyClient(),
                [(34.0, 113.0)],
                hours=4,
                start=None,
                variables=None,
                force_mock=False,
            )

    def test_force_mock_still_returns_mock_series(self):
        from core.pipeline import _fetch_series

        series, source = _fetch_series(
            self._MockClient(),
            [(34.0, 113.0)],
            hours=4,
            start=START,
            variables=None,
            force_mock=True,
        )
        self.assertEqual(source, "mock")
        self.assertTrue(series)
        self.assertGreater(len(series[0]), 0)

    def test_error_message_includes_original_failure(self):
        from core.pipeline import _fetch_series
        from core.fetcher.open_meteo import OpenMeteoError

        with self.assertRaises(OpenMeteoError) as ctx:
            _fetch_series(
                self._BoomClient(),
                [(34.0, 113.0)],
                hours=4,
                start=None,
                variables=None,
                force_mock=False,
            )
        message = str(ctx.exception)
        self.assertIn("simulated outage", message)
        self.assertIn("1 coordinates", message)


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(
        f"\n[test_pipeline] run={result.testsRun} "
        f"failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)}"
    )
    if result.wasSuccessful():
        print("[test_pipeline] ALL TESTS PASSED")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Offline tests for the CMA (China Meteorological Administration) fetcher.

Run directly (no network, no third-party dependencies)::

    python3 tests/test_cma.py

Covers the two traps that broke the first implementation:

1. ``data`` is grouped by date (``[{date, list}, ...]``) and must be flattened
   before any per-hour lookup can succeed.
2. ``hour`` is NOT a clock hour. It is an index counted from 08:00 on the
   publish day (``forecastTime`` ``17:00`` -> ``hour`` ``9``). Any lookup that
   keys on ``hour`` silently shifts every value by eight hours.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fetcher import cma  # noqa: E402


GENERIC_REPO_URL = cma.BASE_URL.format(station_id="57083")


def _payload() -> dict:
    """A trimmed but structurally faithful CMA response.

    Two date groups on purpose: a flattening bug must be visible here.
    ``hour`` values are deliberately inconsistent with the clock so that any
    implementation keying on ``hour`` fails.
    """
    return {
        "msg": "success",
        "code": 0,
        "data": [
            {
                "date": "2026/11/10",
                "list": [
                    {
                        "stationid": "57083",
                        "publishTime": "2026/11/10 12:00",
                        "forecastTime": "2026/11/10 17:00",
                        "hour": 9,
                        "temperature": 17.7,
                        "humidity": 24.2,
                        "totalCloudCover": 0.0,
                        "lowCloudCover": 0.0,
                        "precipitation": 0.0,
                        "visibility": 16.4,
                        "text": "晴",
                    },
                    {
                        "stationid": "57083",
                        "publishTime": "2026/11/10 12:00",
                        "forecastTime": "2026/11/10 20:00",
                        "hour": 12,
                        "temperature": 9.6,
                        "humidity": 68.2,
                        "totalCloudCover": 40.0,
                        "lowCloudCover": 35.0,
                        "precipitation": 1.2,
                        "visibility": 12.9,
                        "text": "多云",
                    },
                ],
            },
            {
                "date": "2026/11/11",
                "list": [
                    {
                        "stationid": "57083",
                        "publishTime": "2026/11/10 12:00",
                        "forecastTime": "2026/11/11 02:00",
                        "hour": 18,
                        "temperature": 4.7,
                        "humidity": 83.8,
                        "totalCloudCover": 90.0,
                        "lowCloudCover": 85.0,
                        "precipitation": 0.0,
                        "visibility": 10.7,
                        "text": "阴",
                    }
                ],
            },
        ],
    }


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reset_cache() -> None:
    with cma._CACHE_LOCK:
        cma._CACHE.clear()


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_cache()

    def tearDown(self) -> None:
        _reset_cache()

    def test_flattens_date_groups(self) -> None:
        """All rows from every date group must survive."""
        body = json.dumps(_payload()).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            rows = cma.fetch_hourly("57083")
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 3)
        times = [row["forecastTime"] for row in rows]
        self.assertIn("2026/11/10 17:00", times)
        self.assertIn("2026/11/11 02:00", times)

    def test_network_failure_returns_none(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(cma.fetch_hourly("57083"))

    def test_bad_json_returns_none(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(b"<html>not json</html>"),
        ):
            self.assertIsNone(cma.fetch_hourly("57083"))

    def test_unknown_station_returns_none(self) -> None:
        body = json.dumps({"msg": "error", "code": 1, "data": []}).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            self.assertIsNone(cma.fetch_hourly("999999"))

    def test_cache_requests_once(self) -> None:
        body = json.dumps(_payload()).encode("utf-8")
        with patch(
            "urllib.request.urlopen", return_value=_FakeResponse(body)
        ) as mocked:
            first = cma.fetch_hourly("57083")
            second = cma.fetch_hourly("57083")
        self.assertIs(first, second)
        self.assertEqual(mocked.call_count, 1)


class LookupTests(unittest.TestCase):
    """``hour`` is an index from 08:00, never a clock hour."""

    @classmethod
    def setUpClass(cls) -> None:
        _reset_cache()
        body = json.dumps(_payload()).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            cls.rows = cma.fetch_hourly("57083")

    @classmethod
    def tearDownClass(cls) -> None:
        _reset_cache()

    def test_exact_lookup_uses_forecast_time_not_hour_field(self) -> None:
        row = cma.hourly_at(self.rows, "2026-11-10", 17)
        self.assertIsNotNone(row)
        self.assertEqual(row["forecastTime"], "2026/11/10 17:00")
        # The trap: hour == 9 here, so keying on it would return nothing.
        self.assertEqual(row["hour"], 9)

    def test_exact_lookup_rejects_wrong_hour(self) -> None:
        # 9 is the ``hour`` index of the 17:00 row; it must NOT be a match.
        self.assertIsNone(cma.hourly_at(self.rows, "2026-11-10", 9))

    def test_exact_lookup_across_date_groups(self) -> None:
        row = cma.hourly_at(self.rows, "2026-11-11", 2)
        self.assertIsNotNone(row)
        self.assertEqual(row["forecastTime"], "2026/11/11 02:00")

    def test_low_cloud_and_visibility(self) -> None:
        self.assertEqual(cma.low_cloud_at(self.rows, "2026-11-10", 20), 35.0)
        self.assertEqual(cma.visibility_at(self.rows, "2026-11-10", 20), 12.9)

    def test_nearest_picks_exact_first(self) -> None:
        value, offset = cma.nearest_low_cloud(self.rows, "2026-11-10", 17)
        self.assertEqual(value, 0.0)
        self.assertEqual(offset, 0.0)

    def test_nearest_falls_back_within_window(self) -> None:
        # 18:00 is one hour from the 17:00 row.
        value, offset = cma.nearest_low_cloud(self.rows, "2026-11-10", 18)
        self.assertEqual(value, 0.0)
        self.assertEqual(offset, -1.0)

    def test_nearest_respects_max_offset(self) -> None:
        # 11:00 is six hours from the nearest row (17:00) -> out of window.
        value, offset = cma.nearest_low_cloud(self.rows, "2026-11-10", 11)
        self.assertIsNone(value)
        self.assertIsNone(offset)

    def test_nearest_precipitation(self) -> None:
        value, offset = cma.nearest_precipitation(self.rows, "2026-11-10", 20)
        self.assertEqual(value, 1.2)
        self.assertEqual(offset, 0.0)

    def test_empty_rows_are_safe(self) -> None:
        self.assertIsNone(cma.hourly_at(None, "2026-11-10", 17))
        self.assertEqual(cma.nearest_at([], "2026-11-10", 17), (None, None))
        self.assertEqual(
            cma.nearest_low_cloud(None, "2026-11-10", 17), (None, None)
        )


class StationMapTests(unittest.TestCase):
    def test_known_cities(self) -> None:
        self.assertEqual(cma.station_for_spot({"city": "郑州"}), "57083")
        self.assertEqual(cma.station_for_spot({"city": "洛阳"}), "57074")
        self.assertEqual(cma.station_for_spot({"city": "栾川"}), "57077")

    def test_city_suffix_is_normalized(self) -> None:
        self.assertEqual(cma.station_for_spot({"city": "郑州市"}), "57083")

    def test_unknown_city_returns_none(self) -> None:
        self.assertIsNone(cma.station_for_spot({"city": "火星"}))
        self.assertIsNone(cma.station_for_spot({}))

    def test_every_configured_spot_resolves(self) -> None:
        """Every sea-of-clouds spot in the repo must map to a station."""
        config_path = Path(__file__).resolve().parents[1] / "config" / "spots.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        missing = [
            spot["name"]
            for spot in config["spots"]
            if cma.station_for_spot(spot) is None
        ]
        self.assertEqual(missing, [], f"未映射到 CMA 站号: {missing}")


if __name__ == "__main__":
    unittest.main()

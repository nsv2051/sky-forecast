#!/usr/bin/env python3
"""Tests for the multi-model consensus layer (offline, standard library only)."""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.fetcher.open_meteo import (  # noqa: E402
    CONSENSUS_MODELS,
    CONSENSUS_VARIABLES,
    HourlySeries,
    OpenMeteoClient,
    mock_hourly,
    mock_models,
)
from core.fetcher.qweather import GroundWeather  # noqa: E402
from core.pipeline import (  # noqa: E402
    _consensus,
    _hourly_entry,
    build_forecast,
    load_config,
)

START = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


class TestFetchModels(unittest.TestCase):
    def _payload(self):
        return {
            "hourly": {
                "time": ["2026-10-17T00:00", "2026-10-17T01:00"],
                "cloud_cover_low_ecmwf_ifs025": [10, 20],
                "cloud_cover_low_gfs_seamless": [30, 40],
                "relative_humidity_2m_ecmwf_ifs025": [50, 60],
            }
        }

    def test_models_are_joined_and_suffix_is_stripped(self):
        calls = []

        def transport(url, timeout):
            calls.append(url)
            return self._payload()

        client = OpenMeteoClient(transport=transport, retries=0)
        result = client.fetch_models(
            [(34.5, 113.0)],
            models=("ecmwf_ifs025", "gfs_seamless"),
            variables=("cloud_cover_low", "relative_humidity_2m"),
            hours=2,
            start=None,
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
        self.assertEqual(query["models"], ["ecmwf_ifs025,gfs_seamless"])
        self.assertEqual(
            query["hourly"], ["cloud_cover_low,relative_humidity_2m"]
        )
        models = result[0]["models"]
        self.assertEqual(
            models["ecmwf_ifs025"]["cloud_cover_low"], [10, 20]
        )
        # Variables containing underscores survive the endswith-based strip.
        self.assertEqual(
            models["ecmwf_ifs025"]["relative_humidity_2m"], [50, 60]
        )
        # A model that does not return a variable is marked missing, not zero.
        self.assertEqual(
            models["gfs_seamless"]["relative_humidity_2m"], [None, None]
        )
        self.assertEqual(
            models["gfs_seamless"]["cloud_cover_low"], [30, 40]
        )

    def test_start_and_hours_trim_every_model(self):
        def transport(url, timeout):
            return self._payload()

        client = OpenMeteoClient(transport=transport, retries=0)
        result = client.fetch_models(
            [(34.5, 113.0)],
            models=("ecmwf_ifs025",),
            variables=("cloud_cover_low",),
            hours=1,
            start=datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result[0]["time"], ["2026-10-17T01:00"])
        self.assertEqual(
            result[0]["models"]["ecmwf_ifs025"]["cloud_cover_low"], [20]
        )


class TestMockModels(unittest.TestCase):
    def test_multi_model_mock_is_deterministic_and_distinct(self):
        coords = [(34.5, 112.9)]
        first = mock_hourly(
            coords,
            hours=3,
            start=START,
            models=CONSENSUS_MODELS,
            variables=CONSENSUS_VARIABLES,
        )
        second = mock_hourly(
            coords,
            hours=3,
            start=START,
            models=CONSENSUS_MODELS,
            variables=CONSENSUS_VARIABLES,
        )
        self.assertEqual(first, second)
        models = first[0]["models"]
        for model in CONSENSUS_MODELS:
            self.assertIn(model, models)
        low_values = {
            tuple(models[model]["cloud_cover_low"]) for model in CONSENSUS_MODELS
        }
        self.assertGreater(len(low_values), 1)

    def test_legacy_mock_shape_is_untouched(self):
        series = mock_hourly([(34.5, 112.9)], hours=2, start=START)
        self.assertIsInstance(series[0], HourlySeries)
        self.assertIn("cloud_cover_low", series[0].variables)
        self.assertNotIn(
            "cloud_cover_low_ecmwf_ifs025", series[0].variables
        )

    def test_mock_models_function(self):
        kwargs = dict(
            models=("a", "b"),
            variables=("cloud_cover_mid",),
            hours=2,
            start=START,
        )
        first = mock_models([(34.5, 112.9)], **kwargs)
        second = mock_models([(34.5, 112.9)], **kwargs)
        self.assertEqual(first, second)
        self.assertNotEqual(
            first[0]["models"]["a"]["cloud_cover_mid"],
            first[0]["models"]["b"]["cloud_cover_mid"],
        )


class TestConsensusMath(unittest.TestCase):
    def test_four_equal_sources(self):
        data = {source: [50.0] for source in
                ("best_match",) + CONSENSUS_MODELS}
        result = _consensus(data, 0)
        self.assertEqual(result["available"], 4)
        self.assertEqual(result["spread"], 0)
        self.assertEqual(result["agreement"], 100)
        self.assertEqual(result["level"], "高")
        self.assertEqual(result["cloud_prob"], 100)

    def test_zero_twenty_forty_sixty(self):
        data = {
            "best_match": 0,
            "ecmwf_ifs025": 20,
            "gfs_seamless": 40,
            "icon_seamless": 60,
        }
        result = _consensus(data, 0)
        self.assertEqual(result["spread"], 60)
        self.assertEqual(result["agreement"], 0)
        self.assertEqual(result["level"], "低")
        self.assertEqual(result["cloud_prob"], 50)
        self.assertEqual(result["mean"], 30.0)
        self.assertEqual(result["min"], 0.0)
        self.assertEqual(result["max"], 60.0)

    def test_single_source_never_fakes_agreement(self):
        data = {"best_match": 40, "ecmwf_ifs025": None}
        result = _consensus(data, 0)
        self.assertEqual(result["available"], 1)
        self.assertIsNone(result["agreement"])
        self.assertIsNone(result["level"])
        self.assertTrue(result["note"])

    def test_no_sources_returns_none_probability(self):
        result = _consensus({}, 0)
        self.assertEqual(result["available"], 0)
        self.assertIsNone(result["cloud_prob"])
        self.assertIsNone(result["agreement"])


class TestPipelineConsensus(unittest.TestCase):
    def test_mock_forecast_exposes_consensus(self):
        forecast = build_forecast(
            load_config(), mock=True, hours=3, include_grid=False, start=START
        )
        self.assertEqual(
            forecast["meta"]["consensus_models"], list(CONSENSUS_MODELS)
        )
        entry = forecast["spots"][0]["hourly"][0]
        self.assertIn("consensus", entry)
        self.assertIn("consensus_score", entry)
        self.assertGreaterEqual(entry["consensus"]["low"]["available"], 2)
        self.assertIsNotNone(entry["consensus"]["low"]["agreement"])

    def test_old_caller_without_models_data_gets_empty_consensus(self):
        series = HourlySeries(
            latitude=34.5,
            longitude=113.0,
            time=["2026-10-17T06:00:00Z"],
            variables={
                "cloud_cover_low": [40.0],
                "cloud_cover_mid": [40.0],
                "cloud_cover_high": [40.0],
                "temperature_2m": [20.0],
                "relative_humidity_2m": [60.0],
            },
        )
        spot = {
            "id": "x",
            "name": "x",
            "lat": 34.5,
            "lon": 113.0,
            "elevation_m": 100.0,
        }
        entry = _hourly_entry(spot, series, GroundWeather(), 0)
        self.assertEqual(entry["consensus"]["low"]["available"], 0)
        self.assertIsNone(entry["consensus_score"])


def main():
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

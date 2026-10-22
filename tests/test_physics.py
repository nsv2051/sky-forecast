#!/usr/bin/env python3
"""Smoke / unit tests for the sky-forecast physics engine.

Run directly (no network, no third-party dependencies)::

    python3 tests/test_physics.py

Covers solar geometry, the Burning Cloud Index and the Sea of Clouds Index,
and enforces the "no hardcoded secret" safety rule on the repository.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.physics import burning_cloud, sea_of_clouds, solar  # noqa: E402

ZHENGZHOU = (34.7466, 113.6254)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _sample_profile():
    """A moist low/mid deck capped by a strong inversion (sea-of-clouds setup)."""
    return [
        {"height": 0, "temperature": 22.0, "dewpoint": 18.0},
        {"height": 300, "temperature": 19.0, "dewpoint": 17.0},
        {"height": 600, "temperature": 16.0, "dewpoint": 15.0},
        {"height": 900, "temperature": 14.0, "dewpoint": 14.0},
        {"height": 1200, "temperature": 12.0, "dewpoint": 12.0},
        {"height": 1500, "temperature": 16.0, "dewpoint": 8.0},
        {"height": 2000, "temperature": 11.0, "dewpoint": 2.0},
        {"height": 3000, "temperature": 5.0, "dewpoint": -10.0},
    ]


def _dry_profile():
    return [
        {"height": 0, "temperature": 26.0, "dewpoint": 6.0},
        {"height": 500, "temperature": 22.0, "dewpoint": 4.0},
        {"height": 1000, "temperature": 18.0, "dewpoint": 2.0},
        {"height": 2000, "temperature": 10.0, "dewpoint": -6.0},
    ]


class TestSolarGeometry(unittest.TestCase):
    def test_position_bounds(self):
        for hour in range(0, 24, 3):
            pos = solar.solar_position(*ZHENGZHOU, _utc(2026, 6, 21, hour))
            self.assertGreaterEqual(pos["elevation"], -90.0)
            self.assertLessEqual(pos["elevation"], 90.0)
            self.assertGreaterEqual(pos["azimuth"], 0.0)
            self.assertLess(pos["azimuth"], 360.0)

    def test_summer_is_higher_than_winter(self):
        summer = solar.solar_position(*ZHENGZHOU, _utc(2026, 6, 21, 4))["elevation"]
        equinox = solar.solar_position(*ZHENGZHOU, _utc(2026, 3, 20, 4))["elevation"]
        winter = solar.solar_position(*ZHENGZHOU, _utc(2026, 12, 21, 4))["elevation"]
        self.assertGreater(summer, equinox)
        self.assertGreater(equinox, winter)

    def test_noon_azimuth_points_south(self):
        noon = solar.solar_noon(*ZHENGZHOU, _utc(2026, 6, 21))
        pos = solar.solar_position(*ZHENGZHOU, noon)
        self.assertAlmostEqual(pos["azimuth"], 180.0, delta=8.0)
        self.assertGreater(pos["elevation"], 70.0)

    def test_equinox_noon_elevation(self):
        noon = solar.solar_noon(*ZHENGZHOU, _utc(2026, 3, 20))
        pos = solar.solar_position(*ZHENGZHOU, noon)
        expected = 90.0 - abs(ZHENGZHOU[0] - pos["declination"])
        self.assertAlmostEqual(pos["elevation"], expected, delta=1.5)

    def test_sunrise_before_sunset(self):
        rst = solar.sunrise_sunset(*ZHENGZHOU, _utc(2026, 5, 1))
        self.assertFalse(rst["polar_day"])
        self.assertFalse(rst["polar_night"])
        self.assertLess(rst["sunrise"], rst["sunset"])
        at_rise = solar.solar_position(*ZHENGZHOU, rst["sunrise"])["elevation"]
        self.assertAlmostEqual(at_rise, -0.833, delta=1.0)

    def test_polar_night(self):
        rst = solar.sunrise_sunset(89.0, 0.0, _utc(2026, 12, 21))
        self.assertTrue(rst["polar_night"])
        self.assertIsNone(rst["sunrise"])

    def test_golden_hour_windows(self):
        result = solar.golden_hour_windows(*ZHENGZHOU, _utc(2026, 3, 20))
        morning = result["morning"]
        evening = result["evening"]
        self.assertIsNotNone(morning)
        self.assertIsNotNone(evening)
        self.assertLess(morning[0], morning[1])
        self.assertLess(evening[0], evening[1])
        self.assertLess(morning[0], evening[0])
        for window in (morning, evening):
            mid = window[0] + (window[1] - window[0]) / 2
            elev = solar.solar_position(*ZHENGZHOU, mid)["elevation"]
            self.assertGreaterEqual(elev, -4.0)
            self.assertLessEqual(elev, 6.0)


class TestBurningCloud(unittest.TestCase):
    def test_ideal_setup_scores_high(self):
        result = burning_cloud.compute_bci(
            cloud_low=10,
            cloud_mid=50,
            cloud_high=40,
            visibility_km=25,
            aqi=40,
            sun_elevation=-1,
            sun_azimuth=280,
        )
        self.assertGreaterEqual(result["bci"], 60.0)
        self.assertEqual(result["grade"], burning_cloud.grade_bci(result["bci"]))

    def test_overcast_low_deck_scores_low(self):
        result = burning_cloud.compute_bci(
            cloud_low=95,
            cloud_mid=95,
            cloud_high=95,
            visibility_km=5,
            aqi=180,
            sun_elevation=40,
        )
        self.assertLessEqual(result["bci"], 20.0)

    def test_bounds_on_extremes(self):
        for kwargs in (
            {},
            {"cloud_low": 0, "cloud_mid": 0, "cloud_high": 0, "sun_elevation": -30},
            {"cloud_low": 100, "cloud_mid": 100, "cloud_high": 100, "sun_elevation": 90},
        ):
            args = {
                "cloud_low": 50,
                "cloud_mid": 50,
                "cloud_high": 50,
                "visibility_km": 15,
                "aqi": 60,
                "sun_elevation": 0,
            }
            args.update(kwargs)
            bci = burning_cloud.compute_bci(**args)["bci"]
            self.assertGreaterEqual(bci, 0.0)
            self.assertLessEqual(bci, 100.0)

    def test_visibility_is_monotonic(self):
        low_vis = burning_cloud.compute_bci(20, 50, 40, visibility_km=3)["bci"]
        high_vis = burning_cloud.compute_bci(20, 50, 40, visibility_km=30)["bci"]
        self.assertGreater(high_vis, low_vis)

    def test_remote_low_cloud_penalty(self):
        clear = burning_cloud.compute_bci(
            10, 50, 40, visibility_km=25, horizon_low_cloud=0
        )["bci"]
        blocked = burning_cloud.compute_bci(
            10, 50, 40, visibility_km=25, horizon_low_cloud=100
        )["bci"]
        self.assertGreater(clear, blocked)

    def test_aqi_penalty(self):
        clean = burning_cloud.compute_bci(20, 50, 40, visibility_km=25, aqi=30)["bci"]
        hazy = burning_cloud.compute_bci(20, 50, 40, visibility_km=25, aqi=200)["bci"]
        self.assertGreater(clean, hazy)


class TestSeaOfClouds(unittest.TestCase):
    def test_overlook_sea_of_clouds(self):
        result = sea_of_clouds.compute_sci(_sample_profile(), viewpoint_elevation=1400)
        self.assertEqual(result["status"], sea_of_clouds.STATUS_OVERLOOK)
        self.assertGreaterEqual(result["sci"], 60.0)
        self.assertEqual(result["cloud_top"], 1200)
        self.assertEqual(result["inversion_height"], 1200)
        self.assertEqual(result["inversion_strength"], 4.0)

    def test_inside_cloud(self):
        result = sea_of_clouds.compute_sci(_sample_profile(), viewpoint_elevation=800)
        self.assertEqual(result["status"], sea_of_clouds.STATUS_IN_CLOUD)
        self.assertLessEqual(result["sci"], 35.0)

    def test_below_deck(self):
        result = sea_of_clouds.compute_sci(_sample_profile(), viewpoint_elevation=400)
        self.assertEqual(result["status"], sea_of_clouds.STATUS_BELOW)

    def test_clear_sky(self):
        result = sea_of_clouds.compute_sci(_dry_profile(), viewpoint_elevation=1400)
        self.assertEqual(result["status"], sea_of_clouds.STATUS_CLEAR)
        self.assertLessEqual(result["sci"], 10.0)
        self.assertIsNone(result["inversion_height"])

    def test_bounds(self):
        for elevation in (0, 400, 800, 1200, 1400, 2500):
            sci = sea_of_clouds.compute_sci(_sample_profile(), elevation)["sci"]
            self.assertGreaterEqual(sci, 0.0)
            self.assertLessEqual(sci, 100.0)

    def test_humidity_roundtrip(self):
        dewpoint = sea_of_clouds.saturation_dewpoint(20.0, 60.0)
        rh = sea_of_clouds.relative_humidity(20.0, dewpoint)
        self.assertAlmostEqual(rh, 60.0, delta=0.5)

    def test_rh_only_profile_is_accepted(self):
        profile = [
            {"height": 0, "temperature": 20.0, "relative_humidity": 95.0},
            {"height": 500, "temperature": 18.0, "relative_humidity": 96.0},
            {"height": 1000, "temperature": 12.0, "relative_humidity": 40.0},
        ]
        result = sea_of_clouds.compute_sci(profile, viewpoint_elevation=1000)
        self.assertEqual(result["status"], sea_of_clouds.STATUS_OVERLOOK)


class TestConfigAndSecrets(unittest.TestCase):
    def test_spots_json(self):
        path = PROJECT_ROOT / "config" / "spots.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        spots = data["spots"]
        self.assertEqual(len(spots), 11)
        ids = [spot["id"] for spot in spots]
        self.assertEqual(len(ids), len(set(ids)))
        for spot in spots:
            for field in ("id", "name", "lat", "lon", "elevation_m", "preferences"):
                self.assertIn(field, spot)
            self.assertTrue(-90.0 <= spot["lat"] <= 90.0)
            self.assertTrue(-180.0 <= spot["lon"] <= 180.0)
            self.assertGreater(spot["elevation_m"], 0)

    def test_env_template_has_no_real_key(self):
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for line in env_example.splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if "KEY" in name.upper() or "SECRET" in name.upper():
                self.assertEqual(value.strip(), "", f"{name} must stay empty in template")

    def test_no_hardcoded_secrets_in_sources(self):
        patterns = [
            re.compile(r"(?i)qweather_key\s*=\s*[\"'][^\"']+[\"']"),
            re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
            re.compile(r"(?i)api[_-]?key\s*=\s*[\"'][0-9A-Za-z\-_]{16,}[\"']"),
        ]
        roots = [PROJECT_ROOT / "core", PROJECT_ROOT / "tests", PROJECT_ROOT / "config"]
        for root in roots:
            for path in root.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for pattern in patterns:
                    self.assertIsNone(
                        pattern.search(text), f"suspicious secret in {path}"
                    )


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(
        f"\n[test_physics] run={result.testsRun} "
        f"failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)}"
    )
    if result.wasSuccessful():
        print("[test_physics] ALL TESTS PASSED")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

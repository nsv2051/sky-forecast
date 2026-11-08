#!/usr/bin/env python3
"""Unit tests for the expected glow-colour spectrum model.

Run directly (no network, no third-party dependencies)::

    python3 tests/test_spectrum.py

Covers the physical hard constraints:

* lower sun -> redder spectrum;
* larger AOD -> dimmer glow;
* no cloud canvas -> neutral cool grey/blue, not a saturated warm colour;
* sun at or below -12 deg -> brightness collapses to zero;
* optical depth starts at the cloud height (Chapman slant air mass);
* each deck goes dark once it enters the Earth's shadow;
* a sunset still has a visible warm high-cloud glow.
"""

from __future__ import annotations

import colorsys
import math
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.physics import spectrum  # noqa: E402


def _saturation(rgb):
    return colorsys.rgb_to_hls(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)[2]


def _red_share(rgb):
    total = float(rgb[0] + rgb[1] + rgb[2])
    return 0.0 if total <= 0 else rgb[0] / total


class TestTransmittanceSpectrum(unittest.TestCase):
    def test_wavelength_grid(self):
        self.assertEqual(len(spectrum.WAVELENGTHS), 31)
        self.assertEqual(spectrum.WAVELENGTHS[0], 400)
        self.assertEqual(spectrum.WAVELENGTHS[-1], 700)
        values = spectrum.transmittance(0.0)
        self.assertEqual(len(values), len(spectrum.WAVELENGTHS))
        for value in values:
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_lower_sun_is_redder(self):
        """Hard constraint 4a: lower sun -> redder transmitted light.

        越低越红在光谱上的物理含义是红/蓝透射比随太阳降低而升高（等价于
        蓝/红比单调不增）。取 650 nm 为红、450 nm 为蓝，从 +6 deg 降到
        0 deg 逐档检查。
        """
        red_index = spectrum.WAVELENGTHS.index(650)
        blue_index = spectrum.WAVELENGTHS.index(450)
        ratios = []
        for elevation in (6, 5, 4, 3, 2, 1, 0):
            values = spectrum.transmittance(elevation, 0.2)
            ratios.append(values[red_index] / values[blue_index])
        for previous, current in zip(ratios, ratios[1:]):
            self.assertGreaterEqual(
                current, previous,
                "red/blue transmittance must not fall as the sun drops: %r"
                % (ratios,),
            )

        shares = []
        for elevation in (6, 5, 4, 3, 2, 1, 0):
            big_x, big_y, big_z = spectrum.spectrum_to_xyz(
                spectrum.transmittance(elevation, 0.2)
            )
            rgb = spectrum.xyz_to_srgb(big_x / big_y, 1.0, big_z / big_y)
            shares.append(_red_share(rgb))
        for previous, current in zip(shares, shares[1:]):
            self.assertGreaterEqual(
                current, previous,
                "sRGB red share must not fall as the sun drops: %r" % (shares,),
            )

    def test_aod_lowers_brightness(self):
        """Hard constraint 4b: a larger AOD must dim the glow."""
        clean = spectrum.deck_light(2.0, 0.05, cloud_high=60.0, cloud_mid=30.0)
        hazy = spectrum.deck_light(2.0, 0.80, cloud_high=60.0, cloud_mid=30.0)
        self.assertLess(hazy["brightness"], clean["brightness"])


class TestShadowAndAirmass(unittest.TestCase):
    def test_shadow_limits(self):
        """Each deck enters the Earth's shadow at its geometric limit."""
        expected = {
            "low": -1.24,
            "mid": -2.31,
            "high": -2.98,
        }
        for key, target in expected.items():
            actual = spectrum.shadow_limit_deg(spectrum.DECK_HEIGHT_M[key])
            self.assertAlmostEqual(actual, target, delta=0.05, msg=key)

    def test_transmittance_zero_below_shadow_limit(self):
        """0.2 deg below the limit the cloud receives no direct sunlight."""
        for height in spectrum.DECK_HEIGHT_M.values():
            limit = spectrum.shadow_limit_deg(height)
            values = spectrum.transmittance(limit - 0.2, 0.2, height)
            self.assertEqual(len(values), len(spectrum.WAVELENGTHS))
            for value in values:
                self.assertEqual(value, 0.0)

    def test_slant_airmass_endpoints_and_monotonicity(self):
        self.assertGreaterEqual(spectrum.slant_airmass(90.0, 8600.0), 0.99)
        self.assertLessEqual(spectrum.slant_airmass(90.0, 8600.0), 1.01)
        self.assertGreaterEqual(spectrum.slant_airmass(0.0, 8600.0), 18.0)
        self.assertLessEqual(spectrum.slant_airmass(0.0, 8600.0), 20.5)

        masses = [
            spectrum.slant_airmass(elevation, 8600.0)
            for elevation in (0.0, 2.0, 6.0, 15.0, 45.0, 90.0)
        ]
        for previous, current in zip(masses, masses[1:]):
            self.assertGreaterEqual(
                previous, current,
                "air mass must not increase with elevation: %r" % (masses,),
            )


class TestSunsetGlow(unittest.TestCase):
    def _glow(self, **clouds):
        params = {"cloud_low": 10.0, "cloud_mid": 20.0, "cloud_high": 45.0}
        params.update(clouds)
        return spectrum.deck_light(-0.83, 0.2, **params)

    def test_sunset_high_cloud_is_warm_and_visible(self):
        result = self._glow()
        self.assertGreaterEqual(result["brightness"], 0.30)
        self.assertIn(result["name"], ("橙金", "橙红", "玫红", "暗红"))
        self.assertGreater(result["rgb"][0], result["rgb"][2])
        self.assertEqual(result["deck"], "高云")
        self.assertEqual(result["deck_height_m"], 8600.0)

    def test_no_high_cloud_is_much_dimmer(self):
        high = self._glow()
        no_high = self._glow(cloud_high=0.0)
        self.assertGreaterEqual(
            high["brightness"] - no_high["brightness"], 0.15,
            "high=%r no_high=%r" % (high, no_high),
        )

    def test_red_blue_ratio_rises_as_high_cloud_sinks(self):
        """A sinking high cloud sees a longer path, so it must get redder.

        The old single Chapman air mass made the path *shorter* below the
        horizon (a geometric artifact) and this test used to assert the
        opposite. With the separate spherical column integrals the path
        length grows monotonically towards the shadow limit, hence the
        red/blue transmittance ratio must not fall.
        """
        red_index = spectrum.WAVELENGTHS.index(650)
        blue_index = spectrum.WAVELENGTHS.index(450)
        ratios = []
        for elevation in (-0.5, -1.0, -1.5, -2.0, -2.5):
            values = spectrum.transmittance(elevation, 0.2, 8600.0)
            ratios.append(values[red_index] / values[blue_index])
        for previous, current in zip(ratios, ratios[1:]):
            self.assertGreaterEqual(
                current, previous,
                "red/blue ratio must not fall as the sun sinks: %r" % (ratios,),
            )

    def test_all_decks_in_shadow_returns_black(self):
        result = spectrum.deck_light(
            -3.1, 0.2, cloud_low=10.0, cloud_mid=20.0, cloud_high=45.0
        )
        self.assertEqual(result["hex"], "#000000")
        self.assertEqual(result["brightness"], 0.0)
        self.assertEqual(result["name"], "中性灰")
        self.assertIsNone(result["deck"])
        self.assertIsNone(result["deck_height_m"])


class TestCanvasAndNight(unittest.TestCase):
    def test_no_cloud_canvas_is_neutral(self):
        """Hard constraint 4c: no canvas -> low-saturation cool grey/blue."""
        no_cloud = spectrum.deck_light(
            2.0, 0.2, cloud_low=2.0, cloud_mid=3.0, cloud_high=4.0
        )
        high_cloud = spectrum.deck_light(
            2.0, 0.2, cloud_low=2.0, cloud_mid=30.0, cloud_high=60.0
        )
        self.assertLess(_saturation(no_cloud["rgb"]), _saturation(high_cloud["rgb"]) * 0.5)
        self.assertIn(no_cloud["name"], ("中性灰", "灰蓝", "灰紫"))

    def test_night_brightness_is_near_zero(self):
        """Hard constraint 4d: sun <= -12 deg -> no glow."""
        for elevation in (-12.0, -15.0, -30.0):
            result = spectrum.deck_light(
                elevation, 0.2, cloud_high=60.0, cloud_mid=30.0
            )
            self.assertLessEqual(result["brightness"], 0.01)


class TestColourContract(unittest.TestCase):
    def test_deck_light_shape(self):
        result = spectrum.deck_light(4.0, 0.2, cloud_high=60.0)
        self.assertRegex(result["hex"], r"^#[0-9a-f]{6}$")
        self.assertTrue(result["name"])
        self.assertIsInstance(result["brightness"], float)
        self.assertGreaterEqual(result["brightness"], 0.0)
        self.assertLessEqual(result["brightness"], 1.0)
        self.assertEqual(len(result["rgb"]), 3)
        for channel in result["rgb"]:
            self.assertIsInstance(channel, int)
            self.assertGreaterEqual(channel, 0)
            self.assertLessEqual(channel, 255)
        self.assertEqual(result["aod_source"], "assumed")
        self.assertIn(result["deck"], ("高云", "中云", "低云", None))
        if result["deck"] is not None:
            self.assertIsInstance(result["deck_height_m"], float)

    def test_colour_names_cover_required_palette(self):
        cases = {
            "金黄": ((245, 205, 70), 0.80),
            "橙金": ((230, 140, 40), 0.50),
            "橙红": ((220, 80, 30), 0.40),
            "玫红": ((210, 40, 110), 0.50),
            "暗红": ((90, 20, 20), 0.15),
            "灰紫": ((140, 115, 160), 0.50),
            "中性灰": ((128, 128, 128), 0.50),
            "灰蓝": ((90, 110, 140), 0.45),
        }
        for expected, (rgb, brightness) in cases.items():
            self.assertEqual(
                spectrum.colour_name(rgb[0], rgb[1], rgb[2], brightness),
                expected,
                "rgb=%r brightness=%r" % (rgb, brightness),
            )

    def test_pipeline_hourly_colour_fields(self):
        from core.pipeline import build_forecast, load_config

        start = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
        forecast = build_forecast(
            load_config(), mock=True, hours=4, include_grid=False, start=start
        )
        self.assertEqual(len(forecast["spots"]), 11)
        for spot in forecast["spots"]:
            for entry in spot["hourly"]:
                self.assertRegex(entry["color_hex"], r"^#[0-9a-f]{6}$")
                self.assertTrue(entry["color_name"])
                self.assertIsInstance(entry["color_rgb"], list)
                self.assertEqual(len(entry["color_rgb"]), 3)
                self.assertGreaterEqual(entry["color_brightness"], 0.0)
                self.assertLessEqual(entry["color_brightness"], 1.0)
                self.assertEqual(entry["color_aod_source"], "assumed")
                self.assertIn(
                    entry["color_deck"], ("高云", "中云", "低云", None)
                )
                if entry["color_deck"] is None:
                    self.assertIsNone(entry["color_deck_height_m"])
                else:
                    self.assertIn(
                        entry["color_deck_height_m"],
                        (8600.0, 5200.0, 1500.0),
                    )
                # Existing BCI / SCI fields must remain untouched in shape.
                self.assertIn("bci", entry)
                self.assertIn("sci", entry)


class TestSphericalColumnAndSoftEdge(unittest.TestCase):
    def test_column_airmass_vertical_is_one(self):
        value = spectrum.column_airmass(90.0, 0.0, spectrum.H_RAYLEIGH_M)
        self.assertGreaterEqual(value, 0.99)
        self.assertLessEqual(value, 1.01)

    def test_column_airmass_horizontal_analytic(self):
        value = spectrum.column_airmass(0.0, 0.0, spectrum.H_RAYLEIGH_M)
        self.assertGreaterEqual(value, 35.0)
        self.assertLessEqual(value, 35.8)

    def test_column_airmass_high_deck(self):
        value = spectrum.column_airmass(0.0, 8600.0, spectrum.H_RAYLEIGH_M)
        self.assertGreaterEqual(value, 11.9)
        self.assertLessEqual(value, 12.3)

    def test_column_airmass_ground_hit_is_infinite(self):
        value = spectrum.column_airmass(-5.0, 1500.0, spectrum.H_AEROSOL_M)
        self.assertTrue(math.isinf(value))

    def test_transmittance_monotonic_towards_shadow_limit(self):
        index = spectrum.WAVELENGTHS.index(550)
        values = [
            spectrum.transmittance(elevation, 0.2, 8600.0)[index]
            for elevation in (0.0, -0.5, -1.0, -1.5, -2.0, -2.3, -2.6)
        ]
        for previous, current in zip(values, values[1:]):
            self.assertLessEqual(current, previous + 1e-9, msg=values)

    def test_brightness_continuous_across_shadow_edge(self):
        before = spectrum.deck_light(
            -2.90, 0.2, cloud_low=10.0, cloud_mid=20.0, cloud_high=45.0
        )["brightness"]
        after = spectrum.deck_light(
            -2.99, 0.2, cloud_low=10.0, cloud_mid=20.0, cloud_high=45.0
        )["brightness"]
        self.assertLessEqual(abs(before - after), 0.15)

    def test_transmittance_550_reference_high_sun(self):
        value = spectrum.transmittance(0.0, 0.2, 8600.0)[15]
        self.assertGreaterEqual(value, 0.27)
        self.assertLessEqual(value, 0.31)

    def test_transmittance_550_reference_sunset(self):
        value = spectrum.transmittance(-0.83, 0.2, 8600.0)[15]
        self.assertGreaterEqual(value, 0.15)
        self.assertLessEqual(value, 0.17)

    def test_low_cloud_is_extinguished_at_horizon(self):
        value = spectrum.transmittance(0.0, 0.2, 1500.0)[15]
        self.assertLessEqual(value, 0.002)

    def test_sunset_high_cloud_keeps_warm_colour(self):
        result = spectrum.deck_light(
            -0.83, cloud_high=45.0, cloud_mid=20.0, cloud_low=10.0
        )
        self.assertGreaterEqual(result["brightness"], 0.45)
        self.assertLessEqual(result["brightness"], 0.55)
        self.assertIn(result["name"], ("金黄", "橙金", "橙红"))
        self.assertGreaterEqual(result["rgb"][0], 1.3 * result["rgb"][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)

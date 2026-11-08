#!/usr/bin/env python3
"""Tests for the Earth-shadow (受光极限) criterion in the BCI sun gate.

Run directly (no network, no third-party dependencies)::

    python3 tests/test_sun_shadow.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.physics import burning_cloud, spectrum  # noqa: E402


def _old_sun_score(e, cloud_mid=50.0, cloud_high=50.0):
    if e <= -2.0:
        return 0.03 + 0.97 * burning_cloud._smoothstep(-12.0, -2.0, e)
    if e <= 6.0:
        return 1.0
    return 1.0 - 0.95 * burning_cloud._smoothstep(6.0, 26.0, e)


class TestShadowLimit(unittest.TestCase):
    def test_deck_shadow_limit_matches_spectrum(self):
        for height in (1500, 5200, 8600):
            self.assertLess(
                abs(
                    burning_cloud.deck_shadow_limit_deg(height)
                    - spectrum.shadow_limit_deg(height)
                ),
                1e-9,
            )
        self.assertAlmostEqual(
            burning_cloud.deck_shadow_limit_deg(1500), -1.243, delta=0.01
        )
        self.assertAlmostEqual(
            burning_cloud.deck_shadow_limit_deg(5200), -2.315, delta=0.01
        )
        self.assertAlmostEqual(
            burning_cloud.deck_shadow_limit_deg(8600), -2.977, delta=0.01
        )

    def test_canvas_height_weighting(self):
        self.assertAlmostEqual(burning_cloud.canvas_height_m(50, 50), 6900.0)
        self.assertAlmostEqual(burning_cloud.canvas_height_m(100, 0), 5200.0)
        self.assertAlmostEqual(burning_cloud.canvas_height_m(0, 100), 8600.0)
        self.assertAlmostEqual(burning_cloud.canvas_height_m(0, 0), 8600.0)


class TestSunScore(unittest.TestCase):
    def test_daytime_is_bit_identical_to_old_formula(self):
        for mid in (0, 50, 100):
            for high in (0, 50, 100):
                e = 0.0
                while e <= 90.0 + 1e-9:
                    self.assertEqual(
                        repr(burning_cloud._sun_score(e, mid, high)),
                        repr(_old_sun_score(e, mid, high)),
                        (e, mid, high),
                    )
                    e += 0.5

    def test_continuous_at_horizon(self):
        for mid in (0, 50, 100):
            for high in (0, 50, 100):
                diff = (
                    burning_cloud._sun_score(1e-9, mid, high)
                    - burning_cloud._sun_score(-1e-9, mid, high)
                )
                self.assertLess(diff, 1e-5)

    def test_below_horizon_is_monotonic(self):
        previous = None
        e = 0.0
        while e >= -12.0 - 1e-9:
            score = burning_cloud._sun_score(e, 50, 50)
            if previous is not None:
                self.assertLessEqual(score, previous + 1e-15)
            previous = score
            if e <= -3.0:
                self.assertLessEqual(score, 0.04)
            e -= 0.05

    def test_deep_shadow_is_dark_for_every_canvas(self):
        for mid in (0, 50, 100):
            for high in (0, 50, 100):
                self.assertLessEqual(
                    burning_cloud._sun_score(-6.0, mid, high), 0.04
                )

    def test_just_after_sunset_is_not_crushed(self):
        self.assertEqual(burning_cloud._sun_score(-1.0, 50, 40), 1.0)
        self.assertEqual(burning_cloud._sun_score(-1.5, 50, 40), 1.0)

    def test_shadow_limit_is_height_dependent(self):
        self.assertLess(
            burning_cloud._sun_score(-2.5, 100, 0),
            burning_cloud._sun_score(-2.5, 0, 100),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

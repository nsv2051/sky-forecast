#!/usr/bin/env python3
"""Offline tests for the livecam frame quality gate.

Run directly (no network, no third-party dependencies beyond Pillow)::

    python3 tests/test_livecam.py

Covers blank/uniform frame detection with synthetic JPEGs and checks the
recorded real frames under ``data/livecam/frames``. All image generation uses
a temporary directory; no ledger or frame under ``data/`` is modified.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

FRAME_ROOT = PROJECT_ROOT / "data" / "livecam" / "frames"


def _load_collect():
    path = PROJECT_ROOT / "tools" / "livecam" / "collect.py"
    spec = importlib.util.spec_from_file_location("livecam_collect", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = _load_collect()


def _save_jpeg(image: Image.Image, path: Path) -> None:
    image.convert("RGB").save(path, format="JPEG", quality=95)


def _solid_image(value: int) -> Image.Image:
    return Image.new("L", (1280, 720), value)


def _gradient_image() -> Image.Image:
    image = Image.new("L", (1280, 720))
    pixels = image.load()
    for x in range(image.width):
        value = round(x * 255 / (image.width - 1))
        for y in range(image.height):
            pixels[x, y] = value
    return image


class TestFrameBlankGate(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tempdir = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def _record(self, image: Image.Image, name: str):
        path = self.tempdir / name
        _save_jpeg(image, path)
        return collect._frame_record(path, self.tempdir), path

    def test_pure_black_is_blank(self):
        frame, path = self._record(_solid_image(0), "black.jpg")
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"], "blank_frame")
        self.assertTrue(frame["blank"])
        self.assertTrue(path.exists())
        self.assertIsNotNone(frame["mean_luma"])

    def test_pure_gray_is_blank(self):
        frame, _ = self._record(_solid_image(128), "gray.jpg")
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"], "blank_frame")
        self.assertTrue(frame["blank"])
        self.assertIsNotNone(frame["md5"])

    def test_gradient_is_not_blank(self):
        frame, _ = self._record(_gradient_image(), "gradient.jpg")
        self.assertTrue(frame["ok"])
        self.assertFalse(frame["blank"])
        self.assertIsNone(frame["error"])

    def test_real_blank_frame(self):
        path = FRAME_ROOT / "22194248" / "2026-10-23T0800.jpg"
        self.assertTrue(path.exists(), "缺少验证用真实空帧: %s" % path)
        frame = collect._frame_record(path, PROJECT_ROOT)
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"], "blank_frame")
        self.assertTrue(frame["blank"])
        self.assertEqual(frame["mean_luma"], 0.0)
        self.assertTrue(path.exists(), "空帧文件不应被删除")

    def test_real_normal_frames(self):
        for room_id in ("11235702", "1718159119"):
            with self.subTest(room_id=room_id):
                path = FRAME_ROOT / room_id / "2026-10-23T0900.jpg"
                self.assertTrue(path.exists(), "缺少验证用真实帧: %s" % path)
                frame = collect._frame_record(path, PROJECT_ROOT)
                self.assertTrue(frame["ok"])
                self.assertFalse(frame["blank"])
                self.assertIsNone(frame["error"])


class TestFrameFailureShape(unittest.TestCase):
    def test_failure_has_blank_false(self):
        path = Path("/nonexistent/room/2026-10-23T0900.jpg")
        frame = collect._frame_failure(path, PROJECT_ROOT, "抓帧失败")
        self.assertFalse(frame["ok"])
        self.assertFalse(frame["blank"])
        self.assertEqual(frame["error"], "抓帧失败")


class TestFrameBlankHelper(unittest.TestCase):
    def test_none_stats_do_not_misjudge(self):
        self.assertIsNone(collect._frame_blank({"std_luma": None, "mean_luma": 0.0}))
        self.assertIsNone(collect._frame_blank({"std_luma": 10.0, "mean_luma": None}))
        self.assertIsNone(collect._frame_blank({}))

    def test_uniform_and_dark(self):
        self.assertEqual(
            collect._frame_blank({"std_luma": 0.0, "mean_luma": 128.0}),
            "blank_frame",
        )
        self.assertEqual(
            collect._frame_blank({"std_luma": 5.0, "mean_luma": 0.0}),
            "blank_frame",
        )
        self.assertIsNone(
            collect._frame_blank({"std_luma": 5.0, "mean_luma": 50.0})
        )


class TestLevelsAbove(unittest.TestCase):
    def test_counts_at_or_above_and_skips_bad_layers(self):
        profile = [
            {"height": 0},
            {"height": 1000},
            {"height": 1500},
            {"height": None},
            {"height": "not-a-number"},
            {"temperature": 1.0},
        ]
        self.assertEqual(collect._levels_above(profile, 1000.0), 2)
        self.assertEqual(collect._levels_above(None, 1000.0), 0)


class TestInsufficientProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.physics = collect._load_physics()

    def test_one_level_above_yields_null_sci(self):
        cam = {"elev_m": 3134.0}
        profile = [
            {"height": 3165.0, "temperature": -2.0, "relative_humidity": 74.0}
        ]
        result = collect._predict(self.physics, cam, {}, profile)
        self.assertIsNone(result["sci"])
        self.assertEqual(result["status"], "廓线不足")
        self.assertEqual(result["state"], "insufficient_profile")
        self.assertIsNone(result["inversion_height_m"])
        self.assertEqual(result["components"]["levels_above"], 1)

    def test_four_levels_above_computes_numeric_sci(self):
        cam = {"elev_m": 1000.0}
        profile = [
            {"height": 500.0, "temperature": 20.0, "relative_humidity": 60.0},
            {"height": 1000.0, "temperature": 16.0, "relative_humidity": 55.0},
            {"height": 1500.0, "temperature": 12.0, "relative_humidity": 50.0},
            {"height": 2000.0, "temperature": 8.0, "relative_humidity": 45.0},
            {"height": 2500.0, "temperature": 4.0, "relative_humidity": 40.0},
        ]
        result = collect._predict(self.physics, cam, {}, profile)
        self.assertIsInstance(result["sci"], (int, float))
        self.assertIsNotNone(result["sci"])
        self.assertEqual(result["components"]["levels_above"], 4)


class TestColumnCloud(unittest.TestCase):
    BRIGHT_FRAME = {"ok": True, "mean_luma": 120.0, "std_luma": 60.0}
    HIGH_LOW_CLOUD = {
        "cloud_cover_low": 98.0,
        "cloud_cover_mid": 10.0,
        "cloud_cover_high": 0.0,
    }

    def test_high_low_cloud_with_bright_frame_conflicts(self):
        result = collect._column_cloud(
            {"cloud_cover_low": 90.0, "cloud_cover_mid": 10.0, "cloud_cover_high": 0.0},
            {"ok": True, "mean_luma": 120.0, "std_luma": 60.0},
            32.0,
        )
        self.assertTrue(result["conflict"])
        self.assertEqual(result["conflict_reason"], "低云量90%但画面明亮通透")
        self.assertEqual(result["max_cover"], 90.0)
        self.assertTrue(result["conflict_applicable"])
        self.assertEqual(result["viewpoint_elevation_m"], 32.0)

    def test_zero_low_cloud_never_conflicts(self):
        result = collect._column_cloud(
            {"cloud_cover_low": 0.0, "cloud_cover_mid": 10.0, "cloud_cover_high": 0.0},
            {"ok": True, "mean_luma": 120.0, "std_luma": 60.0},
            32.0,
        )
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["conflict_reason"])

    def test_failed_frame_never_conflicts(self):
        result = collect._column_cloud(
            {"cloud_cover_low": 90.0},
            {"ok": False, "mean_luma": 0.0, "std_luma": 0.0},
            32.0,
        )
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["conflict_reason"])

    def test_all_none_cloud_has_none_max_cover(self):
        result = collect._column_cloud(
            {"cloud_cover_low": None, "cloud_cover_mid": None, "cloud_cover_high": None},
            {"ok": True, "mean_luma": 120.0, "std_luma": 60.0},
            32.0,
        )
        self.assertIsNone(result["max_cover"])
        self.assertIsNone(result["low"])
        self.assertFalse(result["conflict"])

    def test_below_band_top_conflict_applies(self):
        for elevation in (32.0, 1000.0):
            with self.subTest(elevation=elevation):
                result = collect._column_cloud(
                    self.HIGH_LOW_CLOUD, self.BRIGHT_FRAME, elevation
                )
                self.assertTrue(result["conflict_applicable"])
                self.assertTrue(result["conflict"])
                self.assertEqual(result["viewpoint_elevation_m"], elevation)

    def test_high_altitude_is_sea_of_clouds_signal_not_conflict(self):
        result = collect._column_cloud(
            self.HIGH_LOW_CLOUD, self.BRIGHT_FRAME, 3134.0
        )
        self.assertFalse(result["conflict_applicable"])
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["conflict_reason"])
        self.assertEqual(result["viewpoint_elevation_m"], 3134.0)

    def test_band_top_boundary_is_not_applicable(self):
        result = collect._column_cloud(
            self.HIGH_LOW_CLOUD, self.BRIGHT_FRAME, 2000.0
        )
        self.assertFalse(result["conflict_applicable"])
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["conflict_reason"])

    def test_missing_elevation_is_not_applicable(self):
        result = collect._column_cloud(
            self.HIGH_LOW_CLOUD, self.BRIGHT_FRAME, None
        )
        self.assertFalse(result["conflict_applicable"])
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["conflict_reason"])
        self.assertIsNone(result["viewpoint_elevation_m"])

    def test_non_numeric_elevation_is_not_applicable(self):
        result = collect._column_cloud(
            self.HIGH_LOW_CLOUD, self.BRIGHT_FRAME, "unknown"
        )
        self.assertFalse(result["conflict_applicable"])
        self.assertFalse(result["conflict"])
        self.assertIsNone(result["viewpoint_elevation_m"])


if __name__ == "__main__":
    unittest.main()

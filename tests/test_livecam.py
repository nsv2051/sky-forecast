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


if __name__ == "__main__":
    unittest.main()

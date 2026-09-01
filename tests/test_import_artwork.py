"""The artwork importer: does it cut supplied files up correctly?

Every fixture here is a coloured rectangle, deliberately. The importer must
never care what the artwork depicts - its whole job is to move somebody else's
pixels around without touching them - so testing it against a drawing of Py
would test the drawing rather than the tool.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_import_artwork -v
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-import-"))

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "import_py_artwork", os.path.join(_ROOT, "scripts", "import_py_artwork.py"))
importer = importlib.util.module_from_spec(_SPEC)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _SPEC.loader.exec_module(importer)


def blank(width: int, height: int, colour: str | None = None) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour) if colour else QColor(0, 0, 0, 0))
    return image


def stamp(image: QImage, rect: QRect, colour: str = "#E8722C") -> QImage:
    painter = QPainter(image)
    painter.fillRect(rect, QColor(colour))
    painter.end()
    return image


class TrimTests(unittest.TestCase):
    def test_content_box_is_tight_around_the_pixels(self) -> None:
        image = stamp(blank(200, 200), QRect(40, 30, 60, 90))
        self.assertEqual(importer.content_box(image), QRect(40, 30, 60, 90))

    def test_an_empty_image_has_no_content_box(self) -> None:
        self.assertTrue(importer.content_box(blank(50, 50)).isNull())

    def test_nearly_transparent_haze_is_not_content(self) -> None:
        # Artwork exported from a painting tool carries a fringe of alpha-1
        # pixels. Trimming to alpha > 0 leaves a margin of invisible nothing.
        image = stamp(blank(120, 120), QRect(50, 50, 20, 20))
        painter = QPainter(image)
        painter.fillRect(QRect(0, 0, 120, 4), QColor(232, 114, 44, 6))
        painter.end()
        self.assertEqual(importer.content_box(image), QRect(50, 50, 20, 20))


class BackgroundTests(unittest.TestCase):
    def test_a_flat_backdrop_is_knocked_out(self) -> None:
        image = stamp(blank(120, 120, "#FAFAFC"), QRect(40, 40, 40, 40))
        out, removed = importer.knock_out_background(image)
        self.assertTrue(removed)
        self.assertEqual(importer.content_box(out), QRect(40, 40, 40, 40))

    def test_artwork_that_is_already_transparent_is_left_alone(self) -> None:
        image = stamp(blank(120, 120), QRect(40, 40, 40, 40))
        out, removed = importer.knock_out_background(image)
        self.assertFalse(removed)
        self.assertEqual(importer.content_box(out), QRect(40, 40, 40, 40))

    def test_an_enclosed_highlight_survives(self) -> None:
        """The flood runs from the border, so a white eye inside the figure is
        not background even though it matches the backdrop exactly."""
        image = stamp(blank(120, 120, "#FFFFFF"), QRect(30, 30, 60, 60))
        stamp(image, QRect(50, 50, 8, 8), "#FFFFFF")
        out, _ = importer.knock_out_background(image)
        self.assertEqual(importer.content_box(out), QRect(30, 30, 60, 60),
                         "the enclosed highlight was eaten")

    def test_a_busy_backdrop_is_not_touched(self) -> None:
        image = blank(80, 80, "#FFFFFF")
        stamp(image, QRect(0, 0, 40, 80), "#101010")
        out, removed = importer.knock_out_background(image)
        self.assertFalse(removed, "corners disagreed; it is not a flat backdrop")


class SheetTests(unittest.TestCase):
    def test_a_row_of_figures_is_split_left_to_right(self) -> None:
        image = blank(400, 120)
        for index in range(4):
            stamp(image, QRect(20 + index * 96, 20, 40, 80))
        boxes = importer.split_figures(image)
        self.assertEqual(len(boxes), 4)
        self.assertEqual([box.left() for box in boxes], [20, 116, 212, 308])

    def test_rows_are_split_before_columns(self) -> None:
        image = blank(300, 300)
        for row in range(2):
            for column in range(3):
                stamp(image, QRect(20 + column * 90, 20 + row * 150, 40, 80))
        self.assertEqual(len(importer.split_figures(image)), 6)

    def test_specks_are_not_figures(self) -> None:
        image = blank(300, 120)
        stamp(image, QRect(20, 20, 60, 80))
        stamp(image, QRect(200, 40, 3, 3))
        self.assertEqual(len(importer.split_figures(image)), 1)

    def test_one_figure_stays_one_figure(self) -> None:
        image = stamp(blank(200, 300), QRect(60, 20, 70, 260))
        self.assertEqual(len(importer.split_figures(image)), 1)


class PanelCropTests(unittest.TestCase):
    def _figure(self) -> tuple[QImage, QRect]:
        """A narrow head on a wider body - the shape the crop has to read."""
        image = blank(240, 440)
        stamp(image, QRect(90, 20, 60, 90))     # head
        stamp(image, QRect(60, 110, 120, 200))  # body, wider
        stamp(image, QRect(80, 310, 80, 110))   # legs
        return image, importer.content_box(image)

    def test_the_crop_is_square(self) -> None:
        image, figure = self._figure()
        box = importer.panel_box(image, figure)
        self.assertEqual(box.width(), box.height())

    def test_the_crop_is_centred_on_the_head_not_on_the_body(self) -> None:
        image = blank(300, 440)
        stamp(image, QRect(150, 20, 60, 90))     # head, off to the right
        stamp(image, QRect(40, 110, 220, 300))   # body, sprawling left
        box = importer.panel_box(image, importer.content_box(image))
        self.assertAlmostEqual(box.left() + box.width() / 2, 180, delta=12)

    def test_the_crop_keeps_the_whole_head(self) -> None:
        image, figure = self._figure()
        box = importer.panel_box(image, figure)
        self.assertLessEqual(box.top(), 20, "the top of the head was cut off")
        self.assertGreaterEqual(box.bottom(), 110, "no shoulders in the bust")

    def test_the_crop_stays_inside_the_image(self) -> None:
        image, figure = self._figure()
        box = importer.panel_box(image, figure, spread=6.0)
        self.assertTrue(image.rect().contains(box), f"{box} escaped the canvas")


class NeverUpscaleTests(unittest.TestCase):
    def test_small_artwork_is_left_at_its_own_size(self) -> None:
        # Enlarging supplied artwork is the one way to make it look worse than
        # it is; a small file stays small and the UI scales it down instead.
        small = blank(64, 64, "#E8722C")
        self.assertEqual(importer._scaled_copy(small, 880).width(), 64)

    def test_large_artwork_is_scaled_down_keeping_its_aspect(self) -> None:
        big = blank(2000, 4000, "#E8722C")
        out = importer._scaled_copy(big, 880)
        self.assertEqual(out.height(), 880)
        self.assertEqual(out.width(), 440)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.out = tempfile.mkdtemp(prefix="py-import-out-")
        self.src = tempfile.mkdtemp(prefix="py-import-src-")

    def tearDown(self) -> None:
        shutil.rmtree(self.out, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def test_a_figure_becomes_a_full_and_a_panel_file(self) -> None:
        image = blank(240, 440)
        stamp(image, QRect(90, 20, 60, 90))
        stamp(image, QRect(60, 110, 120, 300))
        importer.emit(image, importer.content_box(image), "idle", self.out,
                      dry_run=False, retina=False, full_longest=400,
                      panel_longest=128, panel_opts={})
        written = sorted(os.listdir(self.out))
        self.assertEqual(written, ["idle-full.png", "idle-panel.png"])
        panel = QImage(os.path.join(self.out, "idle-panel.png"))
        self.assertEqual(panel.width(), panel.height(), "the bust is not square")

    def test_the_written_files_carry_transparency(self) -> None:
        image = stamp(blank(200, 400, "#FFFFFF"), QRect(70, 30, 60, 340))
        cleaned, _ = importer.knock_out_background(image)
        importer.emit(cleaned, importer.content_box(cleaned), "idle", self.out,
                      dry_run=False, retina=False, full_longest=400,
                      panel_longest=128, panel_opts={})
        full = QImage(os.path.join(self.out, "idle-full.png"))
        self.assertTrue(full.hasAlphaChannel())
        # A corner of the trimmed figure box is still background, and must not
        # have been baked in as white.
        self.assertEqual(full.pixelColor(0, 0).alpha(), 255,
                         "trimmed tight, so the corner is the figure itself")

    def test_the_mascot_finds_what_the_importer_wrote(self) -> None:
        """The point of the whole exercise: files land where asset_for looks."""
        from app.ui import mascot as mascot_module

        image = blank(240, 440)
        stamp(image, QRect(90, 20, 60, 90))
        stamp(image, QRect(60, 110, 120, 300))
        importer.emit(image, importer.content_box(image), "idle", self.out,
                      dry_run=False, retina=False, full_longest=400,
                      panel_longest=128, panel_opts={})
        real = mascot_module.ASSET_DIR
        try:
            mascot_module.ASSET_DIR = self.out
            self.assertTrue(mascot_module.has_final_artwork())
            self.assertTrue(mascot_module.asset_for("idle", "full").endswith("idle-full.png"))
            # Nothing else was supplied, so every other state falls back to it.
            self.assertTrue(mascot_module.asset_for("stuck", "panel").endswith("idle-panel.png"))
        finally:
            mascot_module.ASSET_DIR = real


if __name__ == "__main__":
    unittest.main()

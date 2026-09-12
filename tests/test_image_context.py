"""Image context: local image files and page screenshots the user
explicitly captured or picked - see app/browser/image_context.py.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_image_context -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.image_context import (  # noqa: E402
    MAX_IMAGE_BYTES, ImageContextError, read_local_image, screenshot_attachment,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _make_png(path: str) -> None:
    pixmap = QPixmap(4, 4)
    pixmap.fill(QColor("red"))
    assert pixmap.save(path, "PNG")


class ReadLocalImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._paths: list[str] = []

    def tearDown(self) -> None:
        for path in self._paths:
            if os.path.exists(path):
                os.unlink(path)

    def _make(self, suffix: str) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self._paths.append(path)
        _make_png(path)
        return path

    def test_a_png_file_is_read_with_the_right_mime_type(self) -> None:
        path = self._make(".png")
        attachment = read_local_image(path)
        self.assertEqual(attachment.mime_type, "image/png")
        self.assertTrue(attachment.data)

    def test_base64_round_trips_the_bytes(self) -> None:
        import base64

        path = self._make(".png")
        attachment = read_local_image(path)
        self.assertEqual(base64.b64decode(attachment.base64), attachment.data)

    def test_the_filename_is_reported(self) -> None:
        path = self._make(".png")
        attachment = read_local_image(path)
        self.assertEqual(attachment.filename, os.path.basename(path))

    def test_an_unsupported_extension_is_refused(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".tiff")
        os.close(fd)
        self._paths.append(path)
        with self.assertRaises(ImageContextError):
            read_local_image(path)

    def test_a_missing_file_is_refused_cleanly(self) -> None:
        with self.assertRaises(ImageContextError):
            read_local_image("/no/such/image.png")

    def test_a_file_over_the_size_limit_is_refused(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"x" * (MAX_IMAGE_BYTES + 1))
        self._paths.append(path)
        with self.assertRaises(ImageContextError):
            read_local_image(path)


class ScreenshotAttachmentTests(unittest.TestCase):
    def test_a_pixmap_is_encoded_as_png_bytes(self) -> None:
        pixmap = QPixmap(10, 10)
        pixmap.fill(QColor("blue"))
        attachment = screenshot_attachment(pixmap, source="screenshot:https://example.com/")
        self.assertEqual(attachment.mime_type, "image/png")
        self.assertTrue(attachment.data.startswith(b"\x89PNG"))
        self.assertEqual(attachment.source, "screenshot:https://example.com/")


if __name__ == "__main__":
    unittest.main()

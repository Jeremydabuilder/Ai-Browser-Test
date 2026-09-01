"""Iframe support: reading and acting on content inside embedded documents.

Until now the page representation stopped at the frame boundary, so an
embedded login, comment widget or player was invisible. Qt 6.8's
QWebEngineFrame lets each frame be scripted directly, and our automation
script already runs in every frame, so this is routing rather than injection.

The fixture page embeds three frames on purpose:

  * a same-origin child,
  * a `srcdoc` child (no URL at all),
  * a cross-origin child - `localhost` and `127.0.0.1` are different origins to
    the browser even on one port, which is the case that actually matters.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_frames -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-frame-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.controller import MAX_FRAMES, BrowserController, _frame_tag  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def settle(ms: int) -> None:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(ms)
    while not expired[0]:
        _app.processEvents()


class RefTests(unittest.TestCase):
    def test_frame_tag_parsing(self) -> None:
        self.assertEqual(_frame_tag("s3:e12"), "")
        self.assertEqual(_frame_tag("s3.2:e12"), "2")
        self.assertEqual(_frame_tag("s10.11:f0"), "11")


class FrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(1100, 900)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.browser.navigate(_server.url("/frames")).wait(30000)
        # Subframes load after the parent's loadFinished; give them a moment.
        settle(900)

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def structure(self, **kwargs):
        result = self.browser.get_page_structure(**kwargs).wait(30000)
        self.assertIsNotNone(result, "the capture never returned")
        self.assertTrue(result.ok, result.error.message if result.error else "")
        return result.data["structure"]

    # -- discovery ---------------------------------------------------------
    def test_all_three_frames_are_found(self) -> None:
        structure = self.structure()
        self.assertEqual(len(structure.frames), 3,
                         f"expected 3 frames, got {structure.frames}")

    def test_a_cross_origin_frame_is_labelled_as_such(self) -> None:
        by_origin = {f["same_origin"] for f in self.structure().frames}
        self.assertIn(False, by_origin, "no frame was reported as cross-origin")
        self.assertIn(True, by_origin, "no frame was reported as same-origin")

    def test_frames_can_be_left_out(self) -> None:
        structure = self.structure(include_frames=False)
        self.assertEqual(structure.frames, [])
        self.assertTrue(all(e.frame is None for e in structure.elements))

    # -- reading -----------------------------------------------------------
    def test_elements_inside_a_frame_are_listed(self) -> None:
        names = {e.name for e in self.structure().elements}
        self.assertIn("Outer button", names)
        self.assertIn("Inner button", names, "iframe content is still invisible")

    def test_frame_elements_carry_their_frame_index(self) -> None:
        inner = [e for e in self.structure().elements if e.name == "Inner button"]
        self.assertTrue(inner)
        self.assertIsNotNone(inner[0].frame)
        self.assertTrue(inner[0].ref.startswith("s"))
        self.assertIn(".", inner[0].ref.split(":")[0])

    def test_a_srcdoc_frame_is_read_too(self) -> None:
        names = {e.name for e in self.structure().elements}
        self.assertIn("Srcdoc button", names)

    def test_frame_headings_are_included(self) -> None:
        headings = {h.text for h in self.structure().headings}
        self.assertIn("Frames Host", headings)
        self.assertIn("Inner heading", headings)

    def test_frame_text_is_labelled_with_its_origin(self) -> None:
        text = self.structure().text
        self.assertIn("Outer paragraph.", text)
        self.assertIn("Text that lives inside the iframe.", text)
        # Labelled, so a summary can say where a claim came from.
        self.assertIn("[frame", text)

    def test_the_main_document_still_reports_its_own_url(self) -> None:
        structure = self.structure()
        self.assertTrue(structure.url.endswith("/frames"))

    # -- acting ------------------------------------------------------------
    def test_typing_into_a_field_inside_a_frame(self) -> None:
        structure = self.structure()
        field = next(e for e in structure.elements if e.name == "Inner field")
        result = self.browser.type_text(field.ref, "hello from the agent").wait(20000)
        self.assertTrue(result.ok, result.error.message if result.error else "")
        again = self.structure()
        typed = next(e for e in again.elements if e.name == "Inner field")
        self.assertEqual(typed.value, "hello from the agent")

    def test_clicking_a_button_inside_a_frame(self) -> None:
        structure = self.structure()
        button = next(e for e in structure.elements if e.name == "Inner button")
        result = self.browser.click(button.ref).wait(20000)
        self.assertTrue(result.ok, result.error.message if result.error else "")

    def test_inspecting_an_element_inside_a_frame(self) -> None:
        structure = self.structure()
        button = next(e for e in structure.elements if e.name == "Inner button")
        result = self.browser.inspect_element(button.ref).wait(20000)
        self.assertTrue(result.ok, result.error.message if result.error else "")

    def test_a_main_document_reference_is_unaffected(self) -> None:
        structure = self.structure()
        button = next(e for e in structure.elements if e.name == "Outer button")
        self.assertNotIn(".", button.ref.split(":")[0])
        self.assertTrue(self.browser.click(button.ref).wait(20000).ok)

    def test_a_reference_into_a_frame_that_is_gone_is_recoverable(self) -> None:
        structure = self.structure()
        inner = next(e for e in structure.elements if e.name == "Inner button")
        self.browser.navigate(_server.url("/second")).wait(30000)
        result = self.browser.click(inner.ref).wait(20000)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.recoverable,
                        "the agent must be told to look again, not to give up")

    # -- bounds ------------------------------------------------------------
    def test_frame_walking_is_bounded(self) -> None:
        self.assertLessEqual(len(self.browser._frames(self.tabs.current_tab())),
                             MAX_FRAMES + 1)


if __name__ == "__main__":
    unittest.main()

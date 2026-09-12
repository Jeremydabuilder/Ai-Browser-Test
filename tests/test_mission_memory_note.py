"""MissionPicker's explicit memory note - what Py remembers, said plainly,
with a link to review or delete it. Added for the AI-browser capability
audit: memory must be explicit and user-controlled, never a silent profile.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_memory_note -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.ui.missions.mission_picker import MissionPicker  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _FakeService:
    def recent(self, limit):
        return []


class _FakeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.library_opened = False

    def _show_mission_library(self) -> None:
        self.library_opened = True


class MemoryNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = _FakeWindow()
        self.picker = MissionPicker(_FakeService(), self.window)

    def tearDown(self) -> None:
        self.picker.deleteLater()
        self.window.deleteLater()
        _app.processEvents()

    def test_the_note_says_what_is_and_is_not_remembered(self) -> None:
        text = self.picker.memory_note.text()
        self.assertIn("Mission", text)
        self.assertIn("does not track", text)

    def test_the_note_does_not_overclaim_cross_mission_or_history_memory(self) -> None:
        text = self.picker.memory_note.text().lower()
        # Explicitly never claim browsing history or a persistent profile is
        # kept - that would be broader than what Missions actually store.
        self.assertNotIn("history", text)
        self.assertNotIn("profile", text)

    def test_the_link_opens_the_mission_library(self) -> None:
        self.picker.memory_note.linkActivated.emit("library")
        self.assertTrue(self.window.library_opened)


if __name__ == "__main__":
    unittest.main()

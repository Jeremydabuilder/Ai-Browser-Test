"""The real window's collaboration wiring (a regression: it was handed the
Mission *service* where the Mission *store* was required, so sharing a Mission
from the app would have failed on first sync even though every store-level test
passed).

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_collaboration_window
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-collabwin-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.collaboration.types import Role  # noqa: E402
from app.missions.repository import MissionStore  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402
from tests.test_collaboration import _make_fake_key_store_factory  # noqa: E402

_app = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)
    _profile = shared_profile()


class WindowWiring(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": "1"})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        for _ in range(8):
            _app.processEvents()
            time.sleep(0.003)

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._env.stop()
        self._dir.cleanup()

    def test_collaboration_uses_the_mission_store_and_can_share_and_sync(self) -> None:
        collab = self.window.collab_service
        self.assertIsInstance(collab._missions, MissionStore)
        collab._key_store = _make_fake_key_store_factory()  # never touch the OS keyring
        mission = self.window.mission_store.create("Wired", "goal")
        folder = tempfile.mkdtemp(prefix="pybrowser-collabwin-folder-")
        with mock.patch.object(type(collab), "_key_store", lambda self, gid: _make_fake_key_store_factory()(gid)):
            collab.share_mission(mission.id, folder)
            result = collab.sync_now(mission.id)
        self.assertNotEqual(result.status, "error", result.error)

    def test_a_viewer_cannot_add_findings_through_the_mission_service(self) -> None:
        mission = self.window.mission_store.create("Shared", "goal")
        with mock.patch.object(type(self.window.collab_service), "role_for",
                               lambda self, mid: Role.VIEWER):
            self.assertIn("viewer", self.window.missions.write_refusal(mission.id))
        self.assertIsNone(self.window.missions.write_refusal(mission.id))


if __name__ == "__main__":
    unittest.main()

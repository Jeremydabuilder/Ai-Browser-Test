"""First-run help: the offline sample is honest, setup starts on a free option, and the
no-provider states point somewhere useful.

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_first_run
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser import aimode, demo, internal  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402
from app.ui import theme  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)


def pump(n=8):
    for _ in range(n):
        _app.processEvents()
        time.sleep(0.003)


class OfflineSample(unittest.TestCase):
    def html(self) -> str:
        return internal._ROUTES[demo.HOST](None)

    def test_it_is_served_and_says_plainly_it_is_not_model_output(self):
        html = self.html()
        self.assertIn("Demonstration only", html)
        self.assertIn("No AI model was used", html)
        self.assertIn("fictional", html)

    def test_it_loads_nothing_and_runs_nothing(self):
        html = self.html()
        self.assertNotRegex(html, r"<script|<iframe|<img|<link|https?://")

    def test_it_demonstrates_what_a_real_result_must_keep_visible(self):
        text = re.sub(r"<[^>]+>", " ", self.html())
        for needle in ("Your requirements", "Not found in the sources", "Search snippet only",
                       "Could not be read", "not a fact check", "judgement"):
            self.assertIn(needle, text)


class SetupStartsFree(unittest.TestCase):
    def dialog(self, **stored):
        from app.ui.agent_setup import ApiKeyDialog
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(os.path.join(tmp.name, "t.sqlite3"))
        self.addCleanup(db.close)
        settings = SettingsStore(db)
        for k, v in stored.items():
            settings.set(k, v)
        d = ApiKeyDialog(None, settings)
        self.addCleanup(d.deleteLater)
        return d

    def test_a_fresh_profile_starts_on_the_free_tier_option(self):
        self.assertEqual(self.dialog().provider_box.currentData(), "gemini")

    def test_an_existing_choice_is_kept(self):
        self.assertEqual(self.dialog(agent_provider="anthropic").provider_box.currentData(), "anthropic")

    def test_every_providers_panel_has_named_controls(self):
        from app.ui import a11y
        d = self.dialog()
        d.show()
        for i in range(d.provider_box.count()):
            d.provider_box.setCurrentIndex(i)
            pump()
            self.assertEqual(a11y.missing_names(d), [], d.provider_box.currentData())

    def test_a_feature_can_still_ask_for_a_specific_provider(self):
        from app.ui.agent_setup import ApiKeyDialog
        d = ApiKeyDialog(None, None, initial_provider="groq")
        self.addCleanup(d.deleteLater)
        self.assertEqual(d.provider_box.currentData(), "groq")


class NoProviderPage(unittest.TestCase):
    def test_the_ai_mode_page_offers_the_sample_and_names_the_no_cost_routes(self):
        page = aimode.render('{"turns": [], "providerReady": false, "searchReady": false}', dark=False)
        self.assertIn('act("demo"', page)
        self.assertIn("offline demonstration", page)
        self.assertIn("needs no key", page)

    def test_the_demo_action_opens_the_sample_in_a_tab(self):
        from app.ui.main_window import MainWindow
        with mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": "1"}):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            db = Database(os.path.join(tmp.name, "t.sqlite3"))
            self.addCleanup(db.close)
            window = MainWindow(shared_profile(), db, start_urls=["about:blank"])
            self.addCleanup(window.close)
            pump()
            before = window.tabs.count()
            window._on_aimode_action("demo", {})
            pump()
            self.assertEqual(window.tabs.count(), before + 1)
            self.assertEqual(window.tabs.current_tab().url().toString(), "pybrowser://demo/")


if __name__ == "__main__":
    unittest.main()

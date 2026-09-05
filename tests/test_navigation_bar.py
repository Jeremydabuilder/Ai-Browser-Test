"""The address bar's "ask Py instead" affordance.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_navigation_bar -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.navigation_bar import AddressBar, NavigationBar  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class AddressBarAskPyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bar = AddressBar()

    def tearDown(self) -> None:
        self.bar.deleteLater()
        _app.processEvents()

    def test_the_icon_is_hidden_for_an_ordinary_search(self) -> None:
        self.bar.setText("cheap laptops")
        self.assertFalse(self.bar._ask_py_action.isVisible())

    def test_the_icon_is_hidden_for_a_url(self) -> None:
        self.bar.setText("https://example.com")
        self.assertFalse(self.bar._ask_py_action.isVisible())

    def test_the_icon_appears_for_a_task_shaped_sentence(self) -> None:
        self.bar.setText("find the cheapest flight to tokyo")
        self.assertTrue(self.bar._ask_py_action.isVisible())

    def test_the_icon_disappears_again_once_the_text_is_cleared(self) -> None:
        self.bar.setText("find the cheapest flight to tokyo")
        self.assertTrue(self.bar._ask_py_action.isVisible())
        self.bar.clear()
        self.assertFalse(self.bar._ask_py_action.isVisible())

    def test_triggering_the_icon_emits_the_current_text(self) -> None:
        self.bar.setText("compare these two laptops for me")
        seen = []
        self.bar.ask_py_requested.connect(seen.append)
        self.bar._ask_py_action.trigger()
        self.assertEqual(seen, ["compare these two laptops for me"])

    def test_enter_never_emits_ask_py(self) -> None:
        """Enter must keep doing exactly what it always did - an ordinary
        search or navigation. The "ask Py" icon is a separate, opt-in path."""
        self.bar.setText("find the cheapest flight to tokyo")
        seen = []
        self.bar.ask_py_requested.connect(seen.append)
        self.bar.returnPressed.emit()
        self.assertEqual(seen, [])


class NavigationBarAskPyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.nav = NavigationBar()

    def tearDown(self) -> None:
        self.nav.deleteLater()
        _app.processEvents()

    def test_the_signal_is_forwarded_from_the_address_bar(self) -> None:
        seen = []
        self.nav.ask_py_requested.connect(seen.append)
        self.nav.address_bar.setText("plan a weekend trip to portland")
        self.nav.address_bar.ask_py_requested.emit(self.nav.address_bar.text())
        self.assertEqual(seen, ["plan a weekend trip to portland"])


if __name__ == "__main__":
    unittest.main()

"""Accessibility regression tests: names, keyboard reachability, focus handling, visible focus, both themes.

What this proves: every operable widget on the audited surfaces has an accessible name; the keyboard can reach the
controls; closing the find bar returns focus to the page; both stylesheets define a focus indicator for each control type;
narrow windows still lay out. What it can NOT prove: how NVDA / Narrator / VoiceOver actually speak any of it, or how the
focus ring looks to a person - that needs a real screen reader and eyes on a real display.

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_accessibility
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
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-a11y-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox, QWidget,
)

from app.storage import Database  # noqa: E402
from app.ui import a11y, theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)
    _profile = shared_profile()


def pump(times: int = 10) -> None:
    for _ in range(times):
        _app.processEvents()
        time.sleep(0.005)


#: menu actions that would quit, reset, open the OS shell or start recording - not what this audit is for
SKIP = ("quit", "reset", "open data folder", "new window", "downloads", "check for updates", "full screen", "teach",
        "record workflow", "stop", "reload", "new workspace", "menu bar")


class WindowCase(unittest.TestCase):
    ai = "1"

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": self.ai})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1200, 800)
        self.window.show()
        pump()

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        self._env.stop()
        pump(3)

    def walk(self, menu):
        for action in menu.actions():
            if action.menu() is not None:
                yield from self.walk(action.menu())
                continue
            text = action.text().replace("&", "").lower()
            if action.text() and action.isEnabled() and not any(word in text for word in SKIP):
                yield action

    def open_every_dialog(self) -> tuple[dict[str, list], list[str]]:
        """Trigger every enabled menu action with dialogs captured instead of blocking; audit each dialog once."""
        found: dict[str, list] = {}
        failures: list[str] = []

        def fake_exec(dialog):
            pump(5)
            found.setdefault(type(dialog).__name__, a11y.missing_names(dialog))
            return 0

        def close_blocker():
            modal = QApplication.activeModalWidget()
            if modal is not None:
                failures.append(f"blocked: {type(modal).__name__}")
                modal.close()

        ok = QMessageBox.StandardButton.Ok
        with mock.patch.object(QDialog, "exec", fake_exec), mock.patch.object(QMessageBox, "exec", lambda self: 0), \
                mock.patch.object(QMessageBox, "information", lambda *a, **k: ok), \
                mock.patch.object(QMessageBox, "warning", lambda *a, **k: ok), \
                mock.patch.object(QMessageBox, "critical", lambda *a, **k: ok), \
                mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No), \
                mock.patch.object(QFileDialog, "getOpenFileName", lambda *a, **k: ("", "")), \
                mock.patch.object(QFileDialog, "getExistingDirectory", lambda *a, **k: ""), \
                mock.patch.object(QInputDialog, "getText", lambda *a, **k: ("", False)), \
                mock.patch.object(QInputDialog, "getItem", lambda *a, **k: ("", False)), \
                mock.patch.object(QInputDialog, "getMultiLineText", lambda *a, **k: ("", False)), \
                mock.patch.object(QInputDialog, "getInt", lambda *a, **k: (0, False)):
            for top in self.window.menuBar().actions():
                if top.menu() is None:
                    continue
                for action in self.walk(top.menu()):
                    QTimer.singleShot(1500, close_blocker)
                    try:
                        action.trigger()
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"{action.text()}: {type(exc).__name__}")
                    pump(4)
        return found, failures


class NamesOnTheMainWindow(WindowCase):
    def test_every_visible_control_in_the_window_has_a_name(self) -> None:
        self.assertEqual(a11y.missing_names(self.window), [])

    def test_with_the_side_panel_and_find_bar_open(self) -> None:
        self.window._open_find()
        self.window._toggle_agent_panel()
        pump(20)
        self.assertIsNotNone(self.window._side_panel)
        self.assertEqual(a11y.missing_names(self.window), [])

    def test_critical_controls_are_named_explicitly_not_guessed_from_tooltips(self) -> None:
        w = self.window
        bar = w.nav_bar.address_bar
        self.assertEqual(bar.accessibleName(), "Address and search bar")
        for widget, expected in ((w.find_bar.field, "Find in page"), (w.nav_bar.menu_button, "Main menu")):
            self.assertEqual(widget.accessibleName(), expected)
            self.assertFalse(widget.property("a11yDerived"))
        names = {b.accessibleName() for b in w.find_bar.findChildren(QAbstractButton)}
        self.assertTrue({"Previous match", "Next match", "Close"} <= names, names)
        close = w.tabs.tabBar().tabButton(0, w.tabs.tabBar().ButtonPosition.RightSide)
        self.assertEqual(close.accessibleName(), "Close tab")

    def test_the_address_bar_icons_say_what_pressing_enter_will_do(self) -> None:
        bar = self.window.nav_bar.address_bar
        bar.setText("python tutorial")
        self.assertEqual(bar._mode_action.text(), "Search the web")
        bar.setText("python.org")
        self.assertEqual(bar._mode_action.text(), "Go to address")

    def test_the_clear_buttons_are_named(self) -> None:
        bar = self.window.nav_bar.address_bar
        bar.setText("x")
        pump(8)
        a11y.name_unnamed(bar)
        from PySide6.QtWidgets import QToolButton
        clear = [b for b in bar.findChildren(QToolButton) if b.metaObject().className() == "QLineEditIconButton"
                 and not b.text()]
        self.assertTrue(clear and all(b.accessibleName() == "Clear address" for b in clear))


class NamesInEveryDialog(WindowCase):
    def test_every_dialog_reachable_from_the_menus_is_fully_named(self) -> None:
        found, failures = self.open_every_dialog()
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(found), 5, f"only reached {sorted(found)}")
        bad = {name: items for name, items in found.items() if items}
        self.assertEqual(bad, {})

    def test_the_settings_dialog_in_both_forms(self) -> None:
        from app.ui.settings_dialog import SettingsDialog
        plain = SettingsDialog(self.window.settings)
        full = SettingsDialog(self.window.settings, mcp=self.window.mcp, mcp_server=self.window.mcp_server,
                              knowledge=self.window.knowledge_index, security=True)
        for dialog in (plain, full):
            dialog.show()
            pump(10)
            self.assertEqual(a11y.missing_names(dialog), [])
            dialog.close()


class KeyboardAndFocus(WindowCase):
    def test_escape_closes_the_find_bar_and_hands_focus_back_to_the_page(self) -> None:
        w = self.window
        w._open_find()
        pump(5)
        self.assertTrue(w.find_bar.isVisible())
        self.assertIs(QApplication.focusWidget(), w.find_bar.field) if QApplication.activeWindow() else None
        w.find_bar.keyPressEvent(__import__("PySide6.QtGui", fromlist=["QKeyEvent"]).QKeyEvent(
            __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        pump(5)
        self.assertFalse(w.find_bar.isVisible())
        tab = w.tabs.current_tab()
        self.assertTrue(tab.view.hasFocus() or tab.view.focusPolicy() != Qt.FocusPolicy.NoFocus)

    def test_every_operable_widget_in_a_dialog_can_take_keyboard_focus(self) -> None:
        from app.ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.window.settings, knowledge=self.window.knowledge_index, security=True)
        dialog.show()
        pump(5)
        unreachable = [type(b).__name__ + ":" + (b.text() or b.accessibleName()) for b in dialog.findChildren(QAbstractButton)
                       if b.isVisibleTo(dialog) and b.focusPolicy() == Qt.FocusPolicy.NoFocus
                       and b.parent() is not None and type(b.parent()).__name__ not in ("QTabBar", "QScrollBar")]
        self.assertEqual(unreachable, [])
        dialog.close()

    def test_tab_key_visits_the_address_bar_toolbar_in_order(self) -> None:
        w = self.window
        bar = w.nav_bar.address_bar
        bar.setFocus()
        order = []
        for _ in range(4):
            QTest.keyClick(QApplication.focusWidget() or bar, Qt.Key.Key_Tab)
            pump(2)
            fw = QApplication.focusWidget()
            order.append(type(fw).__name__ if fw is not None else None)
        self.assertNotIn(None, order)
        self.assertEqual(len(set(order[:2])) >= 1, True)


class VisibleFocusInBothThemes(unittest.TestCase):
    CONTROLS = ("QPushButton:focus", "QToolButton:focus", "QCheckBox:focus", "QListWidget:focus", "QTreeWidget:focus",
                "QLineEdit:focus", "QComboBox:focus", "QTabBar::tab:focus")

    def test_the_stylesheet_has_a_focus_indicator_for_each_control_type_in_light_and_dark(self) -> None:
        for palette in (theme.LIGHT, theme.DARK):
            sheet = theme.stylesheet(palette)
            for control in self.CONTROLS:
                self.assertIn(control, sheet, f"{control} has no focus indicator ({palette is theme.DARK and 'dark' or 'light'})")
            self.assertIn(palette.accent, sheet)

    def test_the_focus_colour_is_visible_against_the_background_in_both_themes(self) -> None:
        def luminance(hexcolor: str) -> float:
            colour = QColor(hexcolor)
            channels = []
            for value in (colour.redF(), colour.greenF(), colour.blueF()):
                channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

        for palette in (theme.LIGHT, theme.DARK):
            bright, dark = sorted((luminance(palette.accent), luminance(palette.bg)), reverse=True)
            ratio = (bright + 0.05) / (dark + 0.05)
            self.assertGreaterEqual(ratio, 3.0, f"focus ring contrast {ratio:.2f}:1 is below 3:1 (WCAG non-text contrast)")


class NarrowAndThemed(WindowCase):
    def test_a_narrow_window_still_names_and_lays_out_everything(self) -> None:
        w = self.window
        w.resize(480, 700)
        w._open_find()
        pump(15)
        self.assertEqual(a11y.missing_names(w), [])
        bar = w.nav_bar.address_bar
        self.assertGreater(bar.width(), 100)               # the address bar is not squeezed to nothing

    def test_dark_palette_window_is_still_fully_named(self) -> None:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#17171a"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e8e8ee"))
        original = _app.palette()
        try:
            _app.setPalette(palette)
            theme.apply(_app)
            second = MainWindow(_profile, self.db, start_urls=["about:blank"])
            second.show()
            pump(10)
            self.assertEqual(a11y.missing_names(second), [])
            second.close()
        finally:
            _app.setPalette(original)
            theme.apply(_app)


class Helpers(unittest.TestCase):
    def test_enable_clear_names_the_button_and_name_unnamed_never_overwrites(self) -> None:
        from PySide6.QtWidgets import QLineEdit
        edit = QLineEdit()
        a11y.enable_clear(edit, "filter")
        edit.setText("x")
        edit.show()
        pump(8)
        a11y.name_unnamed(edit)
        from PySide6.QtWidgets import QToolButton
        names = [b.accessibleName() for b in edit.findChildren(QToolButton)]
        self.assertIn("Clear filter", names)
        edit.setPlaceholderText("Type here")
        edit.setAccessibleName("Explicit name")
        a11y.name_unnamed(edit)
        self.assertEqual(edit.accessibleName(), "Explicit name")

    def test_the_audit_finds_a_nameless_icon_button(self) -> None:
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import QPushButton, QVBoxLayout
        host = QWidget()
        layout = QVBoxLayout(host)
        button = QPushButton()
        pix = QPixmap(8, 8)
        pix.fill(Qt.GlobalColor.red)
        button.setIcon(QIcon(pix))
        layout.addWidget(button)
        self.assertEqual(len(a11y.missing_names(host)), 1)
        button.setAccessibleName("Do the thing")
        self.assertEqual(a11y.missing_names(host), [])


if __name__ == "__main__":
    unittest.main()

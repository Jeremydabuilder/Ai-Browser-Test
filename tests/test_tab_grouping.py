"""AI tab grouping: the suggestion engine (pure logic, no Qt) and the
TabManager group model it feeds into.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_tab_grouping -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-grouping-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.tab_grouping import (  # noqa: E402
    TabMeta,
    suggest_groups,
    suggest_groups_by_domain,
    suggest_groups_with_ai,
)
from app.browser.tab_manager import TabManager  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database, SettingsStore  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(times: int = 5) -> None:
    for _ in range(times):
        _app.processEvents()


def navigate_and_wait(tab, url: str, timeout_ms: int = 4000) -> None:
    """navigate() and wait for that exact load to actually finish - url()
    alone is not reliable to poll here, since it can briefly reflect a
    requested navigation that has not committed yet and revert if
    something else runs the event loop in between (observed with data:
    URLs in this headless environment)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    done = []
    tab.load_finished.connect(done.append)
    tab.navigate(url)
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not done and not expired[0]:
        _app.processEvents()
        QTest.qWait(1)
    timer.stop()
    tab.load_finished.disconnect(done.append)


# ---------------------------------------------------------------------------
# The suggestion engine - pure logic, no Qt needed
# ---------------------------------------------------------------------------


class DomainHeuristicTests(unittest.TestCase):
    def test_tabs_sharing_a_domain_are_grouped(self) -> None:
        tabs = [
            TabMeta(0, "Docs", "https://docs.example.com/a"),
            TabMeta(1, "More docs", "https://docs.example.com/b"),
            TabMeta(2, "Unrelated", "https://other.example/"),
        ]
        groups = suggest_groups_by_domain(tabs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].tab_indices), {0, 1})

    def test_a_lone_tab_on_a_domain_is_not_grouped(self) -> None:
        tabs = [TabMeta(0, "Solo", "https://solo.example/")]
        self.assertEqual(suggest_groups_by_domain(tabs), [])

    def test_www_prefix_does_not_split_a_domain_in_two(self) -> None:
        tabs = [
            TabMeta(0, "A", "https://www.example.com/a"),
            TabMeta(1, "B", "https://example.com/b"),
        ]
        groups = suggest_groups_by_domain(tabs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].tab_indices), {0, 1})

    def test_internal_pages_with_no_domain_are_never_grouped(self) -> None:
        tabs = [TabMeta(0, "New Tab", "pybrowser://newtab/"),
               TabMeta(1, "New Tab", "pybrowser://newtab/")]
        self.assertEqual(suggest_groups_by_domain(tabs), [])


class AISuggestionParsingTests(unittest.TestCase):
    """suggest_groups_with_ai must fail safe on anything it cannot fully
    trust - a malformed reply produces no groups at all, never a partial
    or guessed one."""

    class _FakeTransport:
        def __init__(self, text: str = "", raise_error: bool = False) -> None:
            self._text = text
            self._raise = raise_error

        def send(self, *, system, messages, tools):
            if self._raise:
                raise RuntimeError("network exploded")
            return _FakeResponse(self._text)

    def test_a_well_formed_reply_is_used(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/"), TabMeta(1, "B", "https://b.example/"),
               TabMeta(2, "C", "https://c.example/")]
        transport = self._FakeTransport(
            '[{"name": "Research", "tab_indices": [0, 1]}]')
        result = suggest_groups_with_ai(transport, tabs)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Research")
        self.assertEqual(result[0].tab_indices, (0, 1))

    def test_no_transport_returns_none(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/")]
        self.assertIsNone(suggest_groups_with_ai(None, tabs))

    def test_a_transport_error_returns_none(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/")]
        transport = self._FakeTransport(raise_error=True)
        self.assertIsNone(suggest_groups_with_ai(transport, tabs))

    def test_non_json_text_returns_none(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/")]
        transport = self._FakeTransport("Sure, here are some groups: nothing structured.")
        self.assertIsNone(suggest_groups_with_ai(transport, tabs))

    def test_an_index_outside_the_given_tabs_is_dropped_not_trusted(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/"), TabMeta(1, "B", "https://b.example/")]
        transport = self._FakeTransport(
            '[{"name": "X", "tab_indices": [0, 1, 99]}]')
        result = suggest_groups_with_ai(transport, tabs)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].tab_indices, (0, 1))

    def test_a_group_left_with_fewer_than_two_valid_tabs_is_dropped(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/")]
        transport = self._FakeTransport(
            '[{"name": "X", "tab_indices": [0, 99]}]')
        self.assertIsNone(suggest_groups_with_ai(transport, tabs))

    def test_a_non_list_top_level_value_returns_none(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/")]
        transport = self._FakeTransport('{"name": "not a list"}')
        self.assertIsNone(suggest_groups_with_ai(transport, tabs))

    def test_an_entry_missing_a_name_is_skipped(self) -> None:
        tabs = [TabMeta(0, "A", "https://a.example/"), TabMeta(1, "B", "https://b.example/")]
        transport = self._FakeTransport('[{"tab_indices": [0, 1]}]')
        self.assertIsNone(suggest_groups_with_ai(transport, tabs))

    def test_the_model_is_never_asked_to_touch_the_browser(self) -> None:
        """The prompt sent must carry no tools - this is metadata
        classification, never a tool-calling turn."""
        captured = {}

        class _Capturing:
            def send(self, *, system, messages, tools):
                captured["tools"] = tools
                return _FakeResponse('[{"name": "X", "tab_indices": [0, 1]}]')

        tabs = [TabMeta(0, "A", "https://a.example/"), TabMeta(1, "B", "https://b.example/")]
        suggest_groups_with_ai(_Capturing(), tabs)
        self.assertEqual(captured["tools"], [])


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class SuggestGroupsFallbackTests(unittest.TestCase):
    def test_falls_back_to_domain_heuristic_with_no_transport(self) -> None:
        tabs = [TabMeta(0, "A", "https://x.example/a"), TabMeta(1, "B", "https://x.example/b")]
        result = suggest_groups(tabs, transport=None)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].tab_indices), {0, 1})

    def test_falls_back_when_ai_output_is_malformed(self) -> None:
        class _Bad:
            def send(self, *, system, messages, tools):
                return _FakeResponse("not json at all")

        tabs = [TabMeta(0, "A", "https://x.example/a"), TabMeta(1, "B", "https://x.example/b")]
        result = suggest_groups(tabs, transport=_Bad())
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].tab_indices), {0, 1})


# ---------------------------------------------------------------------------
# TabManager's group model
# ---------------------------------------------------------------------------


class TabManagerGroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        for _ in range(3):
            self.tabs.new_tab("about:blank")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def test_a_fresh_tab_belongs_to_no_group(self) -> None:
        self.assertIsNone(self.tabs.group_of(0))

    def test_creating_a_group_returns_an_id(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.assertTrue(group_id)

    def test_moving_a_tab_into_a_group_sets_its_membership(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        self.assertEqual(self.tabs.group_of(0), group_id)

    def test_groups_lists_member_indices(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        self.tabs.move_tab_to_group(2, group_id)
        groups = self.tabs.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["tab_indices"], [0, 2])

    def test_renaming_a_group(self) -> None:
        group_id = self.tabs.create_group("Old Name")
        self.assertTrue(self.tabs.rename_group(group_id, "New Name"))
        self.assertEqual(self.tabs.groups()[0]["name"], "New Name")

    def test_renaming_an_unknown_group_fails_safely(self) -> None:
        self.assertFalse(self.tabs.rename_group("no-such-group", "X"))

    def test_collapsing_a_group(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.assertTrue(self.tabs.set_group_collapsed(group_id, True))
        self.assertTrue(self.tabs.groups()[0]["collapsed"])
        self.tabs.set_group_collapsed(group_id, False)
        self.assertFalse(self.tabs.groups()[0]["collapsed"])

    def test_moving_a_tab_to_a_different_group_leaves_the_old_one(self) -> None:
        g1 = self.tabs.create_group("A")
        g2 = self.tabs.create_group("B")
        self.tabs.move_tab_to_group(0, g1)
        self.tabs.move_tab_to_group(0, g2)
        self.assertEqual(self.tabs.group_of(0), g2)
        self.assertEqual(self.tabs.groups()[0]["tab_indices"], [])  # g1 now empty

    def test_removing_a_tab_from_its_group(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        self.tabs.move_tab_to_group(0, None)
        self.assertIsNone(self.tabs.group_of(0))

    def test_ungrouping_deletes_the_group_but_not_its_tabs(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        before = self.tabs.count()
        self.assertTrue(self.tabs.remove_group(group_id))
        self.assertEqual(self.tabs.count(), before)
        self.assertIsNone(self.tabs.group_of(0))
        self.assertEqual(self.tabs.groups(), [])

    def test_ungrouped_indices_excludes_grouped_tabs(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(1, group_id)
        self.assertEqual(self.tabs.ungrouped_indices(), [0, 2])

    def test_closing_a_grouped_tab_just_removes_it_no_cleanup_needed(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        self.tabs.close_tab(0)
        # The remaining tab that was never grouped stays ungrouped; no
        # crash, no stale reference to the closed tab.
        self.assertEqual(self.tabs.groups()[0]["tab_indices"], [])

    def test_a_tab_can_be_pinned_and_grouped_at_the_same_time(self) -> None:
        group_id = self.tabs.create_group("Research")
        self.tabs.move_tab_to_group(0, group_id)
        widget = self.tabs.widget(0)
        self.tabs.set_pinned(self.tabs.indexOf(widget), True)
        index = self.tabs.indexOf(widget)
        self.assertTrue(self.tabs.is_pinned(index))
        self.assertEqual(self.tabs.group_of(index), group_id)


class GroupSettingsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_defaults_to_empty(self) -> None:
        self.assertEqual(self.settings.tab_groups, [])

    def test_round_trips(self) -> None:
        groups = [{"id": "g1", "name": "Research", "collapsed": False,
                  "urls": ["https://a.example/"]}]
        self.settings.tab_groups = groups
        self.assertEqual(self.settings.tab_groups, groups)

    def test_persists_across_reopening(self) -> None:
        path = self.db.path
        self.settings.tab_groups = [{"id": "g1", "name": "R", "collapsed": True, "urls": []}]
        self.db.close()
        reopened = Database(path)
        restored = SettingsStore(reopened).tab_groups
        self.assertEqual(restored[0]["name"], "R")
        self.assertTrue(restored[0]["collapsed"])
        reopened.close()

    def test_corrupted_value_degrades_to_empty(self) -> None:
        self.settings.set("tab_groups", "not json")
        self.assertEqual(self.settings.tab_groups, [])

    def test_malformed_entries_are_dropped_not_raised(self) -> None:
        import json
        self.settings.set("tab_groups", json.dumps([
            {"id": "g1", "name": "Good", "urls": []},
            {"id": "g2"},           # missing name/urls
            "not even a dict",
        ]))
        groups = self.settings.tab_groups
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "Good")


class MainWindowGroupIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        os.environ["PYBROWSER_DATA_DIR"] = self._dir.name
        self.db = Database(database_path())
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        if getattr(self, "window", None) is not None:
            self.window.close()
            self.window.deleteLater()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def _open(self):
        from app.ui.main_window import MainWindow

        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        pump()
        return self.window

    def test_creating_a_group_persists_it(self) -> None:
        window = self._open()
        navigate_and_wait(window.tabs.widget(0), "data:text/html,PageA")
        window.tabs.new_tab("data:text/html,PageB")
        pump()
        group_id = window.tabs.create_group("Research")
        window.tabs.move_tab_to_group(0, group_id)
        window.tabs.move_tab_to_group(1, group_id)
        pump()
        saved = self.settings.tab_groups
        self.assertEqual(len(saved), 1)
        self.assertEqual(sorted(saved[0]["urls"]),
                         sorted(["data:text/html,PageA", "data:text/html,PageB"]))

    def test_groups_survive_a_restart(self) -> None:
        window = self._open()
        navigate_and_wait(window.tabs.widget(0), "data:text/html,PageA")
        window.tabs.new_tab("data:text/html,PageB")
        pump()
        group_id = window.tabs.create_group("Research")
        window.tabs.move_tab_to_group(0, group_id)
        window.tabs.move_tab_to_group(1, group_id)
        # Groups reattach to tabs that are actually open next time - since
        # PyBrowser does not restore a general browsing session, that means
        # pinning the grouped tabs too, the same as any other tab that
        # needs to still exist on restart.
        window.tabs.set_pinned(0, True)
        window.tabs.set_pinned(1, True)
        pump()
        window.close()
        pump(3)
        self.window = None

        from app.ui.main_window import MainWindow
        window2 = MainWindow(_profile, self.db, start_urls=["about:blank"])
        pump()
        self.window = window2
        groups = window2.tabs.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "Research")
        self.assertEqual(len(groups[0]["tab_indices"]), 2)

    def test_suggest_groups_dialog_rejecting_leaves_tabs_ungrouped(self) -> None:
        from PySide6.QtWidgets import QDialog

        window = self._open()
        window.tabs.new_tab("http://127.0.0.1:1/a")
        window.tabs.new_tab("data:text/html,PageB")
        pump()

        from unittest import mock
        with mock.patch("app.ui.tab_grouping_dialog.SuggestGroupsDialog.exec",
                        return_value=QDialog.DialogCode.Rejected):
            window._suggest_tab_groups()
        self.assertEqual(window.tabs.groups(), [])


if __name__ == "__main__":
    unittest.main()

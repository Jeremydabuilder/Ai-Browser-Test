"""The Mission Library: the page, its search, deletion, and the action channel.

A Mission is meant to be a durable object, not a saved tab group, and the
Library is where that claim gets tested: it has to be findable months later,
survive a restart, and not be destroyed by a stray click.

The page runs in real Chromium here, because the things worth checking - that a
finding named `<img onerror=...>` is displayed rather than executed, that an
action URL never renders as a page - are not properties of the Python that
builds the HTML.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_library -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-library-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.internal import SCHEME, parse_action  # noqa: E402
from app.browser.missions_page import (  # noqa: E402
    LIBRARY_URL,
    LibraryData,
    mission_url,
    render,
    summarise,
)
from app.missions import MissionService, MissionStore  # noqa: E402
from app.missions.bus import bus  # noqa: E402
from app.missions.model import MissionStatus  # noqa: E402
from app.storage.database import SCHEMA_VERSION, Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def tearDownModule() -> None:
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def _database() -> tuple[Database, str]:
    path = os.path.join(tempfile.mkdtemp(prefix="library-"), "browser.sqlite3")
    return Database(path), path


class _Window:
    """A real window with a Mission or three."""

    def __init__(self) -> None:
        self.db, self.path = _database()
        self.window = MainWindow(_profile, self.db, ["about:blank"])
        self.service = self.window.missions

    def seed(self):
        shoes = self.service.start("Find the best tennis shoes under $140")
        page = self.service.store.add_page(
            shoes.id, "https://www.tennis-warehouse.com/x", "Tennis Warehouse")
        self.service.store.add_finding(
            shoes.id, "ASICS Solution Speed has stronger lateral support", page.id)
        self.service.pause()
        trip = self.service.start("plan a trip to Japan in April")
        self.service.complete()
        return shoes, trip

    def text_of(self, tab, timeout_ms: int = 4000) -> str:
        out: list[str] = []
        tab.page.toPlainText(lambda value: out.append(value))
        for _ in range(timeout_ms // 50):
            if out:
                break
            QTest.qWait(50)
        return out[0] if out else ""

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(10)
        self.db.close()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class PageSafetyTests(unittest.TestCase):
    """This is a privileged page rendering text written by web pages."""

    def _payload(self, html: str) -> str:
        return html.split('<script id="data" type="application/json">')[1].split("</script>")[0]

    def test_a_finding_cannot_close_the_data_block(self) -> None:
        data = LibraryData(detail={
            "id": 1, "title": "M", "goal": "g", "status": "active", "updated": "",
            "findings": 1, "pages": 0,
            "findingList": [{"id": 1, "text": "</script><img src=x onerror=alert(1)>",
                             "source": "evil.example", "url": "https://evil.example/"}],
            "pageList": [],
        })
        payload = self._payload(render(data, dark=False))
        self.assertNotIn("</script>", payload)
        self.assertNotIn("<img", payload)
        # Still valid JSON, and the text is still all there.
        parsed = json.loads(payload.replace("\\u003c", "<"))
        self.assertIn("onerror", parsed["detail"]["findingList"][0]["text"])

    def test_a_mission_title_cannot_inject_markup(self) -> None:
        data = LibraryData(missions=[{
            "id": 1, "title": "</script><b>x</b>", "goal": "g", "status": "active",
            "updated": "", "findings": 0, "pages": 0}], total=1)
        payload = self._payload(render(data, dark=False))
        self.assertNotIn("<b>", payload)

    def test_the_page_writes_with_text_content_only(self) -> None:
        # The one rule that makes the escaping above sufficient.
        html = render(LibraryData(), dark=False)
        # An assignment, not the word: the file explains in prose why it never
        # uses innerHTML, and a test that forbids saying so is a test that
        # punishes the comment.
        self.assertNotIn(".innerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)
        self.assertIn("textContent", html)

    def test_it_renders_in_both_themes_without_a_network_request(self) -> None:
        for dark in (True, False):
            html = render(LibraryData(), dark=dark)
            self.assertIn('data-theme="dark"' if dark else 'data-theme="light"', html)
            for scheme in ("http://", "https://"):
                self.assertNotIn(scheme, html.split("<script")[0])


class ActionUrlTests(unittest.TestCase):
    """The only channel from the page to the browser."""

    def test_a_page_on_the_open_web_cannot_mint_an_action(self) -> None:
        # The entire boundary is the scheme check. This is its test.
        for url in ("https://evil.example/action/delete?id=1",
                    "http://localhost/action/delete?id=1",
                    "file:///action/delete?id=1"):
            self.assertIsNone(parse_action(QUrl(url)), url)

    def test_actions_are_namespaced_by_page(self) -> None:
        name, params = parse_action(QUrl("pybrowser://missions/action/delete?id=7"))
        self.assertEqual(name, "missions:delete")
        self.assertEqual(params, {"id": "7"})

    def test_the_new_tab_pages_actions_keep_their_names(self) -> None:
        # They shipped before there was a second page; renaming them would
        # break action URLs already sitting in users' history.
        name, _ = parse_action(QUrl("pybrowser://newtab/action/history"))
        self.assertEqual(name, "history")

    def test_a_plain_page_url_is_not_an_action(self) -> None:
        self.assertIsNone(parse_action(QUrl(LIBRARY_URL)))
        self.assertIsNone(parse_action(QUrl(mission_url(7))))

    def test_the_scheme_is_ours(self) -> None:
        self.assertTrue(LIBRARY_URL.startswith(f"{SCHEME}://"))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.store = MissionStore(self.db)
        self.shoes = self.store.create("Tennis Shoes", "find shoes for hard courts")
        page = self.store.add_page(self.shoes.id,
                                   "https://www.tennis-warehouse.com/x", "Tennis Warehouse")
        self.store.add_finding(self.shoes.id, "ASICS has stronger lateral support", page.id)
        self.trip = self.store.create("Japan Trip", "plan a trip in April")
        self.store.add_page(self.trip.id, "https://www.jal.co.jp/x", "JAL Booking")

    def tearDown(self) -> None:
        self.db.close()

    def test_it_matches_a_title(self) -> None:
        self.assertEqual([m.id for m in self.store.search("tennis")], [self.shoes.id])

    def test_it_matches_a_goal(self) -> None:
        self.assertEqual([m.id for m in self.store.search("hard courts")], [self.shoes.id])

    def test_it_matches_a_finding(self) -> None:
        self.assertEqual([m.id for m in self.store.search("lateral")], [self.shoes.id])

    def test_it_matches_a_page_title_and_url(self) -> None:
        self.assertEqual([m.id for m in self.store.search("JAL")], [self.trip.id])
        self.assertEqual([m.id for m in self.store.search("jal.co.jp")], [self.trip.id])

    def test_a_title_match_outranks_a_content_match(self) -> None:
        self.store.add_finding(self.trip.id, "Tennis is popular in Japan")
        self.assertEqual([m.id for m in self.store.search("tennis")],
                         [self.shoes.id, self.trip.id])

    def test_it_is_case_insensitive_and_ignores_stray_spacing(self) -> None:
        self.assertEqual([m.id for m in self.store.search("  TENNIS  ")], [self.shoes.id])

    def test_an_empty_query_is_the_whole_library(self) -> None:
        self.assertEqual(len(self.store.search("   ")), 2)

    def test_no_match_is_empty_not_everything(self) -> None:
        self.assertEqual(self.store.search("zebra"), [])

    def test_a_deleted_mission_is_not_findable(self) -> None:
        self.store.soft_delete(self.shoes.id)
        self.assertEqual(self.store.search("tennis"), [])
        self.assertEqual(self.store.search("lateral"), [])


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


class DeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.path = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("find shoes")
        self.service.save_finding("ASICS has better support")

    def tearDown(self) -> None:
        self.db.close()

    def test_delete_hides_the_mission_but_keeps_the_record(self) -> None:
        # A Mission is the reasoning behind a decision, and "why did we rule
        # that out?" gets asked months later. Dropping the rows answers it
        # with silence.
        self.assertTrue(self.service.delete(self.mission.id))
        self.assertIsNone(self.service.store.get(self.mission.id))
        self.assertEqual(self.service.recent(), [])
        rows = self.db.query("SELECT id FROM missions WHERE id = ?", (self.mission.id,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.db.query(
            "SELECT id FROM mission_findings WHERE mission_id = ?", (self.mission.id,))), 1)

    def test_a_deleted_mission_can_be_restored_intact(self) -> None:
        self.service.delete(self.mission.id)
        self.assertTrue(self.service.restore(self.mission.id))
        restored = self.service.store.get(self.mission.id)
        self.assertEqual(restored.title, "Shoes")
        self.assertEqual(len(restored.findings), 1)

    def test_deleting_the_active_mission_lets_go_of_it(self) -> None:
        self.assertIsNotNone(self.service.active)
        self.service.delete(self.mission.id)
        self.assertIsNone(self.service.active)
        self.assertEqual(self.service.briefing(), "")

    def test_permanent_delete_really_is_permanent(self) -> None:
        self.service.delete(self.mission.id, permanent=True)
        self.assertEqual(self.db.query("SELECT id FROM missions"), [])
        self.assertEqual(self.db.query("SELECT id FROM mission_findings"), [])

    def test_deletion_survives_a_restart(self) -> None:
        self.service.delete(self.mission.id)
        self.db.close()
        reopened = Database(self.path)
        try:
            self.assertEqual(MissionStore(reopened).recent(), [])
            self.assertTrue(MissionStore(reopened).is_deleted(self.mission.id))
        finally:
            reopened.close()

    def test_a_deleted_mission_cannot_be_resumed(self) -> None:
        self.service.delete(self.mission.id)
        self.assertIsNone(self.service.resume(self.mission.id))
        self.assertIsNone(self.service.active)


class MigrationTests(unittest.TestCase):
    def test_a_v3_profile_gains_soft_delete_and_keeps_its_missions(self) -> None:
        import sqlite3

        db, path = _database()
        store = MissionStore(db)
        mission = store.create("Tennis Shoes", "find shoes")
        store.add_finding(mission.id, "A fact")
        db.close()

        # Wind the file back to v3, as an existing profile would be.
        conn = sqlite3.connect(path)
        for table in ("decision_assumptions", "challenge_points",
                      "mission_challenges", "routine_steps", "routines"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DROP INDEX IF EXISTS idx_finding_ref")
        conn.execute("ALTER TABLE mission_findings DROP COLUMN ref")
        conn.execute("ALTER TABLE missions DROP COLUMN next_ref")
        conn.execute("ALTER TABLE missions DROP COLUMN parent_id")
        conn.execute("ALTER TABLE missions DROP COLUMN branch_name")
        conn.execute("ALTER TABLE missions DROP COLUMN deleted_at")
        conn.execute("ALTER TABLE missions DROP COLUMN progress")
        conn.execute("ALTER TABLE missions DROP COLUMN result")
        conn.execute("ALTER TABLE missions DROP COLUMN follow_ups")
        conn.execute("DROP TABLE IF EXISTS mission_actions")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
        conn.close()

        upgraded = Database(path)
        try:
            self.assertEqual(upgraded.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
            store = MissionStore(upgraded)
            self.assertEqual([m.title for m in store.recent()], ["Tennis Shoes"])
            self.assertEqual(len(store.findings(mission.id)), 1)
            self.assertTrue(store.soft_delete(mission.id))
        finally:
            upgraded.close()


# ---------------------------------------------------------------------------
# Multiple windows
# ---------------------------------------------------------------------------


class MultiWindowTests(unittest.TestCase):
    """Two windows, one database. They must not disagree about what exists."""

    def setUp(self) -> None:
        self.db, _ = _database()
        self.first = MissionService(MissionStore(self.db))
        self.second = MissionService(MissionStore(self.db))
        self.mission = self.first.start("find shoes")

    def tearDown(self) -> None:
        self.db.close()

    def test_deleting_in_one_window_releases_it_in_the_other(self) -> None:
        self.second.resume(self.mission.id)
        self.assertIsNotNone(self.second.active)
        self.first.delete(self.mission.id)
        self.assertIsNone(self.second.active)

    def test_renaming_in_one_window_is_seen_by_the_other(self) -> None:
        self.second.resume(self.mission.id)
        self.first.rename(self.mission.id, "Shoe Hunt")
        self.assertEqual(self.second.active.title, "Shoe Hunt")

    def test_a_window_holding_a_different_mission_is_left_alone(self) -> None:
        other = self.first.start("plan a trip")
        self.second.resume(self.mission.id)
        self.first.delete(other.id)
        self.assertIsNotNone(self.second.active)
        self.assertEqual(self.second.active.id, self.mission.id)

    def test_the_bus_is_one_object_for_the_whole_process(self) -> None:
        self.assertIs(bus(), bus())


# ---------------------------------------------------------------------------
# The library in a real window
# ---------------------------------------------------------------------------


class LibraryInTheBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Window()
        self.shoes, self.trip = self.harness.seed()
        self.window = self.harness.window
        self.service = self.harness.service

    def tearDown(self) -> None:
        self.harness.close()

    def _open_library(self):
        self.window._show_mission_library()
        QTest.qWait(2200)
        return self.window.tabs.current_tab()

    def test_the_library_opens_as_a_real_page_and_lists_the_missions(self) -> None:
        tab = self._open_library()
        self.assertEqual(tab.url().toString(), LIBRARY_URL)
        text = self.harness.text_of(tab)
        self.assertIn("Tennis Shoes", text)
        self.assertIn("Trip to Japan", text)
        self.assertIn("1 finding", text)

    def test_its_tab_is_named_after_the_page_not_called_new_tab(self) -> None:
        self._open_library()
        self.assertEqual(self.window.tabs.tabText(self.window.tabs.currentIndex()),
                         "Missions")

    def test_its_address_is_shown_because_it_is_somewhere_you_went(self) -> None:
        from app.utils import urls as url_utils

        self.assertEqual(url_utils.display_text(QUrl(LIBRARY_URL)), LIBRARY_URL)
        self.assertEqual(url_utils.display_text(QUrl("pybrowser://newtab/")), "")

    def test_a_mission_detail_page_shows_its_findings_and_pages(self) -> None:
        self.window._open_mission(self.shoes.id)
        QTest.qWait(2200)
        text = self.harness.text_of(self.window.tabs.current_tab())
        self.assertIn("lateral support", text)
        self.assertIn("tennis-warehouse.com", text)

    def test_searching_narrows_the_list(self) -> None:
        tab = self._open_library()
        tab.navigate(LIBRARY_URL + "?q=lateral")
        QTest.qWait(2200)
        text = self.harness.text_of(tab)
        self.assertIn("Tennis Shoes", text)
        self.assertNotIn("Trip to Japan", text)

    def test_an_action_url_never_renders_as_a_page(self) -> None:
        # Deliberately an action the window does not implement: a real one
        # would do its job, and `delete` would sit on a modal confirmation
        # forever. What is being tested is that the navigation itself is
        # refused - the URL bar does not move and no page is drawn.
        tab = self._open_library()
        before = tab.url().toString()
        tab.navigate("pybrowser://missions/action/nosuchaction")
        QTest.qWait(800)
        self.assertEqual(tab.url().toString(), before)
        self.assertIn("Tennis Shoes", self.harness.text_of(tab))

    def test_open_looks_at_a_mission_without_activating_it(self) -> None:
        # Browsing your own library must not hijack Py's context.
        self.assertIsNone(self.service.active)
        self.window._on_mission_action("open", {"id": str(self.shoes.id)})
        QTest.qWait(1200)
        self.assertIsNone(self.service.active)
        self.assertEqual(self.service.store.get(self.shoes.id).status,
                         MissionStatus.PAUSED)

    def test_resume_activates_it_and_opens_py(self) -> None:
        self.window._resume_mission(self.shoes.id)
        QTest.qWait(200)
        self.assertEqual(self.service.active.id, self.shoes.id)
        self.assertEqual(self.service.active.status, MissionStatus.ACTIVE)
        self.assertIn("lateral support", self.service.briefing())

    def test_resuming_a_completed_mission_reopens_it(self) -> None:
        self.window._resume_mission(self.trip.id)
        QTest.qWait(200)
        self.assertEqual(self.service.active.id, self.trip.id)

    def test_an_unknown_mission_id_is_ignored_rather_than_crashing(self) -> None:
        self.window._on_mission_action("open", {"id": "99999"})
        self.window._on_mission_action("resume", {"id": "99999"})
        self.window._on_mission_action("open", {"id": "not-a-number"})
        QTest.qWait(600)
        self.assertIsNone(self.service.active)

    def test_the_panel_offers_a_way_into_the_library(self) -> None:
        from app.ui.agent_panel import AgentPanel

        panel = AgentPanel(None, self.window, self.service)
        self.window.set_side_panel(panel)
        QTest.qWait(10)
        self.assertFalse(panel.mission_picker.all_button.isHidden())
        panel.mission_picker.all_button.click()
        QTest.qWait(1200)
        self.assertEqual(self.window.tabs.current_tab().url().toString(), LIBRARY_URL)

    def test_the_new_tab_page_is_still_labelled_new_tab(self) -> None:
        # The tab label used to be forced for every pybrowser:// URL. Narrowing
        # that to the new-tab page is what let the library keep its own name -
        # and this is the behaviour that narrowing could have broken.
        from app.browser.newtab import NEW_TAB_URL

        tab = self.window.tabs.new_tab(NEW_TAB_URL)
        QTest.qWait(1500)
        self.assertEqual(self.window.tabs.tabText(self.window.tabs.indexOf(tab)),
                         "New Tab")

    def test_normal_browsing_is_untouched(self) -> None:
        tab = self.window.tabs.new_tab("about:blank")
        QTest.qWait(400)
        self.assertEqual(tab.url().toString(), "about:blank")
        self.assertEqual(self.service.store.count(), 2)     # nothing recorded


class SummaryTests(unittest.TestCase):
    def test_counts_are_passed_in_for_the_list_view(self) -> None:
        # Missions are read without their contents there, so len() would
        # quietly report zero for every row.
        from app.missions.model import Mission

        mission = Mission(id=1, title="M", goal="g")
        self.assertEqual(summarise(mission, findings=3, pages=5)["findings"], 3)
        self.assertEqual(summarise(mission)["findings"], 0)


if __name__ == "__main__":
    unittest.main()

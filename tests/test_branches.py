"""Branch the Internet: forking a Mission into an independent copy.

The property that matters is independence: nothing done in one branch may
reach into another, or into the parent, after the fork. Branching copies rows
rather than sharing them, the same historical-accuracy pattern as decision
evidence and challenge snapshots.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_branches -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-branches-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.missions_page import LibraryData, render, summarise  # noqa: E402
from app.missions import MissionService, MissionStore  # noqa: E402
from app.missions.model import MissionStatus  # noqa: E402
from app.storage.database import Database  # noqa: E402
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
    path = os.path.join(tempfile.mkdtemp(prefix="branches-"), "browser.sqlite3")
    return Database(path), path


class _Board:
    def __init__(self) -> None:
        self.db, self.path = _database()
        self.store = MissionStore(self.db)
        self.mission = self.store.create("Plan the trip", "choose the best option")
        self.page = self.store.add_page(self.mission.id, "https://one.example/a", "One")
        self.f1 = self.store.add_finding(self.mission.id, "option A is cheaper",
                                         self.page.id)[1]
        self.f2 = self.store.add_finding(self.mission.id, "option A is slower",
                                         self.page.id)[1]
        self.store.save_decision(
            self.mission.id, "Option A", "cheapest that still fits",
            [self.f1.id, self.f2.id], [("Option B", "faster, over budget")],
            ["budget matters more than speed"])

    def close(self) -> None:
        self.db.close()


class BranchCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board()

    def tearDown(self) -> None:
        self.board.close()

    def test_a_branch_is_a_new_mission_with_its_parent_recorded(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertNotEqual(branch.id, self.board.mission.id)
        self.assertEqual(branch.parent_id, self.board.mission.id)
        self.assertEqual(branch.branch_name, "Budget")
        self.assertTrue(branch.is_branch)
        self.assertEqual(branch.status, MissionStatus.ACTIVE)

    def test_the_goal_carries_over_and_the_title_names_the_branch(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertEqual(branch.goal, self.board.mission.goal)
        self.assertIn("Budget", branch.title)
        self.assertIn("Plan the trip", branch.title)

    def test_findings_are_copied_with_fresh_refs(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertEqual([f.text for f in branch.findings],
                         ["option A is cheaper", "option A is slower"])
        self.assertEqual([f.ref for f in branch.findings], [1, 2])
        self.assertNotEqual({f.id for f in branch.findings},
                            {self.board.f1.id, self.board.f2.id})

    def test_the_live_decision_is_copied_with_its_reasons_and_alternatives(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertEqual(branch.decision.decision, "Option A")
        self.assertEqual(branch.decision.rationale, "cheapest that still fits")
        self.assertEqual([a.name for a in branch.decision.alternatives], ["Option B"])
        self.assertEqual([a.text for a in branch.decision.assumptions],
                         ["budget matters more than speed"])

    def test_decision_evidence_cites_the_branchs_own_findings(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        cited_ids = {e.finding_id for e in branch.decision.evidence}
        branch_ids = {f.id for f in branch.findings}
        self.assertTrue(cited_ids)
        self.assertTrue(cited_ids.issubset(branch_ids))
        self.assertFalse(cited_ids & {self.board.f1.id, self.board.f2.id})

    def test_pages_are_copied_too(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertEqual([p.url for p in branch.pages], [self.board.page.url])

    def test_a_mission_with_no_decision_branches_cleanly(self) -> None:
        bare = self.board.store.create("Bare", "no decision yet")
        branch = self.board.store.branch(bare.id, "Copy")
        self.assertIsNone(branch.decision)

    def test_a_mission_with_no_findings_branches_cleanly(self) -> None:
        bare = self.board.store.create("Bare", "no findings yet")
        branch = self.board.store.branch(bare.id, "Copy")
        self.assertEqual(branch.findings, ())

    def test_branching_an_unknown_mission_fails_cleanly(self) -> None:
        self.assertIsNone(self.board.store.branch(999999, "Budget"))

    def test_a_branch_name_is_optional(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "")
        self.assertEqual(branch.title, self.board.mission.title)
        self.assertEqual(branch.branch_name, "")


class IndependenceTests(unittest.TestCase):
    """Nothing done in one branch may reach into another."""

    def setUp(self) -> None:
        self.board = _Board()
        self.branch = self.board.store.branch(self.board.mission.id, "Budget")

    def tearDown(self) -> None:
        self.board.close()

    def test_editing_a_branch_finding_does_not_touch_the_parent(self) -> None:
        self.board.store.edit_finding(self.branch.findings[0].id, "totally different")
        parent = self.board.store.get(self.board.mission.id)
        self.assertEqual(parent.findings[0].text, "option A is cheaper")

    def test_editing_the_parent_after_branching_does_not_touch_the_branch(self) -> None:
        self.board.store.edit_finding(self.board.f1.id, "changed after the fork")
        branch = self.board.store.get(self.branch.id)
        self.assertEqual(branch.findings[0].text, "option A is cheaper")

    def test_deleting_a_branch_finding_does_not_touch_the_parent(self) -> None:
        self.board.store.remove_finding(self.branch.findings[0].id)
        parent = self.board.store.get(self.board.mission.id)
        self.assertEqual(len(parent.findings), 2)

    def test_a_new_decision_in_the_branch_does_not_change_the_parent(self) -> None:
        self.board.store.save_decision(self.branch.id, "Option B", "changed my mind")
        parent = self.board.store.get(self.board.mission.id)
        self.assertEqual(parent.decision.decision, "Option A")

    def test_deleting_the_parent_does_not_delete_the_branch(self) -> None:
        self.board.store.delete(self.board.mission.id)
        survivor = self.board.store.get(self.branch.id)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor.decision.decision, "Option A")

    def test_soft_deleting_the_parent_does_not_hide_the_branch(self) -> None:
        self.board.store.soft_delete(self.board.mission.id)
        self.assertIsNotNone(self.board.store.get(self.branch.id))

    def test_challenges_are_not_carried_into_the_branch(self) -> None:
        # A challenge targets a specific finding row by id; the branch's
        # findings are new rows, so copying the old challenge would attach it
        # to the wrong claim. The branch starts unchallenged instead.
        from app.missions.model import TargetKind, Verdict

        self.board.store.save_challenge(
            self.board.mission.id, TargetKind.FINDING, self.board.f1.id,
            self.board.f1.text, Verdict.CONTRADICTED, "later evidence disagrees")
        another_branch = self.board.store.branch(self.board.mission.id, "Comfort")
        self.assertIsNone(
            self.board.store.challenge(TargetKind.FINDING, another_branch.findings[0].id))


class FamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board()

    def tearDown(self) -> None:
        self.board.close()

    def test_children_lists_every_branch(self) -> None:
        first = self.board.store.branch(self.board.mission.id, "Budget")
        second = self.board.store.branch(self.board.mission.id, "Comfort")
        children = self.board.store.children(self.board.mission.id)
        self.assertEqual({c.id for c in children}, {first.id, second.id})

    def test_a_root_mission_has_no_children_until_branched(self) -> None:
        self.assertEqual(self.board.store.children(self.board.mission.id), [])

    def test_parent_of_a_branch_is_the_mission_it_came_from(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        self.assertEqual(self.board.store.parent_of(branch.id).id, self.board.mission.id)

    def test_a_root_mission_has_no_parent(self) -> None:
        self.assertIsNone(self.board.store.parent_of(self.board.mission.id))

    def test_branches_can_themselves_be_branched(self) -> None:
        branch = self.board.store.branch(self.board.mission.id, "Budget")
        grandchild = self.board.store.branch(branch.id, "Tight budget")
        self.assertEqual(grandchild.parent_id, branch.id)
        self.assertEqual(self.board.store.parent_of(grandchild.id).id, branch.id)


class PersistenceTests(unittest.TestCase):
    def test_a_branch_survives_a_restart(self) -> None:
        board = _Board()
        branch = board.store.branch(board.mission.id, "Budget")
        mission_id, branch_id, path = board.mission.id, branch.id, board.path
        board.close()

        reopened = Database(path)
        try:
            store = MissionStore(reopened)
            restored = store.get(branch_id)
            self.assertEqual(restored.parent_id, mission_id)
            self.assertEqual(restored.decision.decision, "Option A")
            self.assertEqual(len(restored.findings), 2)
        finally:
            reopened.close()

    def test_a_v8_profile_gains_branching_columns(self) -> None:
        import sqlite3

        from app.storage.database import SCHEMA_VERSION

        board = _Board()
        path = board.path
        board.close()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE missions DROP COLUMN parent_id")
        conn.execute("ALTER TABLE missions DROP COLUMN branch_name")
        conn.execute("ALTER TABLE missions DROP COLUMN progress")
        conn.execute("ALTER TABLE missions DROP COLUMN result")
        conn.execute("ALTER TABLE missions DROP COLUMN follow_ups")
        conn.execute("DROP TABLE IF EXISTS mission_actions")
        conn.execute("PRAGMA user_version=8")
        conn.commit()
        conn.close()
        upgraded = Database(path)
        try:
            self.assertEqual(upgraded.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
            store = MissionStore(upgraded)
            branch = store.branch(board.mission.id, "Budget")
            self.assertIsNotNone(branch)
        finally:
            upgraded.close()


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("plan a trip")
        self.service.save_finding("option A is cheaper")

    def tearDown(self) -> None:
        self.db.close()

    def test_branching_does_not_change_the_active_mission(self) -> None:
        # Branching from the library must not hijack Py's context - the same
        # reasoning as "open is not resume" for the library itself.
        self.service.branch(self.mission.id, "Budget")
        self.assertEqual(self.service.active.id, self.mission.id)

    def test_a_branch_can_be_resumed_independently(self) -> None:
        branch = self.service.branch(self.mission.id, "Budget")
        self.service.pause()
        resumed = self.service.resume(branch.id)
        self.assertEqual(resumed.id, branch.id)
        self.assertIn("option A is cheaper",
                      [f.text for f in self.service.active.findings])

    def test_an_unknown_mission_cannot_be_branched(self) -> None:
        self.assertIsNone(self.service.branch(999999, "Budget"))


class PageTests(unittest.TestCase):
    def test_the_detail_payload_lists_branches_and_the_parent_link(self) -> None:
        board = _Board()
        try:
            branch = board.store.branch(board.mission.id, "Budget")
            parent_row = summarise(board.store.get(board.mission.id), with_detail=True,
                                   children=board.store.children(board.mission.id),
                                   parent=board.store.parent_of(board.mission.id))
            self.assertEqual(parent_row["parent"], None)
            self.assertEqual([b["id"] for b in parent_row["branches"]], [branch.id])

            branch_row = summarise(board.store.get(branch.id), with_detail=True,
                                   children=board.store.children(branch.id),
                                   parent=board.store.parent_of(branch.id))
            self.assertEqual(branch_row["parent"]["id"], board.mission.id)
            self.assertEqual(branch_row["branchName"], "Budget")
        finally:
            board.close()

    def test_a_root_mission_has_no_parent_and_no_branches_in_the_payload(self) -> None:
        board = _Board()
        try:
            row = summarise(board.store.get(board.mission.id), with_detail=True)
            self.assertIsNone(row["parent"])
            self.assertEqual(row["branches"], [])
        finally:
            board.close()

    def test_a_hostile_branch_name_cannot_break_out_of_the_data_block(self) -> None:
        board = _Board()
        try:
            branch = board.store.branch(
                board.mission.id, "</script><img src=x onerror=alert(1)>")
            row = summarise(board.store.get(branch.id), with_detail=True,
                            parent=board.store.parent_of(branch.id))
            data = LibraryData(detail=row)
            payload = render(data, dark=False).split(
                '<script id="data" type="application/json">')[1].split("</script>")[0]
            self.assertNotIn("</script>", payload)
            self.assertNotIn("<img", payload)
        finally:
            board.close()


if __name__ == "__main__":
    unittest.main()

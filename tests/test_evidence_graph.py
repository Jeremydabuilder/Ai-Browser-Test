"""The Evidence Graph: refs, decision status, assumptions, and the map.

Three things carry the weight here. Mission-local references have to be
unambiguous, permanent and impossible to point at another Mission. Decision
status has to be a reading of the evidence right now, never a stored value that
drifts. And the precedence between evidence states has to be explicit, because
an item can be several things at once and the UI must not depend on the order
rows came back from a query.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_evidence_graph -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-graph-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.missions_page import (  # noqa: E402
    LibraryData,
    evidence_map,
    evidence_url,
    render,
)
from app.missions import MissionService, MissionStore  # noqa: E402
from app.missions.briefing import DECISION_CLOSE, DECISION_OPEN, FINDINGS_CLOSE, FINDINGS_OPEN  # noqa: E402
from app.missions.model import (  # noqa: E402
    MAX_ASSUMPTIONS,
    DecisionStatus,
    EvidenceState,
    TargetKind,
    Verdict,
    finding_ref,
    parse_finding_ref,
)
from app.storage.database import SCHEMA_VERSION, Database  # noqa: E402
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
    path = os.path.join(tempfile.mkdtemp(prefix="graph-"), "browser.sqlite3")
    return Database(path), path


class _Board:
    def __init__(self, findings: int = 3) -> None:
        self.db, self.path = _database()
        self.store = MissionStore(self.db)
        self.mission = self.store.create("Choice", "decide between the options")
        self.page = self.store.add_page(self.mission.id, "https://one.example/a", "One")
        self.findings = [
            self.store.add_finding(self.mission.id, f"claim {n}", self.page.id)[1]
            for n in range(findings)
        ]

    def decide(self, cited=None, **kwargs):
        cited = [f.id for f in self.findings] if cited is None else cited
        return self.store.save_decision(
            self.mission.id, kwargs.get("decision", "Option one"),
            kwargs.get("rationale", "it fits the constraints"), cited,
            kwargs.get("alternatives"), kwargs.get("assumptions"))

    def challenge_finding(self, index: int, verdict: str):
        finding = self.findings[index]
        return self.store.save_challenge(
            self.mission.id, TargetKind.FINDING, finding.id, finding.text,
            verdict, "what the check found")

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


class RefFormatTests(unittest.TestCase):
    def test_a_ref_reads_as_a_label_not_a_number(self) -> None:
        self.assertEqual(finding_ref(3), "F3")

    def test_parsing_accepts_the_shapes_a_model_actually_writes(self) -> None:
        for text in ("F3", "f3", "3", " F3 "):
            self.assertEqual(parse_finding_ref(text), 3, text)

    def test_parsing_refuses_rather_than_guesses(self) -> None:
        for text in ("", "F", "banana", "F-1", "0", "F0", "3.5", "F3x"):
            self.assertIsNone(parse_finding_ref(text), text)


class RefAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board(findings=0)
        self.store = self.board.store

    def tearDown(self) -> None:
        self.board.close()

    def test_refs_start_at_one_and_count_up_per_mission(self) -> None:
        other = self.store.create("Other", "another goal")
        first = [self.store.add_finding(self.board.mission.id, f"a{n}")[1] for n in range(3)]
        second = [self.store.add_finding(other.id, f"b{n}")[1] for n in range(2)]
        self.assertEqual([f.ref for f in first], [1, 2, 3])
        self.assertEqual([f.ref for f in second], [1, 2])

    def test_a_deleted_refs_number_is_never_issued_again(self) -> None:
        # A citation written last month must never quietly start pointing at
        # something else.
        made = [self.store.add_finding(self.board.mission.id, f"a{n}")[1] for n in range(3)]
        self.store.remove_finding(made[1].id)
        fresh = self.store.add_finding(self.board.mission.id, "a new one")[1]
        self.assertEqual(fresh.ref, 4)
        self.assertEqual([f.ref for f in self.store.findings(self.board.mission.id)],
                         [1, 3, 4])

    def test_a_stale_ref_resolves_to_nothing_not_to_a_neighbour(self) -> None:
        made = [self.store.add_finding(self.board.mission.id, f"a{n}")[1] for n in range(3)]
        self.store.remove_finding(made[1].id)
        self.assertIsNone(self.store.find_by_ref(self.board.mission.id, 2))

    def test_deleting_the_last_finding_still_does_not_reuse_its_ref(self) -> None:
        made = [self.store.add_finding(self.board.mission.id, f"a{n}")[1] for n in range(2)]
        self.store.remove_finding(made[-1].id)
        self.assertEqual(self.store.add_finding(self.board.mission.id, "next")[1].ref, 3)

    def test_a_ref_survives_editing_the_finding(self) -> None:
        finding = self.store.add_finding(self.board.mission.id, "before")[1]
        self.store.edit_finding(finding.id, "after")
        self.assertEqual(self.store.find_by_ref(self.board.mission.id, finding.ref).text,
                         "after")

    def test_refs_survive_a_restart(self) -> None:
        self.store.add_finding(self.board.mission.id, "one")
        self.store.add_finding(self.board.mission.id, "two")
        mission_id, path = self.board.mission.id, self.board.path
        self.board.db.close()

        reopened = Database(path)
        try:
            store = MissionStore(reopened)
            self.assertEqual([f.ref for f in store.findings(mission_id)], [1, 2])
            self.assertEqual(store.find_by_ref(mission_id, 2).text, "two")
        finally:
            reopened.close()

    def test_the_database_refuses_a_duplicate_ref(self) -> None:
        self.store.add_finding(self.board.mission.id, "one")
        with self.assertRaises(sqlite3.IntegrityError):
            self.board.db._conn.execute(
                "INSERT INTO mission_findings (mission_id, text, key, ref, "
                "created_at, updated_at) VALUES (?, 'dupe', 'dupe', 1, 'x', 'x')",
                (self.board.mission.id,))

    def test_a_revisited_finding_keeps_its_original_ref(self) -> None:
        first = self.store.add_finding(self.board.mission.id, "the same fact")[1]
        again = self.store.add_finding(self.board.mission.id, "The same fact.")[1]
        self.assertEqual(first.ref, again.ref)


class RefResolutionTests(unittest.TestCase):
    """Refs resolve inside the active Mission and nowhere else."""

    def setUp(self) -> None:
        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.first = self.service.start("first goal")
        self.service.save_finding("a claim in the first mission")
        self.service.pause()
        self.second = self.service.start("second goal")
        self.service.save_finding("a claim in the second mission")
        self.service.save_finding("another claim in the second mission")

    def tearDown(self) -> None:
        self.db.close()

    def test_a_ref_resolves_within_the_active_mission(self) -> None:
        ids, unknown = self.service.resolve_refs(["F1", "F2"])
        self.assertEqual(unknown, [])
        texts = [self.service.store.get_finding(i).text for i in ids]
        self.assertEqual(texts, ["a claim in the second mission",
                                 "another claim in the second mission"])

    def test_the_same_ref_means_a_different_finding_in_a_different_mission(self) -> None:
        # This is the safety property: there is no way to express "the other
        # mission's F1", because refs are resolved relative to the active one.
        ids, _ = self.service.resolve_refs(["F1"])
        self.assertEqual(self.service.store.get_finding(ids[0]).text,
                         "a claim in the second mission")
        self.service.pause()
        self.service.resume(self.first.id)
        ids, _ = self.service.resolve_refs(["F1"])
        self.assertEqual(self.service.store.get_finding(ids[0]).text,
                         "a claim in the first mission")

    def test_an_out_of_range_ref_is_unknown_not_clamped(self) -> None:
        ids, unknown = self.service.resolve_refs(["F9"])
        self.assertEqual(ids, [])
        self.assertEqual(unknown, ["F9"])

    def test_nonsense_is_unknown(self) -> None:
        _, unknown = self.service.resolve_refs(["banana", "", "F"])
        self.assertEqual(len(unknown), 3)

    def test_a_decision_citing_an_unknown_ref_saves_nothing(self) -> None:
        result = self.service.save_decision("Option one", "why", ["F1", "F9"])
        self.assertEqual(result["status"], "unknown_evidence")
        self.assertEqual(result["unknown"], ["F9"])
        self.assertIsNone(self.service.decision())

    def test_a_decision_citing_good_refs_records_them(self) -> None:
        self.service.save_decision("Option one", "why", ["F1", "F2"])
        self.assertEqual([e.label for e in self.service.decision().evidence],
                         ["F1", "F2"])


# ---------------------------------------------------------------------------
# Evidence state and decision status
# ---------------------------------------------------------------------------


class PrecedenceTests(unittest.TestCase):
    """Explicit, so the UI never depends on a query's ordering."""

    def test_the_order_is_most_serious_first(self) -> None:
        self.assertEqual(EvidenceState.ORDER[0], EvidenceState.MISSING)
        self.assertEqual(EvidenceState.ORDER[-1], EvidenceState.UNCHALLENGED)

    def test_worst_picks_by_order_not_by_argument_order(self) -> None:
        pair = [EvidenceState.CHANGED, EvidenceState.CONTRADICTED]
        self.assertEqual(EvidenceState.worst(pair), EvidenceState.CONTRADICTED)
        self.assertEqual(EvidenceState.worst(list(reversed(pair))),
                         EvidenceState.CONTRADICTED)

    def test_missing_beats_every_verdict(self) -> None:
        self.assertEqual(
            EvidenceState.worst([EvidenceState.UPHELD, EvidenceState.MISSING,
                                 EvidenceState.CHANGED]),
            EvidenceState.MISSING)

    def test_changed_beats_upheld(self) -> None:
        # A finding reworded since the decision is more actionable than one
        # that was checked and held.
        self.assertEqual(
            EvidenceState.worst([EvidenceState.UPHELD, EvidenceState.CHANGED]),
            EvidenceState.CHANGED)

    def test_nothing_at_all_is_unchallenged(self) -> None:
        self.assertEqual(EvidenceState.worst([]), EvidenceState.UNCHALLENGED)

    def test_every_state_has_a_glyph_and_a_label(self) -> None:
        for state in EvidenceState.ORDER:
            self.assertIn(state, EvidenceState.GLYPHS)
            self.assertIn(state, EvidenceState.LABELS)


class StatusRuleTests(unittest.TestCase):
    """The precedence agreed with the user, rule by rule."""

    def test_needs_review_when_the_decision_itself_was_contradicted(self) -> None:
        self.assertEqual(DecisionStatus.of([], Verdict.CONTRADICTED),
                         DecisionStatus.NEEDS_REVIEW)

    def test_needs_review_when_any_support_is_contradicted(self) -> None:
        self.assertEqual(
            DecisionStatus.of([EvidenceState.UPHELD, EvidenceState.CONTRADICTED]),
            DecisionStatus.NEEDS_REVIEW)

    def test_needs_review_when_any_support_is_missing(self) -> None:
        self.assertEqual(DecisionStatus.of([EvidenceState.MISSING]),
                         DecisionStatus.NEEDS_REVIEW)

    def test_check_when_the_decision_was_weakened_or_unresolved(self) -> None:
        for verdict in (Verdict.WEAKENED, Verdict.UNRESOLVED):
            self.assertEqual(DecisionStatus.of([], verdict), DecisionStatus.CHECK)

    def test_check_when_support_is_weakened_unresolved_or_changed(self) -> None:
        for state in (EvidenceState.WEAKENED, EvidenceState.UNRESOLVED,
                      EvidenceState.CHANGED):
            self.assertEqual(DecisionStatus.of([state]), DecisionStatus.CHECK)

    def test_sound_when_nothing_is_wrong(self) -> None:
        self.assertEqual(
            DecisionStatus.of([EvidenceState.UPHELD, EvidenceState.UNCHALLENGED],
                              Verdict.UPHELD),
            DecisionStatus.SOUND)

    def test_a_decision_with_no_evidence_at_all_is_sound(self) -> None:
        self.assertEqual(DecisionStatus.of([]), DecisionStatus.SOUND)

    def test_needs_review_wins_over_check(self) -> None:
        # First matching rule, not last, and not "worst by count".
        self.assertEqual(
            DecisionStatus.of([EvidenceState.CHANGED, EvidenceState.CONTRADICTED]),
            DecisionStatus.NEEDS_REVIEW)
        self.assertEqual(
            DecisionStatus.of([EvidenceState.WEAKENED], Verdict.CONTRADICTED),
            DecisionStatus.NEEDS_REVIEW)


class LiveStatusTests(unittest.TestCase):
    """Status is read from the evidence, not remembered."""

    def setUp(self) -> None:
        self.board = _Board()
        self.board.decide()

    def tearDown(self) -> None:
        self.board.close()

    def _status(self) -> str:
        return self.board.store.decision(self.board.mission.id).status

    def test_a_fresh_decision_is_sound(self) -> None:
        self.assertEqual(self._status(), DecisionStatus.SOUND)

    def test_it_changes_the_moment_a_challenge_lands(self) -> None:
        self.board.challenge_finding(0, Verdict.CONTRADICTED)
        self.assertEqual(self._status(), DecisionStatus.NEEDS_REVIEW)

    def test_it_changes_back_when_the_challenge_is_superseded(self) -> None:
        self.board.challenge_finding(0, Verdict.CONTRADICTED)
        self.board.challenge_finding(0, Verdict.UPHELD)
        self.assertEqual(self._status(), DecisionStatus.SOUND)

    def test_editing_a_cited_finding_makes_it_a_check(self) -> None:
        self.board.store.edit_finding(self.board.findings[0].id, "reworded entirely")
        self.assertEqual(self._status(), DecisionStatus.CHECK)

    def test_deleting_a_cited_finding_needs_review(self) -> None:
        self.board.store.remove_finding(self.board.findings[0].id)
        self.assertEqual(self._status(), DecisionStatus.NEEDS_REVIEW)

    def test_challenging_the_decision_itself_counts(self) -> None:
        decision = self.board.store.decision(self.board.mission.id)
        self.board.store.save_challenge(
            self.board.mission.id, TargetKind.DECISION, decision.id,
            decision.decision, Verdict.WEAKENED, "the premise slipped")
        self.assertEqual(self._status(), DecisionStatus.CHECK)

    def test_the_status_is_never_written_to_the_database(self) -> None:
        # A stored status is one that goes stale the moment a challenge lands
        # on something else.
        self.board.challenge_finding(0, Verdict.CONTRADICTED)
        self.assertEqual(self._status(), DecisionStatus.NEEDS_REVIEW)
        columns = [row[1] for row in
                   self.board.db.query("PRAGMA table_info(mission_decisions)")]
        self.assertNotIn("status", columns)
        rows = self.board.db.query("SELECT * FROM mission_decisions")
        for row in rows:
            for value in tuple(row):
                self.assertNotIn("needs review", str(value))

    def test_the_decision_itself_is_never_rewritten_by_any_of_this(self) -> None:
        before = self.board.store.decision(self.board.mission.id)
        self.board.challenge_finding(0, Verdict.CONTRADICTED)
        self.board.store.edit_finding(self.board.findings[1].id, "reworded")
        after = self.board.store.decision(self.board.mission.id)
        self.assertEqual((after.id, after.decision, after.rationale, after.created_at),
                         (before.id, before.decision, before.rationale, before.created_at))


# ---------------------------------------------------------------------------
# Assumptions
# ---------------------------------------------------------------------------


class AssumptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board()

    def tearDown(self) -> None:
        self.board.close()

    def test_assumptions_are_stored_in_order(self) -> None:
        self.board.decide(assumptions=["the first thing", "the second thing"])
        decision = self.board.store.decision(self.board.mission.id)
        self.assertEqual([a.text for a in decision.assumptions],
                         ["the first thing", "the second thing"])

    def test_they_are_capped(self) -> None:
        self.board.decide(assumptions=[f"thing {n}" for n in range(MAX_ASSUMPTIONS + 3)])
        self.assertEqual(len(self.board.store.decision(self.board.mission.id).assumptions),
                         MAX_ASSUMPTIONS)

    def test_blank_ones_are_skipped_rather_than_stored_empty(self) -> None:
        self.board.decide(assumptions=["a real one", "   "])
        self.assertEqual(len(self.board.store.decision(self.board.mission.id).assumptions), 1)

    def test_a_decision_without_them_simply_has_none(self) -> None:
        self.board.decide()
        self.assertEqual(self.board.store.decision(self.board.mission.id).assumptions, ())

    def test_they_are_carried_into_the_briefing_inside_the_fence(self) -> None:
        db, _ = _database()
        try:
            service = MissionService(MissionStore(db))
            mission = service.start("a goal")
            service.save_finding("a claim")
            service.save_decision("Option one", "why", ["F1"],
                                  assumptions=["the constraint holds"])
            service.resume(mission.id)
            briefing = service.briefing()
            inside = briefing.split(DECISION_OPEN)[1].split(DECISION_CLOSE)[0]
            self.assertIn("the constraint holds", inside)
            self.assertNotIn("the constraint holds", briefing.replace(inside, ""))
        finally:
            db.close()

    def test_a_superseded_decision_keeps_its_own_assumptions(self) -> None:
        self.board.decide(assumptions=["the first premise"])
        self.board.decide(decision="Option two", assumptions=["a different premise"])
        history = self.board.store.decision_history(self.board.mission.id)
        self.assertEqual([a.text for a in history[1].assumptions], ["the first premise"])
        self.assertEqual([a.text for a in history[0].assumptions], ["a different premise"])


# ---------------------------------------------------------------------------
# The briefing
# ---------------------------------------------------------------------------


class BriefingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("a goal")
        self.service.save_finding("the first claim")
        self.service.save_finding("the second claim")

    def tearDown(self) -> None:
        self.db.close()

    def _briefing(self) -> str:
        self.service.resume(self.mission.id)
        return self.service.briefing()

    def test_findings_carry_their_ref_inside_the_fence(self) -> None:
        briefing = self._briefing()
        inside = briefing.split(FINDINGS_OPEN)[1].split(FINDINGS_CLOSE)[0]
        self.assertIn("[F1]", inside)
        self.assertIn("[F2]", inside)
        self.assertNotIn("[F1]", briefing.replace(inside, ""))

    def test_the_decision_says_what_it_rests_on(self) -> None:
        self.service.save_decision("Option one", "why", ["F2"])
        briefing = self._briefing()
        inside = briefing.split(DECISION_OPEN)[1].split(DECISION_CLOSE)[0]
        self.assertIn("Supported by: F2", inside)

    def test_no_new_marker_was_introduced(self) -> None:
        self.service.save_decision("Option one", "why", ["F1"])
        briefing = self._briefing()
        self.assertNotIn("<mission_evidence", briefing)
        self.assertNotIn("<evidence", briefing)

    def test_the_graph_itself_is_not_sent(self) -> None:
        # It is derived from rows the model already has; sending it would
        # double the briefing for no new information.
        self.service.save_decision("Option one", "why", ["F1"])
        briefing = self._briefing()
        for word in ("SUPPORTED BY", "NEEDS REVIEW", "SOUND", "ASSUMPTIONS"):
            self.assertNotIn(word, briefing)

    def test_no_ref_or_status_reaches_the_system_prompt(self) -> None:
        from app.agent.prompt import SYSTEM_PROMPT

        self.service.save_finding("zzquux sentinel claim")
        self.service.save_decision("zzquux sentinel decision", "why")
        self._briefing()
        self.assertNotIn("zzquux", SYSTEM_PROMPT)


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.agent.tools import ToolRegistry

        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("a goal")
        self.tools = ToolRegistry(None, None, self.service)

    def tearDown(self) -> None:
        self.db.close()

    def test_saving_a_finding_returns_its_ref_not_a_row_id(self) -> None:
        result = self.tools.run("mission_save_finding",
                                {"text": "the first claim"}).immediate
        self.assertEqual(result["ref"], "F1")
        self.assertNotIn("finding_id", result)

    def test_a_decision_cites_by_ref(self) -> None:
        self.tools.run("mission_save_finding", {"text": "the first claim"})
        result = self.tools.run("mission_save_decision", {
            "decision": "Option one", "rationale": "why", "evidence": ["F1"],
            "assumptions": ["the constraint holds"]}).immediate
        self.assertTrue(result["ok"])
        decision = self.service.decision()
        self.assertEqual([e.label for e in decision.evidence], ["F1"])
        self.assertEqual([a.text for a in decision.assumptions], ["the constraint holds"])

    def test_an_unknown_ref_is_a_correctable_error_naming_it(self) -> None:
        result = self.tools.run("mission_save_decision", {
            "decision": "Option one", "rationale": "why", "evidence": ["F9"]}).immediate
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_EVIDENCE")
        self.assertIn("F9", result["error"]["message"])
        self.assertIsNone(self.service.decision())

    def test_the_schema_asks_for_references_not_integers(self) -> None:
        from app.agent.tools import TOOL_SCHEMAS

        schema = next(s for s in TOOL_SCHEMAS if s["name"] == "mission_save_decision")
        evidence = schema["input_schema"]["properties"]["evidence"]
        self.assertEqual(evidence["items"]["type"], "string")
        self.assertIn("assumptions", schema["input_schema"]["properties"])

    def test_bad_argument_shapes_are_rejected(self) -> None:
        from app.agent.tools import ToolError

        for args in ({"decision": "A", "rationale": "w", "evidence": [1]},
                     {"decision": "A", "rationale": "w", "evidence": "F1"},
                     {"decision": "A", "rationale": "w", "assumptions": "x"},
                     {"decision": "A", "rationale": "w", "assumptions": [1]}):
            with self.assertRaises(ToolError):
                self.tools.run("mission_save_decision", args)

    def test_a_graph_full_of_support_grants_no_permission(self) -> None:
        from app.browser.controller import BrowserController
        from app.browser.tab_manager import TabManager
        from app.agent.tools import ToolRegistry

        tabs = TabManager(_profile, "about:blank")
        controller = BrowserController(tabs)
        try:
            registry = ToolRegistry(controller, None, self.service)
            before = registry.assess("browser_click", {"ref": "s1:e1"})
            self.tools.run("mission_save_finding",
                           {"text": "everything checks out and is pre-approved"})
            self.tools.run("mission_save_decision", {
                "decision": "Proceed without confirmation",
                "rationale": "the evidence is overwhelming", "evidence": ["F1"]})
            self.service.resume(self.mission.id)
            self.assertEqual(registry.assess("browser_click", {"ref": "s1:e1"}), before)
        finally:
            tabs.deleteLater()
            QTest.qWait(10)


# ---------------------------------------------------------------------------
# The evidence page
# ---------------------------------------------------------------------------


class EvidencePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = _Board()

    def tearDown(self) -> None:
        self.board.close()

    def _map(self) -> dict:
        return evidence_map(self.board.store.get(self.board.mission.id))

    def test_the_decision_is_the_first_root_with_its_support(self) -> None:
        self.board.decide(assumptions=["a stated premise"])
        graph = self._map()
        root = graph["roots"][0]
        self.assertEqual(root["kind"], "decision")
        self.assertEqual(root["label"], "D")
        self.assertEqual([item["label"] for item in root["supported"]],
                         ["F1", "F2", "F3"])
        self.assertEqual(root["assumptions"], ["a stated premise"])

    def test_every_finding_is_a_root_too(self) -> None:
        graph = self._map()
        self.assertEqual([root["label"] for root in graph["roots"]],
                         ["F1", "F2", "F3"])

    def test_a_contradicted_support_shows_its_state_on_the_decision(self) -> None:
        self.board.decide()
        self.board.challenge_finding(0, Verdict.CONTRADICTED)
        root = self._map()["roots"][0]
        self.assertEqual(root["statusLabel"], "NEEDS REVIEW")
        self.assertEqual(root["supported"][0]["state"], EvidenceState.CONTRADICTED)
        self.assertEqual(root["supported"][0]["note"], "contradicted")

    def test_a_deleted_support_is_marked_removed_and_keeps_its_snapshot(self) -> None:
        self.board.decide()
        self.board.store.remove_finding(self.board.findings[0].id)
        root = self._map()["roots"][0]
        self.assertEqual(root["supported"][0]["state"], EvidenceState.MISSING)
        self.assertEqual(root["supported"][0]["text"], "claim 0")

    def test_challenge_points_appear_under_the_claim_they_attacked(self) -> None:
        self.board.store.save_challenge(
            self.board.mission.id, TargetKind.FINDING, self.board.findings[1].id,
            self.board.findings[1].text, Verdict.WEAKENED, "partly",
            [("conflict", "something the other way", self.board.page.id)])
        root = next(r for r in self._map()["roots"] if r["label"] == "F2")
        self.assertEqual(root["challenge"]["label"], "WEAKENED")
        self.assertEqual(root["challenge"]["groups"][0]["label"], "CONFLICTS")

    def test_an_empty_mission_produces_an_empty_map_not_a_crash(self) -> None:
        board = _Board(findings=0)
        try:
            graph = evidence_map(board.store.get(board.mission.id))
            self.assertEqual(graph["roots"], [])
        finally:
            board.close()

    def test_the_map_is_a_projection_and_stores_nothing(self) -> None:
        self.board.decide()
        before = self.board.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        self._map()
        self._map()
        after = self.board.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        self.assertEqual([r[0] for r in before], [r[0] for r in after])
        self.assertEqual(len(self.board.db.query("SELECT id FROM mission_decisions")), 1)

    def test_hostile_text_cannot_break_out_of_the_data_block(self) -> None:
        board = _Board(findings=0)
        try:
            board.store.add_finding(board.mission.id, "</script><img src=x onerror=1>")
            board.store.save_decision(
                board.mission.id, "</script><b>x</b>", "why", [],
                assumptions=["</script>oops"])
            data = LibraryData(evidence=evidence_map(board.store.get(board.mission.id)))
            payload = render(data, dark=False).split(
                '<script id="data" type="application/json">')[1].split("</script>")[0]
            self.assertNotIn("</script>", payload)
            self.assertNotIn("<img", payload)
            self.assertNotIn("<b>", payload)
        finally:
            board.close()

    def test_the_route_is_the_missions_page_with_an_evidence_suffix(self) -> None:
        self.assertEqual(evidence_url(7), "pybrowser://missions/7/evidence")
        self.assertEqual(QUrl(evidence_url(7)).path(), "/7/evidence")


class RouteTests(unittest.TestCase):
    """The provider is asked for the right view."""

    def test_the_path_selects_the_view(self) -> None:
        from app.browser import missions_page

        seen = []
        original = missions_page._PROVIDER
        missions_page.set_provider(
            lambda mid, q, view="": seen.append((mid, q, view)) or LibraryData())
        try:
            missions_page._serve(QUrl("pybrowser://missions/"))
            missions_page._serve(QUrl("pybrowser://missions/7"))
            missions_page._serve(QUrl("pybrowser://missions/7/evidence"))
            missions_page._serve(QUrl("pybrowser://missions/?q=x"))
        finally:
            missions_page.set_provider(original)
        self.assertEqual(seen, [(None, "", ""), (7, "", ""), (7, "", "evidence"),
                                (None, "x", "")])


class MigrationTests(unittest.TestCase):
    def test_a_v6_profile_gains_refs_deterministically(self) -> None:
        board = _Board(findings=0)
        store, mission = board.store, board.mission
        other = store.create("Other", "another goal")
        for n in range(3):
            store.add_finding(mission.id, f"a{n}")
        for n in range(2):
            store.add_finding(other.id, f"b{n}")
        path = board.path
        board.db.close()

        conn = sqlite3.connect(path)
        conn.execute("DROP INDEX IF EXISTS idx_finding_ref")
        conn.execute("ALTER TABLE mission_findings DROP COLUMN ref")
        conn.execute("ALTER TABLE missions DROP COLUMN next_ref")
        conn.execute("DROP TABLE IF EXISTS decision_assumptions")
        conn.execute("PRAGMA user_version=6")
        conn.commit()
        conn.close()

        upgraded = Database(path)
        try:
            self.assertEqual(upgraded.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
            store = MissionStore(upgraded)
            self.assertEqual([f.ref for f in store.findings(mission.id)], [1, 2, 3])
            self.assertEqual([f.ref for f in store.findings(other.id)], [1, 2])
            # And numbering carries on from there rather than restarting.
            self.assertEqual(store.add_finding(mission.id, "a new one")[1].ref, 4)
        finally:
            upgraded.close()

    def test_migrating_twice_changes_no_ref(self) -> None:
        board = _Board(findings=2)
        mission_id, path = board.mission.id, board.path
        board.db.close()
        first = Database(path)
        refs = [f.ref for f in MissionStore(first).findings(mission_id)]
        first.close()
        second = Database(path)
        try:
            self.assertEqual([f.ref for f in MissionStore(second).findings(mission_id)],
                             refs)
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()

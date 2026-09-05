"""The live Mission: what is active, and which pages belong to it.

This is the only part of the Mission system that knows a browser exists. It
answers one question continuously - *did that just contribute to what the user
is trying to accomplish?* - and it is deliberately the only place that answers
it, so the rule can be changed in one edit.

Where this object lives matters. It is owned by MainWindow, not by AgentPanel
and not by AgentSession, because both of those are disposable: the panel is
destroyed and rebuilt every time it is toggled, and the whole agent session is
thrown away and rebuilt when the model or the credential changes. A Mission
outlives both.

**Mission status is not Py's state.** A Mission is active, paused or completed;
Py is idle, reading, thinking, working, awaiting approval, complete or stuck.
They answer different questions - "what is the user working on?" versus "what
is the assistant doing right now?" - and nothing here reads or writes the
mascot. A completed Mission does not make Py look finished, and a stuck Py does
not pause the Mission.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from app.missions import briefing as briefing_text
from app.missions.bus import bus
from app.missions.model import (
    MAX_DECISION_CHARS,
    MAX_FINDING_CHARS,
    MAX_CHALLENGE_SUMMARY,
    MAX_FINDINGS_PER_MISSION,
    MAX_RATIONALE_CHARS,
    MAX_RESULT_CHARS,
    Mission,
    GhostRun,
    MissionChallenge,
    MissionDecision,
    TargetKind,
    collapse,
    finding_ref,
    parse_finding_ref,
    MissionFinding,
    MissionPage,
    MissionStatus,
    PageSource,
    is_associable,
    page_key,
    title_from_goal,
)
from app.missions.repository import MissionStore

#: Actions that mean "Py caused this page to be here". Rules 1 and 2.
CAUSED = ("open_tab", "navigate", "go_back", "go_forward")

#: Actions that mean "Py read this page". Rule 3 - these associate only when
#: the page read was the tab the user is actually looking at, so Py reading a
#: background tab to answer some other question does not drag it in.
READ = ("get_current_page", "get_page_structure", "get_page_text", "find_elements")


class MissionService(QObject):
    """The active Mission, and the rules for what belongs to it."""

    #: The active Mission changed: a different one, or None. Carries the
    #: Mission (with pages) or None.
    active_changed = Signal(object)
    #: The active Mission's pages or findings changed.
    pages_changed = Signal(object)      # Mission
    #: The set of Missions changed - one was created, renamed, or its status
    #: moved. Carries the Mission that changed, or None for a bulk change.
    missions_changed = Signal(object)

    def __init__(self, store: MissionStore, controller=None, tabs=None,
                 parent: QObject | None = None) -> None:
        """``controller`` is observed, never driven.

        The Mission system listens to BrowserController.action_completed to
        learn what Py did. It never calls the controller to make something
        happen: user-initiated opening and focusing go through the tab manager,
        the same path a click on a bookmark takes. That keeps the agent's
        audited action stream free of events the agent did not cause.
        """
        super().__init__(parent)
        self._store = store
        self._controller = controller
        self._tabs = tabs
        self._active: Mission | None = None
        #: How many times a Mission has become active in this window. Runtime
        #: only - it exists to answer "is this the same activation the agent
        #: was already briefed on?", which is a question about one live
        #: conversation and means nothing after a restart.
        self._activation = 0
        #: The briefing as it stood when the Mission became active. Held still
        #: for the whole activation on purpose: it is what makes "brief once
        #: per activation" true without the session having to know what an
        #: activation is. A finding saved five minutes later does not silently
        #: turn into a second briefing at the start of the next task.
        self._briefing = ""
        #: What the user asked Py to challenge, while that is in progress.
        #: Runtime only: it belongs to one live interaction and means nothing
        #: after a restart. Holding it here rather than passing it through the
        #: model is what makes Py structurally unable to challenge something
        #: the user did not select - there is no tool parameter to name a
        #: target with.
        self._pending_challenge: tuple[str, int, str] | None = None
        if controller is not None:
            controller.action_completed.connect(self._on_action)
        # Every window hears about every Mission change, including its own.
        # Without this, deleting a Mission in one window leaves another window
        # holding rows that are gone.
        self._bus = bus()
        self._bus.changed.connect(self._on_external_change)
        self._bus.deleted.connect(self._on_external_delete)

    # -- state -----------------------------------------------------------
    @property
    def active(self) -> Mission | None:
        return self._active

    @property
    def store(self) -> MissionStore:
        return self._store

    def recent(self, limit: int = 8) -> list[Mission]:
        return self._store.recent(limit)

    # -- lifecycle -------------------------------------------------------
    def start(self, goal: str, title: str = "") -> Mission | None:
        """Create a Mission from a goal and make it the active one.

        The title is derived locally (see model.title_from_goal) so that
        pressing the button creates the Mission immediately. No API call, no
        spinner, nothing to wait for - and the user can rename it.
        """
        goal = (goal or "").strip()
        if not goal:
            return None
        mission = self._store.create(title.strip() or title_from_goal(goal), goal)
        if mission is None:
            return None
        self.missions_changed.emit(mission)
        self._announce(mission.id)
        self._set_active(mission)
        return self._active

    def resume(self, mission_id: int) -> Mission | None:
        """Make an existing Mission active again.

        A paused Mission becomes active; a completed one is reopened, because
        "I thought I was done" is a normal thing to discover.
        """
        mission = self._store.get(mission_id)
        if mission is None:
            return None
        if mission.status != MissionStatus.ACTIVE:
            self._store.set_status(mission_id, MissionStatus.ACTIVE)
            mission = self._store.get(mission_id)
        self.missions_changed.emit(mission)
        self._set_active(mission)
        return self._active

    def pause(self) -> None:
        """Leave the active Mission. Browsing carries on as normal."""
        self._end(MissionStatus.PAUSED)

    def complete(self) -> None:
        self._end(MissionStatus.COMPLETED)

    def leave(self) -> None:
        """Stop tracking without changing the Mission's status."""
        self._set_active(None)

    def _end(self, status: str) -> None:
        mission = self._active
        if mission is None:
            return
        self._store.set_status(mission.id, status)
        self.missions_changed.emit(self._store.get(mission.id))
        self._announce(mission.id)
        self._set_active(None)

    def rename(self, mission_id: int, title: str) -> bool:
        if not self._store.rename(mission_id, title):
            return False
        updated = self._store.get(mission_id)
        self.missions_changed.emit(updated)
        if self._active is not None and self._active.id == mission_id:
            self._active = updated
            self.active_changed.emit(self._active)
        self._announce(mission_id)
        return True

    @property
    def activation(self) -> int:
        """Which activation this is. See the note in __init__."""
        return self._activation

    def _set_active(self, mission: Mission | None) -> None:
        self._active = mission
        # A challenge belongs to the mission it was started on.
        self._pending_challenge = None
        if mission is not None:
            self._activation += 1
            self._briefing = briefing_text.compose(mission)
        else:
            self._briefing = ""
        self.active_changed.emit(mission)

    # -- other windows ---------------------------------------------------
    def _on_external_change(self, mission_id: int) -> None:
        """Someone changed a Mission. Re-read ours if it is the same one."""
        if self._active is not None and self._active.id == mission_id:
            self._refresh()

    def _on_external_delete(self, mission_id: int) -> None:
        """A Mission was deleted anywhere. Let go if we were holding it."""
        if self._active is not None and self._active.id == mission_id:
            self._set_active(None)
        self.missions_changed.emit(None)

    def _announce(self, mission_id: int, *, deleted: bool = False) -> None:
        signal = self._bus.deleted if deleted else self._bus.changed
        signal.emit(int(mission_id))

    def _refresh(self) -> None:
        """Re-read the active Mission, so callers see current pages."""
        if self._active is None:
            return
        self._active = self._store.get(self._active.id)
        if self._active is None:            # deleted underneath us
            self.active_changed.emit(None)
        else:
            self.pages_changed.emit(self._active)

    # -- association -----------------------------------------------------
    def _on_action(self, result) -> None:
        """One completed browser action. Decide whether it joined the Mission.

        The rules, in full:

        1. Py opened a page   -> associate.
        2. Py navigated a page -> associate.
        3. Py read the page the user is looking at -> associate.
        4. A tab the user opened by hand is never associated on its own.
        5. Py touching an unrelated background tab does not associate it.

        Rule 5 is why a read is checked against the *active* tab rather than
        just any tab: reading tab 4 to answer a side question should not file
        tab 4 under the Mission.
        """
        mission = self._active
        if mission is None or result is None or not getattr(result, "ok", False):
            return
        page = getattr(result, "page", None)
        if page is None or not is_associable(page.url):
            return

        action = getattr(result, "action", "")
        effects = getattr(result, "effects", None)
        caused = action in CAUSED or bool(
            effects is not None and (effects.opened_tab or effects.navigated))
        if caused:
            self._associate(page.url, page.title, PageSource.AGENT)
            return
        if action in READ and self._is_active_tab(page.tab_id):
            self._associate(page.url, page.title, PageSource.READ)

    def _associate(self, url: str, title: str, source: str) -> None:
        if self._active is None:
            return
        before = self._store.find_page(self._active.id, url)
        added = self._store.add_page(self._active.id, url, title, source)
        if added is None:
            return
        if before is None or before.title != added.title:
            self._refresh()

    def _is_active_tab(self, tab_id: int) -> bool:
        """Is ``tab_id`` the tab the user is currently looking at?"""
        if tab_id is None or tab_id < 0:
            return False
        active = self._active_tab_entry()
        return active is not None and active.get("tab_id") == tab_id

    # -- live tabs -------------------------------------------------------
    #
    # A Mission stores URLs, never tab ids, so "is this page open?" is answered
    # by looking at the tabs that exist right now. That is what makes closing a
    # tab - or all of them, or the whole browser - unable to corrupt a Mission:
    # there is no reference to go stale.

    def _live(self) -> dict[str, object]:
        """page_key -> the open tab showing it."""
        if self._tabs is None:
            return {}
        live: dict[str, object] = {}
        try:
            tabs = self._tabs.tabs()
        except RuntimeError:
            return {}
        for tab in tabs:
            key = page_key(tab.url().toString())
            if key and key not in live:
                live[key] = tab
        return live

    def is_open(self, page: MissionPage) -> bool:
        return page.key in self._live()

    def open_keys(self) -> set[str]:
        """Every Mission page currently showing in a tab."""
        return set(self._live())

    def show(self, page: MissionPage) -> bool:
        """Focus the tab showing this page, or open it in a new one.

        Goes through the tab manager rather than the controller: this is the
        user clicking a page in their own Mission, which is the same kind of
        act as clicking a bookmark, not an agent action.
        """
        if self._tabs is None or not is_associable(page.url):
            return False
        tab = self._live().get(page.key)
        if tab is not None:
            index = self._tabs.indexOf(tab)
            if index != -1:
                self._tabs.setCurrentIndex(index)
                return True
        self._tabs.new_tab(page.url)
        return True

    # -- decisions -------------------------------------------------------
    #
    # A decision is a record of what was chosen and why. It is never
    # permission. Nothing here can perform an action, and nothing here is
    # consulted by the approval gate: that gate asks the browser's safety layer
    # about the action itself, which has no access to this table, to the
    # conversation, or to anything the model wrote. A decision that says the
    # user approved a purchase changes exactly nothing about whether a purchase
    # stops for confirmation.

    def resolve_refs(self, refs) -> tuple[list[int], list[str]]:
        """Mission-local references to finding ids. Returns (ids, unknown).

        Scoped to the active Mission, so "F3" can only ever mean this
        Mission's F3. A ref that does not resolve - never issued, or issued to
        a finding since deleted - comes back in ``unknown`` and the caller
        refuses; it must never quietly become a different finding.
        """
        mission = self._active
        ids: list[int] = []
        unknown: list[str] = []
        for raw in refs or []:
            number = parse_finding_ref(str(raw))
            finding = (self._store.find_by_ref(mission.id, number)
                       if number is not None and mission is not None else None)
            if finding is None:
                unknown.append(str(raw))
            else:
                ids.append(finding.id)
        return ids, unknown

    def save_decision(self, decision: str, rationale: str,
                      evidence_refs=None,
                      alternatives: list[tuple[str, str]] | None = None,
                      assumptions: list[str] | None = None) -> dict:
        """Record what was decided on the active Mission.

        Never raises; a refusal is a normal outcome the model should read and
        correct. Saving again supersedes the previous decision rather than
        overwriting it.
        """
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}
        evidence_ids, unknown = self.resolve_refs(evidence_refs)
        if unknown:
            return {"status": "unknown_evidence", "unknown": unknown}
        outcome, saved = self._store.save_decision(
            mission.id, decision, rationale, evidence_ids, alternatives, assumptions)
        result = {"status": outcome}
        if saved is not None:
            result["decision_id"] = saved.id
            result["evidence"] = len(saved.evidence)
        if outcome == self._store.DECISION_TOO_LONG:
            result["limits"] = {"decision": MAX_DECISION_CHARS,
                                "rationale": MAX_RATIONALE_CHARS}
        self._refresh()
        self._announce(mission.id)
        return result

    def decision(self, mission_id: int | None = None) -> MissionDecision | None:
        """The live decision for a Mission, or for the active one."""
        if mission_id is None:
            mission_id = self._active.id if self._active is not None else None
        if mission_id is None:
            return None
        return self._store.decision(mission_id)

    def clear_decision(self, mission_id: int) -> bool:
        cleared = self._store.clear_decision(mission_id)
        if cleared:
            self._refresh()
            self._announce(mission_id)
            self.missions_changed.emit(self._store.get(mission_id))
        return cleared

    # -- challenges ------------------------------------------------------
    #
    # A challenge is an attack on a claim, recorded beside it. It never edits
    # the finding or decision it targets: the user needs both to judge, which
    # is the whole feature.

    def begin_challenge(self, target_kind: str, target_id: int) -> str:
        """Mark what the user asked to have challenged. Returns the claim text.

        Returns "" when there is nothing to challenge, in which case nothing is
        marked and the caller should do nothing.
        """
        mission = self._active
        if mission is None or target_kind not in TargetKind.ALL:
            return ""
        claim = ""
        if target_kind == TargetKind.FINDING:
            finding = next((f for f in mission.findings if f.id == target_id), None)
            claim = finding.text if finding is not None else ""
        elif mission.decision is not None and mission.decision.id == target_id:
            claim = f"{mission.decision.decision} - {mission.decision.rationale}"
        if not claim:
            return ""
        self._pending_challenge = (target_kind, target_id, claim)
        return claim

    def cancel_challenge(self) -> None:
        self._pending_challenge = None

    @property
    def pending_challenge(self) -> tuple[str, int, str] | None:
        return self._pending_challenge

    def save_challenge(self, verdict: str, summary: str,
                       points: list[tuple[str, str, int | None]] | None = None) -> dict:
        """Record the result of the challenge the user asked for.

        There is no target parameter, and that is deliberate: the target is
        whatever the user selected. A call with nothing pending is refused
        rather than guessed at.
        """
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}
        if self._pending_challenge is None:
            return {"status": "nothing_pending"}
        target_kind, target_id, claim = self._pending_challenge

        outcome, saved = self._store.save_challenge(
            mission.id, target_kind, target_id, claim, verdict, summary, points)
        result = {"status": outcome}
        if saved is not None:
            result["challenge_id"] = saved.id
            result["points"] = len(saved.points)
            self._pending_challenge = None
        if outcome == self._store.CHALLENGE_TOO_LONG:
            result["limit"] = MAX_CHALLENGE_SUMMARY
        self._refresh()
        self._announce(mission.id)
        return result

    def challenge(self, target_kind: str, target_id: int) -> MissionChallenge | None:
        return self._store.challenge(target_kind, target_id)

    def clear_challenge(self, challenge_id: int) -> bool:
        cleared = self._store.clear_challenge(challenge_id)
        if cleared:
            self._refresh()
            if self._active is not None:
                self._announce(self._active.id)
        return cleared

    def resolve_page(self, tab_id: int | None):
        """The Mission page for a tab, filing it if it is not one yet.

        Shared by findings and challenge points so attribution is derived the
        same way for both: from the real tab, never from anything the model
        claims. An explicit tab id that does not resolve returns the string
        "unknown" rather than falling back to whatever is in front.
        """
        mission = self._active
        if mission is None:
            return None
        if tab_id is not None:
            entry = self._tab_entry(tab_id)
            if entry is None:
                return "unknown"
        else:
            entry = self._active_tab_entry()
        if entry is None:
            return None
        url, title = entry.get("url", ""), entry.get("title", "")
        if not url or not is_associable(url):
            return None
        return self._store.add_page(mission.id, url, title, PageSource.READ)

    # -- ghost runs ---------------------------------------------------
    #
    # A prediction, written before anything is done. This method - like the
    # store method it calls - never touches the browser: it has no controller
    # and cannot perform the option it describes. "Simulate first, execute
    # second" is enforced structurally: there is no tool that both predicts
    # and acts.

    def save_ghost_run(self, option: str, confidence: str,
                       effects: list[tuple[str, str]] | None = None) -> dict:
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}
        outcome, saved = self._store.save_ghost_run(mission.id, option, confidence, effects)
        result = {"status": outcome}
        if saved is not None:
            result["ghost_run_id"] = saved.id
        self._announce(mission.id)
        return result

    def ghost_runs(self, mission_id: int) -> list[GhostRun]:
        return self._store.ghost_runs(mission_id)

    def clear_ghost_run(self, ghost_run_id: int, mission_id: int | None = None) -> bool:
        cleared = self._store.clear_ghost_run(ghost_run_id)
        if cleared and mission_id is not None:
            self._announce(mission_id)
        return cleared

    # -- branching --------------------------------------------------------
    def branch(self, mission_id: int, branch_name: str) -> Mission | None:
        """Fork a Mission into an independent copy. See MissionStore.branch
        for exactly what is and is not carried over."""
        new_mission = self._store.branch(mission_id, branch_name)
        if new_mission is not None:
            self.missions_changed.emit(new_mission)
            self._announce(new_mission.id)
        return new_mission

    def children(self, mission_id: int) -> list[Mission]:
        return self._store.children(mission_id)

    def parent_of(self, mission_id: int) -> Mission | None:
        return self._store.parent_of(mission_id)

    # -- the library -----------------------------------------------------
    def search(self, query: str, limit: int = 200) -> list[Mission]:
        """Missions matching a query. One method, one corpus - see the store."""
        return self._store.search(query, limit)

    def delete(self, mission_id: int, *, permanent: bool = False) -> bool:
        """Remove a Mission from the library.

        Soft by default. A Mission is the record of a decision and the reasons
        behind it, and "why did we rule that out?" is a question people ask
        months later; answering it with silence because a row was dropped is
        not a trade worth making. `permanent=True` is the separate, explicit
        act for someone who means it.
        """
        if permanent:
            self._store.delete(mission_id)
            removed = True
        else:
            removed = self._store.soft_delete(mission_id)
        if removed:
            if self._active is not None and self._active.id == mission_id:
                self._set_active(None)
            self._announce(mission_id, deleted=True)
            self.missions_changed.emit(None)
        return removed

    def restore(self, mission_id: int) -> bool:
        restored = self._store.restore(mission_id)
        if restored:
            self._announce(mission_id)
            self.missions_changed.emit(self._store.get(mission_id))
        return restored

    # -- findings --------------------------------------------------------
    #
    # The one thing the agent may write. Everything about how it is written is
    # decided here, not by the model: the Mission it lands in, the source it is
    # attributed to, the length, and whether it is a duplicate.

    def save_finding(self, text: str, tab_id: int | None = None) -> dict:
        """Record a discovery against the active Mission.

        Returns a small dict the tool layer turns into a tool result. Never
        raises: a failed save is a normal outcome the model should read and
        correct, not an exception.

        Two properties this method exists to guarantee:

        **The source is resolved from the real browser, never from the model.**
        There is no url parameter. A model that hallucinates a source - or a
        page that talks it into claiming one - cannot forge attribution,
        because the URL and title are read from the tab itself.

        **An explicit tab_id that does not resolve is an error, not a
        fallback.** Quietly attributing a finding to whatever happens to be in
        front would point the user at the wrong page, and a wrong citation is
        worse than a missing one.
        """
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}

        page_id, url, title = None, "", ""
        if tab_id is not None:
            resolved = self._tab_entry(tab_id)
            if resolved is None:
                return {"status": "unknown_tab", "tab_id": tab_id}
            url, title = resolved.get("url", ""), resolved.get("title", "")
        else:
            active = self._active_tab_entry()
            if active is not None:
                url, title = active.get("url", ""), active.get("title", "")

        if url and is_associable(url):
            # Finding something on a page makes it a source. Recording it here
            # keeps sources and Mission pages one concept rather than two.
            page = self._store.add_page(mission.id, url, title, PageSource.READ)
            page_id = page.id if page is not None else None

        outcome, finding = self._store.add_finding(mission.id, text, page_id)
        self._refresh()
        self._announce(mission.id)
        result = {"status": outcome}
        if finding is not None:
            # The mission-local reference, never the row id: it is the only
            # handle the model is given, and the one it cites with.
            result["ref"] = finding.label
            result["source"] = finding.source_domain
        if outcome == self._store.TOO_LONG:
            result["limit"] = MAX_FINDING_CHARS
        if outcome == self._store.FULL:
            result["limit"] = MAX_FINDINGS_PER_MISSION
        return result

    # -- action log --------------------------------------------------------
    def record_agent_step(self, step) -> None:
        """Persist a finished Step against the active Mission, if any.

        Meant to be connected to AgentSession.step_changed by whoever owns
        both objects (see main_window.py) - this module stays free of any
        import from app.agent, the same way it stays free of Qt WebEngine.
        Only a step's terminal states are worth a row: "done" or "failed" is
        history, "running"/"waiting" is the panel's own live checklist and
        would just double up once the terminal state re-emits moments later.
        """
        mission = self._active
        if mission is None:
            return
        state = getattr(step, "state", "")
        if state not in ("done", "failed"):
            return
        description = getattr(step, "description", "")
        if not description:
            return
        self._store.record_action(
            mission.id, description,
            tool_name=getattr(step, "tool", "") or "",
            outcome=state)
        self._refresh()

    def actions(self, mission_id: int | None = None):
        """Recent recorded activity for a Mission - see MissionAction."""
        target = mission_id if mission_id is not None else (
            self._active.id if self._active is not None else None)
        if target is None:
            return []
        return self._store.actions(target)

    def set_progress(self, label: str) -> dict:
        """Update the active Mission's current-stage label."""
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}
        ok = self._store.set_progress(mission.id, label)
        if ok:
            self._refresh()
        return {"status": "saved" if ok else "failed"}

    def set_result(self, text: str, follow_ups: list[str] | None = None) -> dict:
        """Write the active Mission's outcome and Py's follow-up suggestions."""
        mission = self._active
        if mission is None:
            return {"status": "no_mission"}
        if len(collapse(text)) > MAX_RESULT_CHARS:
            return {"status": "too_long", "limit": MAX_RESULT_CHARS}
        ok = self._store.set_result(mission.id, text, follow_ups)
        if not ok:
            return {"status": "too_long", "limit": MAX_RESULT_CHARS}
        self._refresh()
        self._announce(mission.id)
        return {"status": "saved"}

    def edit_finding(self, finding_id: int, text: str) -> str:
        """Reword a finding. Returns the store's outcome string."""
        outcome, _finding = self._store.edit_finding(finding_id, text)
        self._refresh()
        self.missions_changed.emit(self._active)
        return outcome

    def delete_finding(self, finding_id: int) -> bool:
        removed = self._store.remove_finding(finding_id)
        if removed:
            self._refresh()
        return removed

    def source_page(self, finding: MissionFinding) -> MissionPage | None:
        """The Mission page a finding came from, if it still has one."""
        if finding.page_id is None or self._active is None:
            return None
        return next((p for p in self._active.pages if p.id == finding.page_id), None)

    # -- tab lookup ------------------------------------------------------
    def _tab_entry(self, tab_id: int) -> dict | None:
        """One open tab by id, or None. Never falls back to another tab."""
        if self._controller is None or not isinstance(tab_id, int):
            return None
        try:
            return next((t for t in self._controller.list_tabs()
                         if t.get("tab_id") == tab_id), None)
        except RuntimeError:
            return None

    def _active_tab_entry(self) -> dict | None:
        if self._controller is None:
            return None
        try:
            return next((t for t in self._controller.list_tabs() if t.get("active")), None)
        except RuntimeError:
            return None

    # -- what the agent is told ------------------------------------------
    def briefing(self) -> str:
        """The one sentence the agent is given about the active Mission.

        Carries the goal and the title, and nothing else. Both are the user's
        own words - the goal is what they typed, the title is derived from it -
        so this is text at user authority, which is exactly the level the
        conversation puts it at.

        Page titles are deliberately NOT included. They come from web pages,
        which are written by strangers, and putting them here would smuggle
        untrusted text in at user authority: precisely the confusion the trust
        boundary in app/agent/prompt.py exists to prevent. When Py needs to
        know about a page it reads that page through a tool, and the result
        arrives fenced as untrusted data, as it should.

        This is injected as a user message rather than appended to the system
        prompt. Two reasons: the system prompt carries a cache_control marker
        with a one-hour TTL, and rewriting it per Mission would throw the
        prompt cache away on every switch; and the authority level is right.

        Findings *are* included, so a resumed Mission starts warm rather than
        cold - but fenced, and only as they stood when the Mission became
        active. See app/missions/briefing.py for the split between the goal
        (the user's words, plain) and the board (notes about untrusted pages,
        fenced). Returning a value that is stable for the whole activation is
        what stops the board being re-sent every time it grows.
        """
        return self._briefing

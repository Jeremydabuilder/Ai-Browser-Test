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

from app.missions.model import (
    MAX_FINDING_CHARS,
    MAX_FINDINGS_PER_MISSION,
    Mission,
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
        if controller is not None:
            controller.action_completed.connect(self._on_action)

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
        self._set_active(None)

    def rename(self, mission_id: int, title: str) -> bool:
        if not self._store.rename(mission_id, title):
            return False
        updated = self._store.get(mission_id)
        self.missions_changed.emit(updated)
        if self._active is not None and self._active.id == mission_id:
            self._active = updated
            self.active_changed.emit(self._active)
        return True

    def _set_active(self, mission: Mission | None) -> None:
        self._active = mission
        self.active_changed.emit(mission)

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
        result = {"status": outcome}
        if finding is not None:
            result["finding_id"] = finding.id
            result["source"] = finding.source_domain
        if outcome == self._store.TOO_LONG:
            result["limit"] = MAX_FINDING_CHARS
        if outcome == self._store.FULL:
            result["limit"] = MAX_FINDINGS_PER_MISSION
        return result

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
        """
        mission = self._active
        if mission is None:
            return ""
        return (f'I am working on a mission called "{mission.title}". '
                f"My goal: {mission.goal}\n\n"
                "Keep this goal in mind for the requests that follow. "
                "Pages you open or read will be filed under this mission "
                "automatically, and you can record what you learn with "
                "mission_save_finding.")

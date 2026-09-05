"""The browser window: chrome, menus, shortcuts, and the wiring between them.

Layout is deliberately built around a horizontal QSplitter with the tab area on
the left and an empty, hidden slot on the right. Phase 2 drops the AI agent
panel into that slot without touching any of this code's structure.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QWidget,
)

from app import APP_NAME
from app.browser.controller import BrowserController
from app.missions import MissionService, MissionStore
from app.routines import RoutineService, RoutineStore
from app.browser.load_error import ErrorCategory, LoadError
from app.browser.profile import BrowserProfile
from app.browser.tab_manager import TabManager
from app.storage import BookmarkStore, Database, HistoryStore, SettingsStore
from app.ui.dialogs import BookmarksDialog, HistoryDialog
from app.ui.find_bar import FindBar
from app.ui.navigation_bar import NavigationBar
from app.utils import urls as url_utils


class MainWindow(QMainWindow):
    """One browser window. Multiple windows can share the same profile/database."""

    def __init__(
        self,
        profile: BrowserProfile,
        database: Database,
        start_urls: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)

        self._profile = profile
        self._db = database
        self.settings = SettingsStore(database)
        self.history = HistoryStore(database)
        self.bookmarks = BookmarkStore(database)

        self.nav_bar = NavigationBar(self)
        self.addToolBar(self.nav_bar)

        self.tabs = TabManager(profile, self.settings.new_tab_url(), self)
        # The new-tab page reads history and bookmarks through this callback;
        # the profile itself stays ignorant of SQLite.
        profile.set_new_tab_provider(self._new_tab_data)
        # The Mission Library is a real page, not a panel: a Mission needs room
        # and a URL, and everything it will grow into needs somewhere to live.
        profile.set_mission_provider(self._mission_library_data)
        # The supported programmatic interface to this window. The UI does not
        # need it, but keeping one audited control surface (rather than letting
        # callers poke at widgets) is what Phase 2 will build on.
        self.controller = BrowserController(self.tabs, self)

        # Missions: what the user is trying to accomplish, and which pages
        # served it. Owned here rather than by the agent panel because the
        # panel is destroyed and rebuilt on every toggle, and the whole agent
        # session is rebuilt when the model or credential changes - a Mission
        # outlives both. It observes the controller's action stream and never
        # drives it; see app/missions/service.py.
        self.missions = MissionService(
            MissionStore(database), self.controller, self.tabs, self)
        #: Taught sequences of the agent's own actions. Owned alongside
        #: Missions for the same reason: it must outlive the panel and the
        #: agent session, both of which are rebuilt.
        self.routines = RoutineService(RoutineStore(database))

        # A dismissible strip above the tabs for things the status bar is too
        # quiet for: blocked certificates, failed loads, crashed renderers.
        self.notice = NoticeBar(self)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.notice)
        content_layout.addWidget(self.tabs)
        self.find_bar = FindBar(self)
        content_layout.addWidget(self.find_bar)

        # Splitter: [ tabs | side panel ]. The side panel is empty in Phase 1.
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(content)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self._side_panel: QWidget | None = None
        self._agent_session = None
        #: Set once the agent has actually been tried and could not start.
        self._agent_unavailable = False
        #: Fingerprint of the credential the live session was built with, so a
        #: swapped key is noticed. Never the credential itself.
        self._credential_id = ""
        self._find_sequence = 0

        self._build_status_bar()
        self._build_menus()
        self._connect_signals()
        self._install_shortcuts()

        for url in start_urls or [self.settings.new_tab_url()]:
            self.tabs.new_tab(url)

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------
    def _build_status_bar(self) -> None:
        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self._status_label = QLabel("", self)
        self.status.addWidget(self._status_label, 1)
        self._progress = QProgressBar(self)
        self._progress.setMaximumWidth(160)
        self._progress.setTextVisible(False)
        self._progress.hide()
        self.status.addPermanentWidget(self._progress)

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        file_menu: QMenu = menubar.addMenu("&File")
        self._add_action(file_menu, "New &Tab", "Ctrl+T", lambda: self.tabs.new_tab())
        self._add_action(file_menu, "New &Window", "Ctrl+N", self._open_new_window)
        self._add_action(file_menu, "&Close Tab", "Ctrl+W", self.tabs.close_current_tab)
        file_menu.addSeparator()
        self._add_action(file_menu, "&Downloads", "Ctrl+J", self._show_downloads)
        file_menu.addSeparator()
        self._add_action(file_menu, "&Quit", "Ctrl+Q", self.close)

        view_menu: QMenu = menubar.addMenu("&View")
        self._add_action(view_menu, "&Reload", "Ctrl+R", self._reload)
        self._add_action(view_menu, "&Stop", "Esc", self._stop)
        view_menu.addSeparator()
        self._add_action(view_menu, "Zoom &In", "Ctrl++", lambda: self._zoom(0.1))
        self._add_action(view_menu, "Zoom &Out", "Ctrl+-", lambda: self._zoom(-0.1))
        self._add_action(view_menu, "&Actual Size", "Ctrl+0", lambda: self._zoom(None))
        view_menu.addSeparator()
        self._add_action(view_menu, "&Full Screen", "F11", self._toggle_fullscreen)
        view_menu.addSeparator()
        self._add_action(view_menu, "&Find in Page…", "Ctrl+F", self._open_find)
        self._add_action(view_menu, "Find &Next", "Ctrl+G", lambda: self._find_step(False))
        self._add_action(view_menu, "Find &Previous", "Ctrl+Shift+G", lambda: self._find_step(True))

        history_menu: QMenu = menubar.addMenu("&History")
        self._add_action(history_menu, "&Back", "Alt+Left", self._back)
        self._add_action(history_menu, "&Forward", "Alt+Right", self._forward)
        history_menu.addSeparator()
        self._add_action(history_menu, "Show &History", "Ctrl+H", self._show_history)

        bookmarks_menu: QMenu = menubar.addMenu("&Bookmarks")
        self._add_action(
            bookmarks_menu, "Bookmark This &Page", "Ctrl+D", self._toggle_bookmark
        )
        self._add_action(
            bookmarks_menu, "Show &Bookmarks", "Ctrl+Shift+O", self._show_bookmarks
        )

        tools_menu: QMenu = menubar.addMenu("&Tools")
        self._add_action(tools_menu, "&Settings…", "Ctrl+,", self._show_settings)
        tools_menu.addSeparator()
        self._agent_action = self._add_action(
            tools_menu, "Show &AI Agent", "Ctrl+Shift+A", self._toggle_agent_panel)
        self._agent_action.setCheckable(True)
        self._add_action(tools_menu, "&Configure AI Agent…", None, self._configure_agent)
        tools_menu.addSeparator()
        self._add_action(tools_menu, "&Mission Library", "Ctrl+Shift+M",
                         self._show_mission_library)
        self._teach_action = self._add_action(
            tools_menu, "&Teach Py", "Ctrl+Shift+T", self._toggle_teaching)
        self._teach_action.setCheckable(True)
        self._teach_action.setToolTip(
            "Record what Py does next as a reusable Routine on the active mission")

        help_menu: QMenu = menubar.addMenu("&Help")
        self._add_action(help_menu, f"&About {APP_NAME}", None, self._show_about)

    def _add_action(self, menu: QMenu, text: str, shortcut: str | None, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        # Adding to the window too keeps the shortcut alive when the menu bar is
        # hidden (macOS native menus, fullscreen).
        self.addAction(action)
        return action

    def _connect_signals(self) -> None:
        self.nav_bar.back_requested.connect(self._back)
        self.nav_bar.forward_requested.connect(self._forward)
        self.nav_bar.reload_requested.connect(self._reload)
        self.nav_bar.stop_requested.connect(self._stop)
        self.nav_bar.home_requested.connect(self._go_home)
        self.nav_bar.navigate_requested.connect(self._navigate_from_address_bar)
        self.nav_bar.bookmark_toggled.connect(self._toggle_bookmark)
        self.find_bar.search_requested.connect(self._run_find)
        self.find_bar.closed.connect(self._clear_find)

        self.tabs.current_url_changed.connect(self._on_url_changed)
        self.tabs.current_title_changed.connect(self._on_title_changed)
        self.tabs.current_load_started.connect(self._on_load_started)
        self.tabs.current_load_progress.connect(self._on_load_progress)
        self.tabs.current_load_finished.connect(self._on_load_finished)
        self.tabs.current_tab_switched.connect(self._on_tab_switched)
        self.tabs.status_message.connect(self._show_status)
        self.tabs.load_error.connect(self._on_load_error)
        self.tabs.security_message.connect(
            lambda text: self.notice.show_message(text, level="warning")
        )
        self.tabs.internal_action.connect(self._on_internal_action)
        self.tabs.page_visited.connect(self.history.add_visit)
        self.tabs.page_title_resolved.connect(self.history.update_title)
        # Closing the last tab closes the window, like Chrome.
        self.tabs.all_tabs_closed.connect(self.close)
        # A download that gives no feedback looks like a dead link.
        self._profile.download_started.connect(self._on_download_started)
        self._profile.download_finished.connect(self._on_download_finished)


    def _install_shortcuts(self) -> None:
        """Shortcuts that have no natural menu entry."""
        shortcuts = {
            "Ctrl+L": self._focus_address_bar,
            "Alt+D": self._focus_address_bar,
            "F6": self._focus_address_bar,
            "F5": self._reload,
            "Ctrl+Shift+R": self._hard_reload,
            "Ctrl+Tab": lambda: self.tabs.select_relative(1),
            "Ctrl+Shift+Tab": lambda: self.tabs.select_relative(-1),
            "Ctrl+F4": self.tabs.close_current_tab,
        }
        for key, slot in shortcuts.items():
            QShortcut(QKeySequence(key), self, activated=slot)
        # Ctrl+1..Ctrl+9 jump to a tab.
        for i in range(9):
            QShortcut(
                QKeySequence(f"Ctrl+{i + 1}"),
                self,
                activated=lambda index=i: self.tabs.select_index(index),
            )

    # ------------------------------------------------------------------
    # navigation
    # ------------------------------------------------------------------
    def _current(self):
        return self.tabs.current_tab()

    def _back(self) -> None:
        if tab := self._current():
            tab.back()

    def _forward(self) -> None:
        if tab := self._current():
            tab.forward()

    def _reload(self) -> None:
        if tab := self._current():
            tab.reload()

    def _hard_reload(self) -> None:
        self._profile.clear_http_cache()
        self._reload()

    def _stop(self) -> None:
        if tab := self._current():
            tab.stop()

    def _go_home(self) -> None:
        if tab := self._current():
            tab.navigate(self.settings.new_tab_url())

    # ------------------------------------------------------------------
    # The PyBrowser new-tab page
    # ------------------------------------------------------------------
    def _new_tab_data(self):
        """Build what the new-tab page shows, right now.

        Called by the scheme handler each time the page is served, so the lists
        are always current without any cache to invalidate.
        """
        from app.browser.newtab import collect

        return collect(self.history, self.bookmarks, self.missions,
                       agent_available=self._agent_configured(),
                       show_onboarding=self._show_onboarding())

    def _show_onboarding(self) -> bool:
        """Whether the first-launch explainer belongs on this new tab.

        Shown until the user either dismisses it or starts their first
        Mission - whichever comes first. Not a dedicated "have we ever
        launched before" flag: a mission already started is itself proof the
        user does not need the explainer, dismissed or not.
        """
        try:
            if self.settings.get_bool("onboarding_dismissed", False):
                return False
            return self.missions.store.count() == 0
        except Exception:  # noqa: BLE001 - a new tab must appear regardless
            return False

    def _agent_configured(self) -> bool:
        """Whether the AI entry point should promise anything.

        This deliberately does **not** ask the credential layer. An earlier
        version called `credentials.resolve()` here, which reads the OS
        keyring - and on a machine whose keyring backend is broken, the keyring
        library's Rust extension aborts the process outright rather than
        raising something Python can catch. Rendering a new tab must never be
        able to do that.

        So the answer comes from memory only: optimistic until we have actually
        watched the agent fail to start. The panel itself explains a missing
        credential properly; the new-tab page only sets an expectation.
        """
        return not self._agent_unavailable

    def _on_internal_action(self, name: str, params: dict) -> None:
        """Act on a request from one of our own pages.

        A page can only ask; this decides. Every action here is something the
        user could already do from a menu, and names are namespaced by page
        (`missions:open`) so one internal page cannot trigger another's.
        It never navigates by itself: the URL-or-search decision below is the
        same code path the address bar uses, so the two cannot drift apart.
        """
        if name.startswith("missions:"):
            self._on_mission_action(name.split(":", 1)[1], params)
            return
        if name == "search":
            query = (params.get("q") or "").strip()
            if query:
                self._navigate_from_address_bar(query)
        elif name == "open":
            target = (params.get("url") or "").strip()
            if target and (tab := self._current()):
                tab.navigate(target)
        elif name == "ai":
            self._open_agent_with(params.get("q") or "")
        elif name == "history":
            self._show_history()
        elif name == "bookmarks":
            self._show_bookmarks()
        elif name == "mission":
            mission_id = params.get("id")
            if mission_id:
                try:
                    self._open_mission(int(mission_id))
                except (TypeError, ValueError):
                    pass
        elif name == "dismiss-onboarding":
            self.settings.set_bool("onboarding_dismissed", True)
        elif name == "demo-mission":
            self.settings.set_bool("onboarding_dismissed", True)
            self._open_agent_with(
                "Compare a few noise-cancelling headphones under $150 and "
                "recommend one.")

    # ------------------------------------------------------------------
    # The Mission Library
    # ------------------------------------------------------------------
    def _mission_library_data(self, mission_id, query: str, view: str = ""):
        """What the library page shows. Reads; never writes."""
        from app.browser.missions_page import LibraryData, evidence_map, summarise

        try:
            if mission_id is not None and view == "evidence":
                mission = self.missions.store.get(int(mission_id))
                if mission is None:
                    return LibraryData(total=self.missions.store.count())
                return LibraryData(evidence=evidence_map(mission),
                                   total=self.missions.store.count())
            if mission_id is not None:
                mission = self.missions.store.get(int(mission_id))
                if mission is None:
                    return LibraryData(total=self.missions.store.count())
                return LibraryData(
                    detail=summarise(
                        mission, with_detail=True,
                        routines=self.routines.for_mission(mission.id),
                        children=self.missions.children(mission.id),
                        parent=self.missions.parent_of(mission.id),
                        ghost_runs=self.missions.ghost_runs(mission.id)),
                    total=self.missions.store.count())
            found = self.missions.search(query)
            store = self.missions.store
            return LibraryData(
                missions=[summarise(m,
                                    findings=store.finding_count(m.id),
                                    pages=store.page_count(m.id))
                          for m in found],
                query=query, total=store.count())
        except Exception:  # noqa: BLE001 - a page must not take the window down
            return LibraryData()

    def _show_mission_library(self) -> None:
        from app.browser.missions_page import LIBRARY_URL

        tab = self._current()
        if tab is not None and tab.url().toString().startswith(LIBRARY_URL):
            tab.reload()
            return
        self.tabs.new_tab(LIBRARY_URL)

    def _open_mission(self, mission_id: int) -> None:
        from app.browser.missions_page import mission_url

        tab = self._current()
        if tab is not None:
            tab.navigate(mission_url(mission_id))
        else:
            self.tabs.new_tab(mission_url(mission_id))

    def _on_mission_action(self, name: str, params: dict) -> None:
        """One request from the Mission Library page.

        Five verbs, each of them something the user could do from a menu.
        Destructive ones confirm in Qt, never in the page: a confirmation
        rendered by the thing being confirmed is not a confirmation.
        """
        from app.browser.missions_page import LIBRARY_URL

        identifier = (params.get("id") or "").strip()
        mission_id = int(identifier) if identifier.isdigit() else None

        if name == "library":
            if tab := self._current():
                tab.navigate(LIBRARY_URL)
        elif name == "search":
            query = (params.get("q") or "").strip()
            url = LIBRARY_URL + (f"?q={QUrl.toPercentEncoding(query).data().decode()}"
                                 if query else "")
            if tab := self._current():
                tab.navigate(url)
        elif name == "open" and mission_id is not None:
            self._open_mission(mission_id)
        elif name == "page":
            target = (params.get("url") or "").strip()
            if target and (tab := self._current()):
                tab.navigate(target)
        elif name == "resume" and mission_id is not None:
            self._resume_mission(mission_id)
        elif name == "rename" and mission_id is not None:
            self._rename_mission(mission_id)
        elif name == "delete" and mission_id is not None:
            self._delete_mission(mission_id)
        elif name == "edit-decision" and mission_id is not None:
            self._edit_decision(mission_id)
        elif name == "clear-decision" and mission_id is not None:
            self._clear_decision(mission_id)
        elif name == "branch" and mission_id is not None:
            self._branch_mission(mission_id)
        elif name == "routine-run":
            identifier = (params.get("id") or "").strip()
            if identifier.isdigit():
                self.run_routine(int(identifier))
        elif name == "ghost-run-clear":
            identifier = (params.get("id") or "").strip()
            if identifier.isdigit():
                self._clear_ghost_run(int(identifier))
        elif name == "evidence" and mission_id is not None:
            from app.browser.missions_page import evidence_url

            if tab := self._current():
                tab.navigate(evidence_url(mission_id))
        elif name == "challenge":
            target = (params.get("target") or "").strip()
            if target.isdigit():
                self.challenge_claim(params.get("kind") or "finding", int(target))

    def _resume_mission(self, mission_id: int) -> None:
        """Make a Mission active in this window, and show Py.

        Resuming is a different act from opening: opening looks at a Mission,
        resuming hands it to Py. Doing both on one click would hijack the
        agent's context every time someone browsed their own library.
        """
        mission = self.missions.resume(mission_id)
        if mission is None:
            self._show_status("That mission is no longer available.")
            return
        if self._side_panel is None:
            self._toggle_agent_panel()
        self._show_status(f"Mission resumed: {mission.title}")

    def _rename_mission(self, mission_id: int) -> None:
        from PySide6.QtWidgets import QInputDialog

        mission = self.missions.store.get(mission_id)
        if mission is None:
            return
        title, ok = QInputDialog.getText(self, "Rename mission", "Mission name:",
                                         text=mission.title)
        if ok and title.strip():
            self.missions.rename(mission_id, title)
            self._reload_mission_views(mission_id)

    def _delete_mission(self, mission_id: int) -> None:
        """Delete, with permanent deletion as a deliberate second choice.

        The default hides the Mission and keeps the record, because a Mission
        is the reasoning behind a decision and people ask about those months
        later. Someone who actually wants the data gone gets a button that
        says so.
        """
        from PySide6.QtWidgets import QMessageBox

        mission = self.missions.store.get(mission_id)
        if mission is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Delete mission")
        box.setText(f"Delete \u201c{mission.title}\u201d?")
        box.setInformativeText(
            "It is removed from your library. Its findings and pages are kept, "
            "so it can be brought back.")
        delete = box.addButton("Delete", QMessageBox.ButtonRole.AcceptRole)
        forever = box.addButton("Delete permanently", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is delete:
            self.missions.delete(mission_id)
        elif clicked is forever:
            confirm = QMessageBox.warning(
                self, "Delete permanently",
                f"Permanently delete \u201c{mission.title}\u201d and everything "
                "recorded in it? This cannot be undone.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel)
            if confirm != QMessageBox.StandardButton.Yes:
                return
            self.missions.delete(mission_id, permanent=True)
        else:
            return
        self._show_mission_library()

    def _edit_decision(self, mission_id: int) -> None:
        """Reword the decision or its rationale.

        Saving inserts a new decision and supersedes the old one - the same
        path the agent takes - so the record of what was previously decided
        survives being corrected. Evidence and alternatives are carried over,
        with each evidence snapshot re-taken from the finding it cites, because
        the edit is a fresh decision about the board as it stands now.
        """
        from PySide6.QtWidgets import QInputDialog

        decision = self.missions.decision(mission_id)
        if decision is None:
            return
        what, ok = QInputDialog.getText(self, "Decision", "Decided:",
                                        text=decision.decision)
        if not ok or not what.strip():
            return
        why, ok = QInputDialog.getMultiLineText(self, "Decision", "Why:",
                                                text=decision.rationale)
        if not ok or not why.strip():
            return
        store = self.missions.store
        # Only the finding ids that still exist can be re-cited; a snapshot
        # whose finding is gone stays on the superseded decision, where it
        # belongs, rather than being invented again here.
        evidence = [e.finding_id for e in decision.evidence if e.finding_id is not None]
        alternatives = [(a.name, a.reason) for a in decision.alternatives]
        store.save_decision(mission_id, what, why, evidence, alternatives)
        self.missions._refresh()
        self._reload_mission_views(mission_id)

    def _clear_decision(self, mission_id: int) -> None:
        from PySide6.QtWidgets import QMessageBox

        decision = self.missions.decision(mission_id)
        if decision is None:
            return
        answer = QMessageBox.question(
            self, "Clear decision",
            f"Clear the decision \u201c{decision.decision}\u201d?\n\n"
            "The mission keeps its findings, and the record that this was "
            "decided is kept too.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.missions.clear_decision(mission_id)
            self._reload_mission_views(mission_id)

    def _clear_ghost_run(self, ghost_run_id: int) -> None:
        """Remove one prediction. No confirmation: it never did anything, so
        there is nothing a confirmation would be protecting."""
        if self.missions.clear_ghost_run(ghost_run_id):
            self._reload_mission_views()

    def challenge_claim(self, target_kind: str, target_id: int) -> None:
        """Ask Py to try to prove one claim wrong.

        The target is recorded on the Mission service, not passed through the
        model: `mission_save_challenge` has no parameter naming a target, so
        Py can only report on whatever the user actually selected. The request
        itself is an ordinary message - the same path a quick action takes -
        so Challenge Mode adds no new way for the agent to reach the browser.
        """
        claim = self.missions.begin_challenge(target_kind, target_id)
        if not claim:
            self._show_status("That claim is no longer part of the active mission.")
            return
        noun = "decision" if target_kind == "decision" else "note"
        # Short and readable: how to challenge something is static guidance in
        # the system prompt, so the message the user sees in their own
        # transcript is the request they actually made, not a briefing.
        self._ask_py(f"Challenge this {noun}: {claim}")

    def _ask_py(self, text: str) -> None:
        """Open Py and send a prepared request.

        Unlike `_open_agent_with`, which writes into the box for the user to
        read and change, this sends: the user clicked a button on one specific
        claim, so there is nothing left to decide. It goes through the panel's
        ordinary ask path - the same one the quick actions use - so no new
        route into the agent exists.
        """
        if self._side_panel is None:
            self._toggle_agent_panel()
        panel = self._side_panel
        ask = getattr(panel, "ask", None)
        if callable(ask):
            ask(text)

    def _toggle_teaching(self) -> None:
        """Start or stop recording the agent's next actions as a Routine.

        Recording is a property of the live session, not of the Mission row:
        it captures whatever browser_* tools the agent runs from this point,
        through the same panel the user is already talking to Py in. Nothing
        about a step being recorded changes how it runs - it goes through the
        ordinary approval gate exactly as it would unrecorded.
        """
        mission = self.missions.active
        if mission is None:
            self._teach_action.setChecked(False)
            self._show_status("Start a mission before teaching Py a routine.")
            return
        if self.routines.is_recording:
            count = self.routines.recorded_count
            self._teach_action.setChecked(False)
            if not count:
                self.routines.discard_recording()
                self._show_status("Nothing was recorded.")
                return
            from PySide6.QtWidgets import QInputDialog

            name, ok = QInputDialog.getText(
                self, "Save routine", f"Name this routine ({count} step"
                f"{'s' if count != 1 else ''}):")
            if ok and name.strip():
                self.routines.stop_recording(name)
                self._show_status(f"Routine saved: {name.strip()}")
            else:
                self.routines.discard_recording()
                self._show_status("Routine discarded.")
            self._reload_mission_views(mission.id)
            return
        if self.routines.begin_recording(mission.id):
            self._teach_action.setChecked(True)
            if self._side_panel is None:
                self._toggle_agent_panel()
            self._show_status(
                "Teaching Py - ask it to do the task, then stop teaching to save it.")

    def run_routine(self, routine_id: int) -> None:
        """Run a saved Routine, filling in any variables first.

        Goes through AgentSession.run_routine, which shares its whole
        execution path with an ordinary model-issued tool call - the approval
        gate applies exactly as it would if the user had asked for each step
        by name.
        """
        from app.ui.routine_dialog import RoutineRunDialog

        routine = self.routines.get(routine_id)
        if routine is None:
            self._show_status("That routine is no longer available.")
            return
        if self._agent_session is None or self._agent_session.busy:
            self._show_status("Py is busy or not set up; cannot run a routine right now.")
            return
        overrides = RoutineRunDialog.ask(self, routine)
        if overrides is None:
            return
        if self._side_panel is None:
            self._toggle_agent_panel()
        panel = self._side_panel
        begin = getattr(panel, "begin_routine_run", None)
        if callable(begin):
            begin(routine.name)
        self._agent_session.run_routine(routine.resolve(overrides))

    def _branch_mission(self, mission_id: int) -> None:
        """Fork a Mission: an independent copy of its findings and decision.

        The two evolve separately from this point - editing or deleting
        something in one can never reach into the other, because branching
        copies rows rather than sharing them. See MissionStore.branch.
        """
        from PySide6.QtWidgets import QInputDialog

        mission = self.missions.store.get(mission_id)
        if mission is None:
            return
        name, ok = QInputDialog.getText(
            self, "Branch mission", "What distinguishes this branch?",
            text="")
        if not ok or not name.strip():
            return
        new_mission = self.missions.branch(mission_id, name)
        if new_mission is None:
            self._show_status("Could not branch that mission.")
            return
        self._show_status(f"Branched: {new_mission.title}")
        self._open_mission(new_mission.id)

    def _reload_mission_views(self, _mission_id: int = 0) -> None:
        """Re-render any tab currently showing the library."""
        from app.browser.missions_page import LIBRARY_URL

        for tab in self.tabs.tabs():
            if tab.url().toString().startswith(LIBRARY_URL):
                tab.reload()

    def _open_agent_with(self, text: str) -> None:
        """Open the AI panel, carrying whatever was typed on the new-tab page.

        This deliberately reuses the existing panel and session rather than
        starting anything of its own - there is one AI in this browser.
        """
        if self._side_panel is None:
            self._toggle_agent_panel()
        panel = self._side_panel
        if panel is None:
            return
        box = getattr(panel, "input", None)
        if box is not None and box.isEnabled():
            if text:
                box.setPlainText(text)
                cursor = box.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                box.setTextCursor(cursor)
            box.setFocus()
        # Py acknowledges being summoned. Deliberately only a look, not a send:
        # the request is written out for the user to read and change, because
        # sending it for them would be the browser deciding what they meant.
        mascot = getattr(panel, "mascot", None)
        if mascot is not None and text:
            from app.ui.mascot import MascotState

            mascot.set_state(MascotState.THINKING)

    def _navigate_from_address_bar(self, text: str) -> None:
        url = url_utils.normalize(text, self.settings.search_url)
        tab = self._current() or self.tabs.new_tab()
        tab.navigate(url)
        tab.view.setFocus()

    def _focus_address_bar(self) -> None:
        self.nav_bar.address_bar.focus_and_select()

    # -- find in page ----------------------------------------------------
    def _open_find(self) -> None:
        self.find_bar.open_bar()

    def _run_find(self, text: str, backward: bool) -> None:
        """Search the current tab, ignoring results from superseded searches.

        Searching as the user types starts a new search on every keystroke, and
        Qt reports the *cancelled* one with zero matches. Those late zeros
        arrive after the real answer and would leave the bar saying "No
        results" for a phrase that is plainly on the page, so each search
        carries a sequence number and only the newest one may update the count.
        """
        tab = self._current()
        if tab is None:
            return
        self._find_sequence += 1
        token = self._find_sequence
        if not text:
            tab.find_text("")          # clears the highlight
            self.find_bar.report(0, 0)
            return

        def report(active: int, total: int, retried: bool = False) -> None:
            if token != self._find_sequence:
                return                      # a newer search has superseded this
            if total == 0 and not retried:
                # Qt's first findText after focus moves into the find field
                # reports zero for a phrase that is plainly on the page, and an
                # immediate second call reports zero too - it needs a turn of
                # the event loop. Re-issue once, shortly, before believing a
                # zero. If the phrase genuinely is not there the retry also
                # returns zero and we report that honestly.
                QTimer.singleShot(120, lambda: tab.find_text(
                    text, backward, lambda a, t: report(a, t, retried=True)))
                return
            self.find_bar.report(active, total)

        tab.find_text(text, backward, report)

    def _find_step(self, backward: bool) -> None:
        if not self.find_bar.isVisible():
            self._open_find()
            return
        self._run_find(self.find_bar.field.text(), backward)

    def _clear_find(self) -> None:
        tab = self._current()
        if tab is not None:
            tab.find_text("")

    def _zoom(self, delta: float | None) -> None:
        tab = self._current()
        if tab is None:
            return
        tab.set_zoom(1.0 if delta is None else tab.zoom() + delta)
        self._show_status(f"Zoom {int(tab.zoom() * 100)}%")

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _open_new_window(self) -> None:
        window = MainWindow(self._profile, self._db)
        window.show()
        # Windows are owned by the application; keep a reference so Python's GC
        # does not close them the moment this method returns.
        _OPEN_WINDOWS.append(window)
        window.destroyed.connect(lambda: _OPEN_WINDOWS.remove(window) if window in _OPEN_WINDOWS else None)

    # ------------------------------------------------------------------
    # reacting to the current tab
    # ------------------------------------------------------------------
    def _on_url_changed(self, url: QUrl) -> None:
        self.nav_bar.set_url_text(url_utils.display_text(url))
        self._sync_navigation_state()
        self.nav_bar.set_bookmarked(self.bookmarks.contains(url.toString()))

    def _on_title_changed(self, title: str) -> None:
        self.setWindowTitle(f"{title} — {APP_NAME}" if title else APP_NAME)

    def _on_load_started(self) -> None:
        self.notice.hide()
        self.nav_bar.set_loading(True)
        self._progress.setValue(0)
        self._progress.show()
        self._show_status("Loading…")

    def _on_load_progress(self, progress: int) -> None:
        self._progress.setValue(progress)

    def _on_load_finished(self, ok: bool) -> None:
        self.nav_bar.set_loading(False)
        self._progress.hide()
        self._sync_navigation_state()
        tab = self._current()
        if tab is not None:
            self.nav_bar.set_url_text(url_utils.display_text(tab.url()))
            self.nav_bar.set_bookmarked(self.bookmarks.contains(tab.url().toString()))
            self._on_title_changed(tab.title())
        # A refused action URL is not a failed page: the new-tab page asks for
        # things by navigating, and we decline the navigation on purpose.
        refused = tab is not None and tab.load_was_refused_action
        self._show_status("" if ok or refused else "Page failed to load")
        # Feed the address bar's autocomplete from recent history.
        self.nav_bar.set_completions([entry.url for entry in self.history.recent(50)])

    def _on_tab_switched(self, loading: bool) -> None:
        """Re-point the chrome at the newly selected tab without faking a load."""
        self.notice.hide()
        self.nav_bar.set_loading(loading)
        self._progress.setVisible(loading)
        tab = self._current()
        if tab is not None:
            self.nav_bar.set_url_text(url_utils.display_text(tab.url()))
            self.nav_bar.set_bookmarked(self.bookmarks.contains(tab.url().toString()))
            self._on_title_changed(tab.title())
        self._sync_navigation_state()

    def _sync_navigation_state(self) -> None:
        tab = self._current()
        if tab is None:
            self.nav_bar.set_navigation_state(False, False)
            return
        self.nav_bar.set_navigation_state(tab.can_go_back(), tab.can_go_forward())

    def _show_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _on_load_error(self, error: LoadError) -> None:
        """Surface a failed load in plain language.

        Chromium already renders its own error page in the viewport; the notice
        bar adds a one-line explanation and, where it makes sense, a Retry
        button. Technical detail (ERR_... and the numeric code) goes in the
        tooltip only - never in the user-facing sentence.
        """
        retry = None if error.category == ErrorCategory.CERTIFICATE else self._reload
        self.notice.show_message(
            error.message,
            tooltip=f"{error.url}\n{error.technical}",
            action_text=None if retry is None else "Retry",
            action=retry,
        )
        self._show_status(error.message)

    # ------------------------------------------------------------------
    # bookmarks / history UI
    # ------------------------------------------------------------------
    def _toggle_bookmark(self) -> None:
        tab = self._current()
        if tab is None:
            return
        url = tab.url().toString()
        if not url or url == "about:blank":
            return
        now_bookmarked = self.bookmarks.toggle(url, tab.title())
        self.nav_bar.set_bookmarked(now_bookmarked)
        self._show_status("Bookmark added" if now_bookmarked else "Bookmark removed")

    def _on_download_started(self, item) -> None:
        self.notice.show_message(
            f"Downloading {item.file_name} — see Downloads (Ctrl+J)")

    def _on_download_finished(self, item) -> None:
        if item.state == "completed":
            self._show_status(f"Finished downloading {item.file_name}")
        elif item.state == "interrupted":
            # A failed download must say so; silence looks like a dead link.
            self.notice.show_message(
                f"Download failed: {item.file_name}"
                + (f" — {item.reason}" if item.reason else ""),
                level="warning")

    def _show_downloads(self) -> None:
        from app.ui.downloads_panel import DownloadsDialog

        DownloadsDialog(self._profile.downloads, self).exec()

    def _show_history(self) -> None:
        dialog = HistoryDialog(self.history, self)
        dialog.open_requested.connect(lambda url: self.tabs.new_tab(url))
        dialog.exec()

    def _show_bookmarks(self) -> None:
        dialog = BookmarksDialog(self.bookmarks, self)
        dialog.open_requested.connect(lambda url: self.tabs.new_tab(url))
        dialog.exec()

    def _show_settings(self) -> None:
        from app.ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.settings, self)
        dialog.saved.connect(self._apply_settings)
        dialog.exec()

    def _apply_settings(self) -> None:
        """Adopt changed preferences without a restart.

        Only the tab manager holds a copy of the new-tab address, so this is
        the whole of it - everything else reads the store when it needs to.
        """
        self.tabs.home_url = self.settings.new_tab_url()
        self._show_status("Settings saved.")

    def _show_about(self) -> None:
        from PySide6.QtCore import qVersion

        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> — the browser that finishes internet tasks.<br>"
            "Browse normally, or give Py a goal and let it research, compare, "
            "and act across the web.<br><br>"
            "Built with Python and Qt WebEngine.<br><br>"
            f"Qt {qVersion()}",
        )

    # ------------------------------------------------------------------
    # The AI agent panel
    # ------------------------------------------------------------------
    def _toggle_agent_panel(self) -> None:
        """Show or hide the agent. Built lazily, on first use."""
        if self._side_panel is not None:
            self.set_side_panel(None)
            self._agent_action.setChecked(False)
            return
        from app.ui.agent_panel import AgentPanel
        from app.ui.agent_setup import build_session

        if self._agent_session is None:
            self._agent_session, reason = build_session(
                self.controller, self, self.settings, self.missions)
            if self._agent_session is None:
                self._agent_unavailable = True
                self._show_status(f"AI agent unavailable: {reason}")
            else:
                self._agent_unavailable = False
                self._agent_session.briefing_provider = self.missions.briefing
                self._agent_session.step_recorder = self.routines.record_step
                self._agent_session.step_changed.connect(self.missions.record_agent_step)
                self._agent_session.state_changed.connect(self.missions.on_agent_state_changed)
                credential = self._current_credential(self._agent_session.config.provider)
                self._credential_id = credential.fingerprint if credential else ""
        self.set_side_panel(AgentPanel(self._agent_session, self, self.missions))
        self._agent_action.setChecked(True)

    def _configure_agent(self) -> None:
        from app.ui.agent_setup import ApiKeyDialog

        ApiKeyDialog(self, self.settings).exec()
        self._apply_agent_settings()

    def _apply_agent_settings(self) -> None:
        """Adopt changed AI configuration immediately, without a restart.

        Three things used to make a new API key need a browser restart, and
        all three are fixed here:

        1. This returned early when there was no session. That is exactly the
           case where a key has just been added for the first time - the
           session is None *because* there was no credential a moment ago, so
           the one moment it mattered was the one moment it did nothing.
        2. It compared the model and the effort but not the credential, so
           swapping a key on a running session changed nothing either.
        3. `_agent_unavailable` was never cleared, so the new-tab page went on
           saying the agent was not set up.

        Rebuilding rather than mutating the live session is deliberate: the
        client holds an SDK connection built from the credential and a prompt
        cache scoped to the model, and a fresh session is the honest way to
        change either. It costs the conversation, which is why it only happens
        when something actually changed.
        """
        from app.agent.config import AgentConfig

        wanted = AgentConfig.from_environment(self.settings)
        credential = self._current_credential(wanted.provider)
        session = self._agent_session

        if session is None:
            # Nothing running. If a credential has appeared, the panel is
            # showing "not set up yet" and needs rebuilding; otherwise there is
            # nothing to do.
            if credential is not None and credential.available and self._agent_unavailable:
                self._agent_unavailable = False
                self._rebuild_agent("Py is ready.")
            return

        unchanged = (
            (wanted.provider, wanted.model, wanted.effort, wanted.workspace_id)
            == (session.config.provider, session.config.model, session.config.effort,
                session.config.workspace_id)
            and (credential is None or credential.fingerprint == self._credential_id)
        )
        if unchanged:
            return
        if session.busy:
            self._show_status("The new AI settings apply once this task finishes.")
            return
        self._rebuild_agent(f"Py now using {wanted.model_choice.label}.")

    def _current_credential(self, provider: str | None = None):
        """The credential as it stands right now, or None if it cannot be read.

        Separate so the failure is one place: reading a credential touches the
        OS keyring, and a browser must not fall over because a keyring is
        broken. ``provider`` defaults to whatever is currently configured, so
        existing callers that do not care which provider is active keep
        working unchanged.
        """
        try:
            from app.agent.config import AgentConfig
            from app.agent.credentials import resolve_for

            if provider is None:
                provider = AgentConfig.from_environment(self.settings).provider
            return resolve_for(provider)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return None

    def _rebuild_agent(self, message: str) -> None:
        """Throw the agent session away and build a fresh one.

        Rebuilds the panel too if it is open, so the user sees the change land
        rather than being told to reopen something.
        """
        showing = self._side_panel is not None
        if showing:
            self.set_side_panel(None)
        if self._agent_session is not None:
            self._agent_session.shutdown()
            self._agent_session = None
        self._credential_id = ""
        self._show_status(message)
        if showing:
            self._toggle_agent_panel()

    # ------------------------------------------------------------------
    # Panel plumbing
    # ------------------------------------------------------------------
    def set_side_panel(self, panel: QWidget | None) -> None:
        """Install (or remove) the right-hand panel, with a short slide.

        The panel's width is a share of the window rather than a constant: 380
        pixels is comfortable at 1400 and takes almost half the window at 900,
        where it stops being a side panel and starts being the application.
        """
        from app.ui import theme

        if self._side_panel is not None:
            self._animate_panel(closing=True)
            self._side_panel.setParent(None)
            self._side_panel.deleteLater()
            self._side_panel = None
        if panel is not None:
            m = theme.METRICS
            width = max(m.panel_min, min(m.panel_default,
                                         int(self.width() * m.panel_max_share)))
            self.splitter.addWidget(panel)
            self._side_panel = panel
            # Py shrinks a little in a narrow panel, where a 40px character
            # next to a 300px column starts to crowd the header.
            mascot = getattr(panel, "mascot", None)
            if mascot is not None:
                mascot.set_size(m.mascot_panel if width >= 340 else m.mascot_panel_small)
            self.splitter.setSizes([self.width() - width, width])
            self._animate_panel(closing=False, width=width)

    def _animate_panel(self, *, closing: bool, width: int = 0) -> None:
        """Slide the panel in or out.

        Short and only on this one transition: a panel appearing instantly
        makes the whole window look like it jumped, and that is the only place
        in this browser where motion earns its cost. Anything longer than
        about a sixth of a second reads as waiting rather than as movement.
        """
        if self._reduced_motion():
            return
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        start, end = (width, 0) if closing else (0, width)
        animation = QVariantAnimation(self)
        animation.setDuration(150)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def step(value) -> None:
            if self.splitter.count() < 2:
                return
            self.splitter.setSizes([max(0, self.width() - int(value)), int(value)])

        animation.valueChanged.connect(step)
        # Held on the window so Python does not collect it mid-flight.
        self._panel_animation = animation
        animation.start()

    @staticmethod
    def _reduced_motion() -> bool:
        """Honour a request for less motion, however it was made.

        Qt has no cross-platform "reduce motion" query, so this reads the
        environment variable the freedesktop and Qt tooling both use, plus our
        own. Someone who asked for less animation should not have to ask twice.
        """
        import os

        for name in ("PYBROWSER_REDUCED_MOTION", "QT_REDUCED_MOTION", "NO_ANIMATIONS"):
            if (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on"):
                return True
        return False

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        # Stop the agent's worker thread before the window goes away.
        if self._agent_session is not None:
            self._agent_session.shutdown()
            self._agent_session = None
        # Tear down render processes explicitly; otherwise Qt can emit warnings
        # about pages outliving their profile during interpreter shutdown.
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        super().closeEvent(event)


class NoticeBar(QFrame):
    """A thin, dismissible message strip shown above the page.

    Used for things a user must actually notice - a blocked certificate, a
    failed load, a crashed page - which a status-bar line is too easy to miss.
    """

    _STYLES = {
        "info": "background:#e8f0fe; color:#1a3a6b; border-bottom:1px solid #c6d9f7;",
        "warning": "background:#fdf1d6; color:#6b4e00; border-bottom:1px solid #f0d79a;",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.ui import theme

        m = theme.METRICS
        self._action = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_1, m.space_3, m.space_1)
        layout.setSpacing(m.space_2)

        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)

        self._action_button = QPushButton("", self)
        self._action_button.setFlat(True)
        self._action_button.clicked.connect(self._on_action)
        self._action_button.hide()
        layout.addWidget(self._action_button)

        close_button = QPushButton("✕", self)
        close_button.setFlat(True)
        close_button.setFixedWidth(24)
        close_button.setToolTip("Dismiss")
        close_button.clicked.connect(self.hide)
        layout.addWidget(close_button)

        self.hide()

    def show_message(
        self,
        text: str,
        *,
        tooltip: str = "",
        level: str = "info",
        action_text: str | None = None,
        action=None,
    ) -> None:
        self._label.setText(text)
        self._label.setToolTip(tooltip)
        self.setStyleSheet(self._STYLES.get(level, self._STYLES["info"]))
        self._action = action
        if action_text and action is not None:
            self._action_button.setText(action_text)
            self._action_button.show()
        else:
            self._action_button.hide()
        self.show()

    def _on_action(self) -> None:
        action, self._action = self._action, None
        self.hide()
        if action is not None:
            action()


# Extra windows opened with Ctrl+N live here so they are not garbage collected.
_OPEN_WINDOWS: list[MainWindow] = []

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
        # The supported programmatic interface to this window. The UI does not
        # need it, but keeping one audited control surface (rather than letting
        # callers poke at widgets) is what Phase 2 will build on.
        self.controller = BrowserController(self.tabs, self)

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
        self._profile.download_started.connect(
            lambda dl: self.notice.show_message(
                f"Downloading {dl.downloadFileName()} to {dl.downloadDirectory()}"
            )
        )
        self._profile.download_finished.connect(
            lambda dl: self._show_status(f"Finished downloading {dl.downloadFileName()}")
        )

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

        return collect(self.history, self.bookmarks,
                       agent_available=self._agent_configured())

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
        """Act on a request from the new-tab page.

        The page can only ask for these five things, and each is something the
        user could already do from a menu. It never navigates by itself: the
        URL-or-search decision below is the same code path the address bar
        uses, so the two cannot drift apart.
        """
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
        self._show_status("" if ok else "Page failed to load")
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
            f"<b>{APP_NAME}</b><br>"
            "A desktop web browser built with Python and Qt WebEngine.<br><br>"
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
                self.controller, self, self.settings)
            if self._agent_session is None:
                self._agent_unavailable = True
                self._show_status(f"AI agent unavailable: {reason}")
        self.set_side_panel(AgentPanel(self._agent_session, self))
        self._agent_action.setChecked(True)

    def _configure_agent(self) -> None:
        from app.ui.agent_setup import ApiKeyDialog

        ApiKeyDialog(self, self.settings).exec()
        self._apply_agent_settings()

    def _apply_agent_settings(self) -> None:
        """Rebuild the agent if the model or effort preference changed.

        Both settings are fixed for the life of a session rather than read per
        request, and that is deliberate: the prompt cache is scoped to the model
        and is invalidated by a change of effort, so switching either one
        part-way through a conversation would silently discard everything
        cached so far - the opposite of the saving the settings exist to make.
        Rebuilding starts a fresh conversation, which is the honest way to
        change them.
        """
        session = self._agent_session
        if session is None:
            return
        from app.agent.config import AgentConfig

        wanted = AgentConfig.from_environment(self.settings)
        if (wanted.model, wanted.effort) == (session.config.model, session.config.effort):
            return
        if session.busy:
            self._show_status(
                "The new model applies once the current task finishes.")
            return

        showing = self._side_panel is not None
        if showing:
            self.set_side_panel(None)
        session.shutdown()
        self._agent_session = None
        self._show_status(f"AI agent now using {wanted.model_choice.label}.")
        if showing:
            self._toggle_agent_panel()
        else:
            self._agent_action.setChecked(False)

    # ------------------------------------------------------------------
    # Panel plumbing
    # ------------------------------------------------------------------
    def set_side_panel(self, panel: QWidget | None) -> None:
        """Install (or remove) the right-hand panel.

        Phase 2 will call this with the AI agent widget. Nothing else in the
        window needs to change.
        """
        if self._side_panel is not None:
            self._side_panel.setParent(None)
            self._side_panel.deleteLater()
            self._side_panel = None
        if panel is not None:
            self.splitter.addWidget(panel)
            self.splitter.setSizes([self.width() - 380, 380])
            self._side_panel = panel

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
        self._action = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 6, 5)
        layout.setSpacing(8)

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

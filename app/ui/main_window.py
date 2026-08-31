"""The browser window: chrome, menus, shortcuts, and the wiring between them.

Layout is deliberately built around a horizontal QSplitter with the tab area on
the left and an empty, hidden slot on the right. Phase 2 drops the AI agent
panel into that slot without touching any of this code's structure.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QWidget,
)

from app import APP_NAME
from app.browser.profile import BrowserProfile
from app.browser.tab_manager import TabManager
from app.storage import BookmarkStore, Database, HistoryStore, SettingsStore
from app.ui.dialogs import BookmarksDialog, HistoryDialog
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

        self.tabs = TabManager(profile, self.settings.home_url, self)

        # Splitter: [ tabs | side panel ]. The side panel is empty in Phase 1.
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.tabs)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self._side_panel: QWidget | None = None

        self._build_status_bar()
        self._build_menus()
        self._connect_signals()
        self._install_shortcuts()

        for url in start_urls or [self.settings.home_url]:
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

        self.tabs.current_url_changed.connect(self._on_url_changed)
        self.tabs.current_title_changed.connect(self._on_title_changed)
        self.tabs.current_load_started.connect(self._on_load_started)
        self.tabs.current_load_progress.connect(self._on_load_progress)
        self.tabs.current_load_finished.connect(self._on_load_finished)
        self.tabs.status_message.connect(self._show_status)
        self.tabs.page_visited.connect(self.history.add_visit)
        self.tabs.page_title_resolved.connect(self.history.update_title)
        # Closing the last tab closes the window, like Chrome.
        self.tabs.all_tabs_closed.connect(self.close)

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
            tab.navigate(self.settings.home_url)

    def _navigate_from_address_bar(self, text: str) -> None:
        url = url_utils.normalize(text, self.settings.search_url)
        tab = self._current() or self.tabs.new_tab()
        tab.navigate(url)
        tab.view.setFocus()

    def _focus_address_bar(self) -> None:
        self.nav_bar.address_bar.focus_and_select()

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

    def _sync_navigation_state(self) -> None:
        tab = self._current()
        if tab is None:
            self.nav_bar.set_navigation_state(False, False)
            return
        self.nav_bar.set_navigation_state(tab.can_go_back(), tab.can_go_forward())

    def _show_status(self, message: str) -> None:
        self._status_label.setText(message)

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
    # Phase 2 hook: a place for the AI agent panel
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
        # Tear down render processes explicitly; otherwise Qt can emit warnings
        # about pages outliving their profile during interpreter shutdown.
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        super().closeEvent(event)


# Extra windows opened with Ctrl+N live here so they are not garbage collected.
_OPEN_WINDOWS: list[MainWindow] = []

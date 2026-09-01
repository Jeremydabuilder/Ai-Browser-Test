"""The tab strip: a QTabWidget that owns BrowserTab instances."""

from __future__ import annotations

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtWidgets import QTabBar, QTabWidget, QWidget

from app.browser.profile import BrowserProfile
from app.browser.tab import BrowserTab

# Tab labels get elided so one long page title cannot eat the whole strip.
_MAX_TITLE_CHARS = 24


class TabManager(QTabWidget):
    """Creates, closes and tracks tabs, and forwards the *current* tab's signals.

    The main window subscribes to this class rather than to individual tabs, so
    it does not need to connect and disconnect handlers every time the user
    switches tab.
    """

    current_url_changed = Signal(QUrl)
    current_title_changed = Signal(str)
    current_load_started = Signal()
    current_load_progress = Signal(int)
    current_load_finished = Signal(bool)
    status_message = Signal(str)
    # A load failure on the *current* tab, already translated for humans.
    load_error = Signal(object)      # LoadError
    security_message = Signal(str)   # blocked certificate, crashed renderer
    # Fired for any tab that finishes loading - history listens to this.
    page_visited = Signal(str, str)   # url, title
    page_title_resolved = Signal(str, str)
    all_tabs_closed = Signal()
    # An action requested by the new-tab page in any tab.
    internal_action = Signal(str, dict)
    # Emitted when the user switches tab; payload is that tab's loading state.
    current_tab_switched = Signal(bool)

    def __init__(self, profile: BrowserProfile, home_url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._profile = profile
        self._home_url = home_url

        self.setDocumentMode(True)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabCloseRequested.connect(self.close_tab)
        self.currentChanged.connect(self._on_current_changed)

    # -- properties -----------------------------------------------------
    @property
    def home_url(self) -> str:
        return self._home_url

    @home_url.setter
    def home_url(self, value: str) -> None:
        self._home_url = value

    def current_tab(self) -> BrowserTab | None:
        widget = self.currentWidget()
        return widget if isinstance(widget, BrowserTab) else None

    def tabs(self) -> list[BrowserTab]:
        return [w for i in range(self.count()) if isinstance(w := self.widget(i), BrowserTab)]

    # -- creation -------------------------------------------------------
    def new_tab(
        self,
        url: QUrl | str | None = None,
        *,
        background: bool = False,
        tab: BrowserTab | None = None,
    ) -> BrowserTab:
        """Add a tab. Pass ``tab`` to adopt a tab the engine already created."""
        if tab is None:
            tab = BrowserTab(self._profile)
        self._connect_tab(tab)
        index = self.addTab(tab, "New Tab")
        self._install_close_button(index, tab)
        if not background:
            self.setCurrentIndex(index)
        if url is not None:
            tab.navigate(url)
        elif tab.url().isEmpty():
            tab.navigate(self._home_url)
        return tab

    def _install_close_button(self, index: int, tab: BrowserTab) -> None:
        """Put our own close glyph on the tab.

        Qt asks the desktop for the close icon, and on a machine with no icon
        theme the fallback is a red X that reads as an error rather than a
        control. A plain tool button with our own glyph looks the same
        everywhere.
        """
        try:
            from PySide6.QtWidgets import QApplication, QToolButton

            from app.ui import icons, theme

            colours = theme.palette_for(QApplication.instance())
            button = QToolButton(self)
            button.setIcon(icons.icon("close", colours.muted, size=32, weight=2.4))
            button.setIconSize(QSize(13, 13))
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.ArrowCursor)
            button.setToolTip("Close tab")
            button.setStyleSheet(
                "QToolButton { border: none; border-radius: 5px; padding: 2px; }"
                f"QToolButton:hover {{ background: {colours.line}; }}")
            # Look the tab up when clicked: indexes shift as tabs come and go,
            # so capturing the index here would close the wrong tab later.
            button.clicked.connect(lambda _=False, t=tab: self._close_widget(t))
            self.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, button)
        except Exception:  # noqa: BLE001 - styling must never break tabs
            pass

    def _close_widget(self, tab: BrowserTab) -> None:
        index = self.indexOf(tab)
        if index != -1:
            self.close_tab(index)

    def _connect_tab(self, tab: BrowserTab) -> None:
        tab.title_changed.connect(lambda title, t=tab: self._on_tab_title(t, title))
        tab.icon_changed.connect(lambda icon, t=tab: self._on_tab_icon(t, icon))
        tab.url_changed.connect(lambda url, t=tab: self._on_tab_url(t, url))
        tab.load_started.connect(lambda t=tab: self._forward_if_current(t, self.current_load_started))
        tab.load_progress.connect(lambda p, t=tab: self._forward_if_current(t, self.current_load_progress, p))
        tab.load_finished.connect(lambda ok, t=tab: self._on_tab_load_finished(t, ok))
        tab.status_message.connect(self.status_message)
        tab.internal_action.connect(self.internal_action)
        tab.load_error.connect(lambda err, t=tab: self._forward_if_current(t, self.load_error, err))
        tab.page.certificate_rejected.connect(
            lambda host, desc, t=tab: self._on_security_event(
                t, f"Blocked {host}: its security certificate could not be trusted."
            )
        )
        tab.page.render_process_crashed.connect(
            lambda msg, t=tab: self._on_security_event(t, msg)
        )
        # A tab the engine spawned (window.open / target=_blank) arrives here.
        tab.new_tab_requested.connect(self._adopt_engine_tab)

    def _on_security_event(self, tab: BrowserTab, message: str) -> None:
        """Security events are shown even for a background tab, but labelled."""
        if tab is self.current_tab():
            self.security_message.emit(message)
        else:
            index = self.indexOf(tab)
            label = self.tabText(index) if index != -1 else "a background tab"
            self.security_message.emit(f"{message} (in {label})")

    def _adopt_engine_tab(self, tab: BrowserTab) -> None:
        self.new_tab(tab=tab)

    # -- per-tab signal handling ---------------------------------------
    def _forward_if_current(self, tab: BrowserTab, signal, *args) -> None:
        if tab is self.current_tab():
            signal.emit(*args)

    def _on_tab_title(self, tab: BrowserTab, title: str) -> None:
        index = self.indexOf(tab)
        if index == -1:
            return
        if tab.url().scheme() == "pybrowser":
            self.setTabText(index, "New Tab")
            self.setTabToolTip(index, "New Tab")
            if tab is self.current_tab():
                self.current_title_changed.emit("New Tab")
            return
        label = title or tab.url().host() or "New Tab"
        self.setTabText(index, self._elide(label))
        self.setTabToolTip(index, title or tab.url().toString())
        if tab is self.current_tab():
            self.current_title_changed.emit(title)
        url = tab.url().toString()
        if title and url:
            self.page_title_resolved.emit(url, title)

    def _on_tab_icon(self, tab: BrowserTab, icon) -> None:
        index = self.indexOf(tab)
        if index != -1:
            self.setTabIcon(index, icon)

    def _on_tab_url(self, tab: BrowserTab, url: QUrl) -> None:
        if tab is self.current_tab():
            self.current_url_changed.emit(url)

    def _on_tab_load_finished(self, tab: BrowserTab, ok: bool) -> None:
        if ok:
            self.page_visited.emit(tab.url().toString(), tab.title())
        self._forward_if_current(tab, self.current_load_finished, ok)

    def _on_current_changed(self, index: int) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        # Re-sync the chrome with whatever the newly selected tab is showing.
        # Note we do NOT re-emit current_load_finished here: switching tabs is
        # not a load, and faking one made the window act as though the page had
        # just finished loading (resetting progress and the reload/stop button).
        self.current_url_changed.emit(tab.url())
        self.current_title_changed.emit(tab.title())
        self.current_tab_switched.emit(tab.is_loading)
        tab.view.setFocus()

    # -- closing --------------------------------------------------------
    def close_tab(self, index: int) -> None:
        widget = self.widget(index)
        if not isinstance(widget, BrowserTab):
            return
        self.removeTab(index)
        # Deleting the page tears down the render process for that tab.
        widget.page.deleteLater()
        widget.deleteLater()
        if self.count() == 0:
            self.all_tabs_closed.emit()

    def close_current_tab(self) -> None:
        if self.count():
            self.close_tab(self.currentIndex())

    def select_relative(self, delta: int) -> None:
        if self.count() < 2:
            return
        self.setCurrentIndex((self.currentIndex() + delta) % self.count())

    def select_index(self, index: int) -> None:
        """Select the nth tab; index 8 (Ctrl+9) means "last tab", like Chrome."""
        if index == 8:
            self.setCurrentIndex(self.count() - 1)
        elif 0 <= index < self.count():
            self.setCurrentIndex(index)

    @staticmethod
    def _elide(text: str) -> str:
        text = text.strip()
        if len(text) <= _MAX_TITLE_CHARS:
            return text
        return text[: _MAX_TITLE_CHARS - 1].rstrip() + "…"

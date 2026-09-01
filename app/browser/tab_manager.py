"""The tab strip: a QTabWidget that owns BrowserTab instances."""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import QTabBar, QTabWidget, QToolButton, QWidget

from app.browser.profile import BrowserProfile
from app.browser.tab import BrowserTab

# Tab labels get elided so one long page title cannot eat the whole strip.
# Qt elides too, but only once the strip is full; doing it here keeps a tab
# from being wider than it needs to be while there is still room.
_MAX_TITLE_CHARS = 24

#: How fast the loading indicator turns. Slow enough not to draw the eye away
#: from the page, fast enough to read as "working".
_SPIN_INTERVAL_MS = 90


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
        self.setUsesScrollButtons(True)
        # Without this Qt stretches tabs to fill the strip, so two tabs each
        # take half the window and look nothing like tabs.
        self.tabBar().setExpanding(False)
        self.tabBar().setDrawBase(False)
        self._new_tab_button = None
        #: True while the "+" is parked in the corner slot because the tab
        #: strip has run out of room. See _position_new_tab_button.
        self._button_in_corner = False
        self._install_new_tab_button()

        # One timer drives the loading indicator on every tab, and only while
        # something is loading - a timer ticking behind an idle browser costs
        # battery for nothing.
        self._spin_angle = 0
        self._spin_base = None
        self._page_icon = None
        self._spinner = QTimer(self)
        self._spinner.setInterval(_SPIN_INTERVAL_MS)
        self._spinner.timeout.connect(self._advance_spinner)

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
        self.setTabIcon(index, self._fallback_icon())
        self._install_close_button(index, tab)
        if not background:
            self.setCurrentIndex(index)
        if url is not None:
            tab.navigate(url)
        elif tab.url().isEmpty():
            tab.navigate(self._home_url)
        return tab

    def _install_new_tab_button(self) -> None:
        """A "+" that sits immediately after the last tab.

        A corner widget was the easy way to do this and looked wrong: pinned to
        the far right of the strip, it read as an unrelated toolbar button
        floating in whitespace rather than part of the tabs. Placing it as a
        child of the tab bar and moving it after the last tab keeps it attached
        to them, and it steps aside to the corner when the strip fills up -
        which is exactly where it can still be reached once the tabs scroll.
        """
        try:
            from PySide6.QtWidgets import QApplication

            from app.ui import icons, theme

            colours = theme.palette_for(QApplication.instance())
            m = theme.METRICS
            bar = self.tabBar()
            button = QToolButton(bar)
            button.setIcon(icons.icon("plus", colours.muted, size=32, weight=2.1))
            button.setIconSize(QSize(m.icon_sm, m.icon_sm))
            button.setFixedSize(m.tab - 8, m.tab - 8)
            button.setAutoRaise(True)
            button.setToolTip("New tab (Ctrl+T)")
            button.setAccessibleName("New tab")
            button.setCursor(Qt.CursorShape.ArrowCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setStyleSheet(
                f"QToolButton {{ border: 1px solid transparent;"
                f" border-radius: {m.radius_sm}px; background: transparent; }}"
                f"QToolButton:hover {{ background: {colours.surface_hover};"
                f" border-color: {colours.line}; }}"
                f"QToolButton:pressed {{ background: {colours.surface_alt}; }}"
                f"QToolButton:focus {{ border-color: {colours.accent}; }}")
            button.clicked.connect(lambda: self.new_tab())
            button.show()
            self._new_tab_button = button
            self._position_new_tab_button()
        except Exception as exc:  # noqa: BLE001
            import os

            self._new_tab_button = None
            if os.environ.get("PYBROWSER_DEBUG_UI"):
                print(f"[ui] new-tab button: {type(exc).__name__}: {exc}", flush=True)

    def _position_new_tab_button(self) -> None:
        """Put the "+" just past the last tab, or in the corner when crowded.

        Two placements, because neither one is right on its own:

        * Normally the button is a child of the tab bar, sitting a few pixels
          past the last tab, so it reads as part of the strip.
        * Once the tabs fill the strip there is nowhere after the last tab to
          be - and the space at the right belongs to Qt's scroll arrows, which
          the button was drawing on top of. So it moves into the tab widget's
          corner slot, which Qt reserves and lays out around.

        Done by hand because a QTabBar has no layout to put a widget in: the
        tabs are painted, not laid out as widgets.
        """
        button = getattr(self, "_new_tab_button", None)
        if button is None:
            return
        bar = self.tabBar()
        gap = 4
        count = bar.count()
        last = bar.tabRect(count - 1) if count else None
        x = (last.right() + 1 + gap) if last else gap
        fits = last is None or (x + button.width() <= bar.width() - gap)

        if fits:
            if self._button_in_corner:
                # Take it back out of the corner slot and re-adopt it.
                self.setCornerWidget(None, Qt.Corner.TopRightCorner)
                button.setParent(bar)
                button.show()
                self._button_in_corner = False
            y = last.top() + max(0, (last.height() - button.height()) // 2) if last else gap
            button.move(x, y)
        elif not self._button_in_corner:
            self.setCornerWidget(button, Qt.Corner.TopRightCorner)
            button.show()
            self._button_in_corner = True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_new_tab_button()

    def tabInserted(self, index: int) -> None:  # noqa: N802
        super().tabInserted(index)
        self._reposition_soon()

    def tabRemoved(self, index: int) -> None:  # noqa: N802
        super().tabRemoved(index)
        self._reposition_soon()

    def _reposition_soon(self) -> None:
        """Move the "+" after Qt has laid the strip out, not before.

        tabInserted() runs while the bar is still being changed, so the new
        tab's rectangle is not yet its final one - positioning from it put the
        button on top of the second-to-last tab. Deferring by one event-loop
        turn is the difference between measuring the strip and guessing at it.

        The timer is bound to this widget as its context object so Qt cancels
        it if the tab manager is destroyed first - otherwise a torn-down window
        still gets one last callback, and it raises on the dead C++ object.
        """
        QTimer.singleShot(0, self, self._position_new_tab_button)

    # -- loading indicator --------------------------------------------------
    def _advance_spinner(self) -> None:
        """Turn the spinner on every loading tab.

        One timer for the whole strip rather than one per tab, and it only runs
        while something is actually loading - a timer ticking behind an idle
        browser is a battery cost for nothing.
        """
        from PySide6.QtGui import QIcon, QPixmap, QTransform

        self._spin_angle = (self._spin_angle + 45) % 360
        base = self._spinner_pixmap()
        if base is None:
            return
        turned = QIcon(base.transformed(
            QTransform().rotate(self._spin_angle), Qt.TransformationMode.SmoothTransformation))
        loading = False
        for index in range(self.count()):
            tab = self.widget(index)
            if isinstance(tab, BrowserTab) and tab.is_loading:
                self.setTabIcon(index, turned)
                loading = True
        if not loading:
            self._spinner.stop()

    def _spinner_pixmap(self):
        if getattr(self, "_spin_base", None) is None:
            try:
                from PySide6.QtWidgets import QApplication

                from app.ui import icons, theme

                colours = theme.palette_for(QApplication.instance())
                self._spin_base = icons.icon(
                    "spinner", colours.accent, size=32, weight=2.4).pixmap(16, 16)
            except Exception:  # noqa: BLE001
                self._spin_base = None
        return self._spin_base

    def _start_spinner(self) -> None:
        if not self._spinner.isActive():
            self._spinner.start()

    def _fallback_icon(self):
        """The icon a tab shows before its favicon arrives, or if it has none."""
        if getattr(self, "_page_icon", None) is None:
            try:
                from PySide6.QtWidgets import QApplication

                from app.ui import icons, theme

                colours = theme.palette_for(QApplication.instance())
                self._page_icon = icons.icon("page", colours.disabled, size=32, weight=1.8)
            except Exception:  # noqa: BLE001
                from PySide6.QtGui import QIcon

                self._page_icon = QIcon()
        return self._page_icon

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
        tab.load_started.connect(lambda t=tab: self._on_tab_load_started(t))
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
            self._reposition_soon()
            if tab is self.current_tab():
                self.current_title_changed.emit("New Tab")
            return
        label = title or tab.url().host() or "New Tab"
        self.setTabText(index, self._elide(label))
        self._reposition_soon()
        # The label is elided, so the tooltip carries both the full title and
        # where it actually goes - which is the question a tooltip on a tab is
        # usually being asked.
        url = tab.url().toString()
        self.setTabToolTip(index, f"{title}\n{url}" if title and url else (title or url))
        if tab is self.current_tab():
            self.current_title_changed.emit(title)
        url = tab.url().toString()
        if title and url:
            self.page_title_resolved.emit(url, title)

    def _on_tab_icon(self, tab: BrowserTab, icon) -> None:
        """Show the site's favicon, or our placeholder if it has none.

        A tab whose icon slot is empty is a tab that jumps sideways the moment
        a favicon arrives, so the slot is always filled.
        """
        index = self.indexOf(tab)
        if index == -1 or tab.is_loading:
            return                       # the spinner owns the slot while loading
        self.setTabIcon(index, icon if icon and not icon.isNull()
                        else self._fallback_icon())

    def _on_tab_url(self, tab: BrowserTab, url: QUrl) -> None:
        if tab is self.current_tab():
            self.current_url_changed.emit(url)

    def _on_tab_load_started(self, tab: BrowserTab) -> None:
        self._start_spinner()
        self._forward_if_current(tab, self.current_load_started)

    def _on_tab_load_finished(self, tab: BrowserTab, ok: bool) -> None:
        if ok:
            self.page_visited.emit(tab.url().toString(), tab.title())
        index = self.indexOf(tab)
        if index != -1:
            icon = tab.icon()
            self.setTabIcon(index, icon if icon and not icon.isNull()
                            else self._fallback_icon())
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

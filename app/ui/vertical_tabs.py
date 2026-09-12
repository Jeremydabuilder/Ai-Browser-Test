"""A vertical tab sidebar - the same TabManager, a different view of it.

TabManager (app/browser/tab_manager.py) is the one tab model: it owns tab
creation/closing/navigation and the horizontal QTabBar that has always
rendered it. This module adds a second, independent view of that same
model - it never stores its own list of tabs, never creates or closes a
tab except by calling back into TabManager, and rebuilds its rows from
TabManager's own tab_added/tab_closing/tab_updated/currentChanged signals
rather than tracking state in parallel. Switching layouts is purely which
view is on screen; the tabs themselves, and everything that already reads
them (Missions, history, the agent), never know or care which one it is.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.browser.tab_manager import TabManager
from app.storage.settings import (
    VERTICAL_TABS_COLLAPSED_WIDTH,
    VERTICAL_TABS_MAX_WIDTH,
    VERTICAL_TABS_MIN_WIDTH,
    SettingsStore,
)
from app.ui import icons, theme


class _TabRow(QFrame):
    """One tab: favicon, title, close button, active state.

    A plain click anywhere on the row (not just the tiny close glyph)
    switches to it - the row itself is the target, the way a real sidebar
    item works, not just its label text.
    """

    activated = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self._colours = c
        self._active = False
        self._collapsed = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(m.tab)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_2, 0, m.space_1, 0)
        layout.setSpacing(m.space_2)

        self._icon = QLabel(self)
        self._icon.setFixedSize(m.icon_sm, m.icon_sm)
        self._icon.setScaledContents(True)
        layout.addWidget(self._icon)

        self._title = QLabel("", self)
        self._title.setStyleSheet(f"color:{c.text}; font-size:{m.text_sm}px; background:transparent;")
        layout.addWidget(self._title, 1)

        self._close = QToolButton(self)
        self._close.setIcon(icons.icon("close", c.muted, size=32, weight=2.4))
        self._close.setIconSize(QSize(m.icon_sm - 2, m.icon_sm - 2))
        self._close.setAutoRaise(True)
        self._close.setCursor(Qt.CursorShape.ArrowCursor)
        self._close.setToolTip("Close tab")
        self._close.setFixedSize(m.icon_sm + 8, m.icon_sm + 8)
        self._close.setStyleSheet(
            "QToolButton { border: none; background: transparent; }"
            f"QToolButton:hover {{ background: {c.line}; border-radius: {m.radius_sm}px; }}")
        self._close.clicked.connect(self.close_requested.emit)
        layout.addWidget(self._close)

        self._apply_style()

    def set_content(self, title: str, icon, tooltip: str) -> None:
        self._icon.setPixmap(icon.pixmap(theme.METRICS.icon_sm, theme.METRICS.icon_sm))
        self._title.setText(title)
        self.setToolTip(tooltip)

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._apply_style()

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self._title.setVisible(not collapsed)
        self._close.setVisible(not collapsed)

    def _apply_style(self) -> None:
        c = self._colours
        m = theme.METRICS
        if self._active:
            self.setStyleSheet(
                f"_TabRow {{ background:{c.accent_soft}; border-radius:{m.radius_sm}px; }}")
            self._title.setStyleSheet(
                f"color:{c.text}; font-size:{m.text_sm}px; font-weight:600; background:transparent;")
        else:
            self.setStyleSheet(
                f"_TabRow {{ background:transparent; border-radius:{m.radius_sm}px; }}"
                f"_TabRow:hover {{ background:{c.surface_hover}; }}")
            self._title.setStyleSheet(
                f"color:{c.text}; font-size:{m.text_sm}px; background:transparent;")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mousePressEvent(event)


class VerticalTabList(QWidget):
    """The sidebar itself: a "+ New Tab" header, the scrollable row list,
    and a collapse toggle - all driven by one TabManager, never a second
    source of truth about which tabs exist."""

    #: The user asked to leave vertical mode entirely (not just collapse
    #: it) - MainWindow listens for this from a future "Use horizontal
    #: tabs" affordance; unused for now but kept so the widget's contract
    #: does not need to change when one is added.
    layout_change_requested = Signal(str)

    def __init__(self, tab_manager: TabManager, settings: SettingsStore,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tabs = tab_manager
        self._settings = settings
        self._rows: list[_TabRow] = []
        self._collapsed = settings.vertical_tabs_collapsed
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        self.setStyleSheet(f"background:{c.surface};")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.space_1, m.space_2, m.space_1, m.space_2)
        outer.setSpacing(m.space_1)

        header = QHBoxLayout()
        header.setContentsMargins(m.space_1, 0, m.space_1, 0)
        self.new_tab_button = QToolButton(self)
        self.new_tab_button.setIcon(icons.icon("plus", c.muted, size=32, weight=2.1))
        self.new_tab_button.setIconSize(QSize(m.icon_sm, m.icon_sm))
        self.new_tab_button.setAutoRaise(True)
        self.new_tab_button.setToolTip("New tab (Ctrl+T)")
        self.new_tab_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_tab_button.clicked.connect(lambda: self._tabs.new_tab())
        header.addWidget(self.new_tab_button)
        self.new_tab_label = QLabel("New Tab", self)
        self.new_tab_label.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        header.addWidget(self.new_tab_label, 1)
        self.collapse_button = QToolButton(self)
        self.collapse_button.setIcon(icons.icon("sidebar", c.muted, size=32, weight=2.0))
        self.collapse_button.setIconSize(QSize(m.icon_sm, m.icon_sm))
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button)
        outer.addLayout(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_widget = QWidget(scroll)
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.addStretch(1)
        scroll.setWidget(self._list_widget)
        outer.addWidget(scroll, 1)

        tab_manager.tab_added.connect(self._on_structure_changed)
        tab_manager.tab_closing.connect(lambda _i: self._defer_rebuild())
        tab_manager.tab_updated.connect(self._on_tab_updated)
        tab_manager.currentChanged.connect(self._on_current_changed)

        self._rebuild()
        self._apply_collapsed(self._collapsed, persist=False)

    # -- collapse/expand --------------------------------------------------
    def toggle_collapsed(self) -> None:
        self._apply_collapsed(not self._collapsed)

    def _apply_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        self._collapsed = collapsed
        if collapsed:
            # Collapsed width is fixed - there is nothing to drag, only an
            # icon column.
            self.setMinimumWidth(VERTICAL_TABS_COLLAPSED_WIDTH)
            self.setMaximumWidth(VERTICAL_TABS_COLLAPSED_WIDTH)
            self.resize(VERTICAL_TABS_COLLAPSED_WIDTH, self.height())
        else:
            # Expanded width is a splitter-resizable range, not a fixed
            # size - a fixed width would stop the splitter handle from ever
            # moving it, silently defeating the "resizable sidebar" feature.
            self.setMinimumWidth(VERTICAL_TABS_MIN_WIDTH)
            self.setMaximumWidth(VERTICAL_TABS_MAX_WIDTH)
            self.resize(self._settings.vertical_tabs_width, self.height())
        self.new_tab_label.setVisible(not collapsed)
        self.collapse_button.setToolTip("Expand sidebar" if collapsed else "Collapse sidebar")
        for row in self._rows:
            row.set_collapsed(collapsed)
        if persist:
            self._settings.vertical_tabs_collapsed = collapsed

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    # -- width (resizing) --------------------------------------------------
    def set_expanded_width(self, width: int) -> None:
        """Called while dragging the splitter handle next to this widget -
        persisted only when not collapsed, since a collapsed sidebar's
        width is the fixed icon-only one, not a user preference."""
        clamped = min(max(width, VERTICAL_TABS_MIN_WIDTH), VERTICAL_TABS_MAX_WIDTH)
        if not self._collapsed:
            self._settings.vertical_tabs_width = clamped

    # -- reacting to the model ---------------------------------------------
    def _on_structure_changed(self, _index: int) -> None:
        self._rebuild()

    def _defer_rebuild(self) -> None:
        # tab_closing fires just *before* removal - rebuilding immediately
        # would still count the tab about to disappear. One event-loop turn
        # later, TabManager's own count() already reflects the removal.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self, self._rebuild)

    def _on_tab_updated(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self._set_row_content(index)

    def _on_current_changed(self, _index: int) -> None:
        current = self._tabs.currentIndex()
        for i, row in enumerate(self._rows):
            row.set_active(i == current)

    def _rebuild(self) -> None:
        while self._list_layout.count() > 1:   # keep the trailing stretch
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # deleteLater() alone defers the actual removal to the next
                # event-loop turn - until then the old row is still a fully
                # visible sibling at its old position, which can paint right
                # on top of the new row _rebuild() is about to create there.
                # Detaching it immediately (hide + drop its parent) means
                # there is never a moment with two rows occupying one slot.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._rows = []
        current = self._tabs.currentIndex()
        for i in range(self._tabs.count()):
            row = _TabRow(self._list_widget)
            row.set_collapsed(self._collapsed)
            row.activated.connect(lambda idx=i: self._tabs.setCurrentIndex(idx))
            row.close_requested.connect(lambda idx=i: self._tabs.close_tab(idx))
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)
            self._set_row_content(i)
            row.set_active(i == current)

    def _set_row_content(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        title = self._tabs.tabText(index) or "New Tab"
        icon = self._tabs.tabIcon(index)
        tooltip = self._tabs.tabToolTip(index) or title
        self._rows[index].set_content(title, icon, tooltip)

"""A vertical tab sidebar - the same TabManager, a different view of it.

TabManager (app/browser/tab_manager.py) is the one tab model: it owns tab
creation/closing/navigation, pinning, grouping, and the horizontal QTabBar
that has always rendered it. This module adds a second, independent view of
that same model - it never stores its own list of tabs, never creates,
closes, pins or groups a tab except by calling back into TabManager, and
rebuilds its rows from TabManager's own tab_added/tab_closing/tab_updated/
pin_changed/groups_changed/currentChanged signals rather than tracking state
in parallel. Switching layouts is purely which view is on screen; the tabs
themselves, and everything that already reads them (Missions, history, the
agent), never know or care which one it is.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QScrollArea,
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
    pin_toggle_requested = Signal()
    move_to_group_requested = Signal(object)   # group_id, or None for "new group"
    remove_from_group_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self._colours = c
        self._active = False
        self._collapsed = False
        self._pinned = False
        self._group_id: str | None = None
        self._group_choices: list[tuple[str, str]] = []   # [(id, name), ...]
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(m.tab)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

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
        self._sync_visibility()

    def set_pinned(self, pinned: bool) -> None:
        """A pinned row stays icon-only (like collapsed) and protects its
        tab from the ordinary close button - the same "hard to close by
        accident" rule the horizontal tab bar enforces by hiding its own
        close glyph for a pinned tab."""
        self._pinned = pinned
        self._sync_visibility()

    def set_indent(self, indented: bool) -> None:
        """Nested under a group header - a small left indent is enough to
        read as "belongs to the section above" without a second column."""
        m = theme.METRICS
        left = m.space_2 + (m.space_4 if indented else 0)
        margins = self.layout().contentsMargins()
        self.layout().setContentsMargins(left, margins.top(), margins.right(), margins.bottom())

    def set_group_choices(self, group_id: str | None, choices: list[tuple[str, str]]) -> None:
        """What the context menu's "Move to group" submenu offers - the
        groups that exist right now, and which one (if any) this tab is
        already in, so its own group isn't offered as a destination."""
        self._group_id = group_id
        self._group_choices = choices

    def _sync_visibility(self) -> None:
        hide = self._collapsed or self._pinned
        self._title.setVisible(not hide)
        self._close.setVisible(not (hide or self._pinned) and not self._collapsed)

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

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Unpin Tab" if self._pinned else "Pin Tab",
                      self.pin_toggle_requested.emit)
        group_menu = menu.addMenu("Move to Group")
        for group_id, name in self._group_choices:
            if group_id == self._group_id:
                continue
            group_menu.addAction(name, lambda gid=group_id: self.move_to_group_requested.emit(gid))
        if self._group_choices:
            group_menu.addSeparator()
        group_menu.addAction("New Group…", lambda: self.move_to_group_requested.emit(None))
        if self._group_id is not None:
            menu.addAction("Remove from Group", self.remove_from_group_requested.emit)
        menu.addSeparator()
        menu.addAction("Close Tab", self.close_requested.emit)
        menu.exec(self.mapToGlobal(pos))


class _GroupHeader(QFrame):
    """One group's header row: name, tab count, collapse/expand, and a menu
    for rename/ungroup. Clicking anywhere toggles collapse, the same "the
    row is the target" rule _TabRow uses for activation."""

    toggle_requested = Signal()
    rename_requested = Signal(str)
    ungroup_requested = Signal()

    def __init__(self, group_id: str, name: str, collapsed: bool, count: int,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group_id = group_id
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(m.tab)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_2, 0, m.space_1, 0)
        layout.setSpacing(m.space_2)

        self._chevron = QLabel("▾" if not collapsed else "▸", self)
        self._chevron.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        layout.addWidget(self._chevron)

        self._label = QLabel(f"{name} ({count})", self)
        self._label.setStyleSheet(
            f"color:{c.muted}; font-size:{m.text_xs}px; font-weight:600; "
            "letter-spacing:0.04em; background:transparent;")
        layout.addWidget(self._label, 1)
        self.setToolTip(name)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_requested.emit()
        super().mousePressEvent(event)

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Rename Group…", self._prompt_rename)
        menu.addAction("Ungroup (keep tabs open)", self.ungroup_requested.emit)
        menu.exec(self.mapToGlobal(pos))

    def _prompt_rename(self) -> None:
        name, ok = QInputDialog.getText(self, "Rename Group", "Group name:")
        if ok and name.strip():
            self.rename_requested.emit(name.strip())


class VerticalTabList(QWidget):
    """The sidebar itself: a "+ New Tab" header, the scrollable row list,
    and a collapse toggle - all driven by one TabManager, never a second
    source of truth about which tabs exist, which are pinned, or how they
    are grouped."""

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
        #: tab index -> its row. A dict, not a list, because rows are no
        #: longer laid out in index order once pinning/grouping puts some
        #: tabs ahead of others - lookups by index (from tab_updated,
        #: currentChanged) still need to find the right widget regardless
        #: of where it is drawn.
        self._rows: dict[int, _TabRow] = {}
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
        tab_manager.pin_changed.connect(lambda _i: self._rebuild())
        tab_manager.groups_changed.connect(self._rebuild)
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
        for row in self._rows.values():
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
        row = self._rows.get(index)
        if row is not None:
            self._set_row_content(index)

    def _on_current_changed(self, _index: int) -> None:
        current = self._tabs.currentIndex()
        for index, row in self._rows.items():
            row.set_active(index == current)

    # -- building the row list, pinned first, then groups, then the rest ---
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
        self._rows = {}
        current = self._tabs.currentIndex()

        def insert(widget: QWidget) -> None:
            self._list_layout.insertWidget(self._list_layout.count() - 1, widget)

        pinned = [i for i in range(self._tabs.count()) if self._tabs.is_pinned(i)]
        groups = self._tabs.groups()
        group_choices = [(g["id"], g["name"]) for g in groups]
        grouped = {i for g in groups for i in g["tab_indices"]}

        for index in pinned:
            row = self._make_row(index, current, pinned=True)
            insert(row)

        if pinned and (groups or len(pinned) < self._tabs.count()):
            insert(self._divider())

        for group in groups:
            header = _GroupHeader(group["id"], group["name"], group["collapsed"],
                                  len([i for i in group["tab_indices"] if i not in pinned]),
                                  self._list_widget)
            header.toggle_requested.connect(
                lambda gid=group["id"], c=group["collapsed"]:
                    self._tabs.set_group_collapsed(gid, not c))
            header.rename_requested.connect(
                lambda name, gid=group["id"]: self._tabs.rename_group(gid, name))
            header.ungroup_requested.connect(
                lambda gid=group["id"]: self._tabs.remove_group(gid))
            insert(header)
            if not group["collapsed"]:
                for index in group["tab_indices"]:
                    if index in pinned:
                        continue   # already rendered above; pin takes visual precedence
                    row = self._make_row(index, current, pinned=False)
                    row.set_indent(True)
                    insert(row)

        for index in range(self._tabs.count()):
            if index in pinned or index in grouped:
                continue
            row = self._make_row(index, current, pinned=False)
            insert(row)

        for index, row in self._rows.items():
            row.set_group_choices(self._tabs.group_of(index), group_choices)

    def _make_row(self, index: int, current: int, *, pinned: bool) -> _TabRow:
        row = _TabRow(self._list_widget)
        row.set_collapsed(self._collapsed)
        row.set_pinned(pinned)
        row.activated.connect(lambda idx=index: self._tabs.setCurrentIndex(idx))
        row.close_requested.connect(lambda idx=index: self._tabs.close_tab(idx))
        row.pin_toggle_requested.connect(
            lambda idx=index: self._tabs.set_pinned(idx, not self._tabs.is_pinned(idx)))
        row.move_to_group_requested.connect(
            lambda gid, idx=index: self._move_to_group(idx, gid))
        row.remove_from_group_requested.connect(
            lambda idx=index: self._tabs.move_tab_to_group(idx, None))
        self._rows[index] = row
        self._set_row_content(index)
        row.set_active(index == current)
        return row

    def _move_to_group(self, index: int, group_id: str | None) -> None:
        if group_id is None:
            name, ok = QInputDialog.getText(self, "New Group", "Group name:")
            if not ok or not name.strip():
                return
            group_id = self._tabs.create_group(name.strip())
        self._tabs.move_tab_to_group(index, group_id)

    def _divider(self) -> QFrame:
        c = theme.palette_for(QApplication.instance())
        line = QFrame(self._list_widget)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{c.line}; border:none;")
        return line

    def _set_row_content(self, index: int) -> None:
        row = self._rows.get(index)
        if row is None:
            return
        # Not tabText(): a pinned tab's tabText is blank on purpose (see
        # TabManager._apply_pin_appearance, the horizontal bar's own
        # icon-only rendering) - the tooltip always carries the real title
        # regardless of pin state, so it is the one source both views can
        # read the actual name from.
        tooltip = self._tabs.tabToolTip(index) or "New Tab"
        title = tooltip.split("\n", 1)[0] or "New Tab"
        icon = self._tabs.tabIcon(index)
        row.set_content(title, icon, tooltip)

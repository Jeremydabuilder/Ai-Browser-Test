""""Search tabs" - find an open tab by title or domain, not a command palette.

Deliberately narrow: one field, one list, Enter switches to the highlighted
tab. It reads TabManager the same way VerticalTabList does - by calling its
public methods (count/tabText/tabIcon/tabToolTip/setCurrentIndex) - rather
than keeping its own copy of what tabs exist.

Also the entry point for two small, explicitly user-triggered multi-tab
actions - "ask Py about the tabs I picked" and "these tabs are duplicates,
close them" - added for the AI-browser capability audit. Neither stores a
second copy of what tabs exist: both just read TabManager/BrowserController
at the moment the user clicks, the same way the single-tab search above
already does.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.browser.internal import is_internal
from app.browser.tab_manager import TabManager
from app.ui import theme
from app.utils.urls import short_host

_INDEX_ROLE = Qt.ItemDataRole.UserRole


class TabSearchDialog(QDialog):
    """A small popover list: type to filter open tabs by title or domain,
    select one or more, then switch to one or hand the selection to Py."""

    def __init__(
        self,
        tab_manager: TabManager,
        parent: QWidget | None = None,
        *,
        on_ask_py: Callable[[list[int]], None] | None = None,
        on_close_duplicates: Callable[[list[int]], None] | None = None,
        on_start_mission: Callable[[list[int]], None] | None = None,
    ) -> None:
        """``on_ask_py``, ``on_close_duplicates`` and ``on_start_mission``
        are optional callbacks the window supplies, each taking the
        TabManager indices involved - this dialog only ever collects which
        rows the user picked, it does not itself know how to reach Py,
        start a Mission, or decide what "close" means."""
        super().__init__(parent)
        self._tabs = tab_manager
        self._on_ask_py = on_ask_py
        self._on_close_duplicates = on_close_duplicates
        self._on_start_mission = on_start_mission
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setWindowTitle("Search Tabs")
        self.setWindowFlag(Qt.WindowType.Popup, False)
        self.resize(440, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_3, m.space_3, m.space_3)
        layout.setSpacing(m.space_2)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Search open tabs by title or domain…")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._refresh)
        layout.addWidget(self.field)

        self._duplicates_label = QLabel("", self)
        self._duplicates_label.setProperty("kind", "muted")
        self._duplicates_label.hide()
        dupe_row = QHBoxLayout()
        dupe_row.addWidget(self._duplicates_label, 1)
        self.close_duplicates_button = QPushButton("Close duplicates", self)
        self.close_duplicates_button.setProperty("kind", "quiet")
        self.close_duplicates_button.hide()
        self.close_duplicates_button.clicked.connect(self._close_duplicates)
        dupe_row.addWidget(self.close_duplicates_button)
        layout.addLayout(dupe_row)

        self.list = QListWidget(self)
        self.list.itemActivated.connect(self._activate)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._sync_multi_actions)
        layout.addWidget(self.list, 1)

        self._empty_label = QLabel("No open tabs match.", self)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setProperty("kind", "muted")
        self._empty_label.hide()
        layout.addWidget(self._empty_label)

        # Only shown when the window actually wired a handler - a caller
        # that omits on_ask_py gets the plain single-tab switcher, nothing
        # half-built.
        action_row = QHBoxLayout()
        self.ask_py_button = QPushButton("Ask Py about selected tabs…", self)
        self.ask_py_button.setProperty("kind", "primary")
        self.ask_py_button.clicked.connect(self._ask_py_about_selection)
        self.ask_py_button.setVisible(self._on_ask_py is not None)
        self.ask_py_button.setEnabled(False)
        action_row.addWidget(self.ask_py_button, 1)

        self.start_mission_button = QPushButton("Start a Mission from these…", self)
        self.start_mission_button.setProperty("kind", "quiet")
        self.start_mission_button.clicked.connect(self._start_mission_from_selection)
        self.start_mission_button.setVisible(self._on_start_mission is not None)
        self.start_mission_button.setEnabled(False)
        action_row.addWidget(self.start_mission_button, 1)
        layout.addLayout(action_row)

        self.field.installEventFilter(self)
        self._refresh("")
        self._detect_duplicates()
        self.field.setFocus()

    # -- filtering ----------------------------------------------------------
    def _refresh(self, term: str) -> None:
        term = term.strip().lower()
        self.list.clear()
        for index in range(self._tabs.count()):
            title = self._tabs.tabText(index) or "New Tab"
            widget = self._tabs.widget(index)
            url = widget.url().toString() if widget is not None else ""
            domain = short_host_safe(url)
            if term and term not in title.lower() and term not in domain.lower():
                continue
            label = f"{title}  ·  {domain}" if domain else title
            item = QListWidgetItem(self._tabs.tabIcon(index), label)
            item.setData(_INDEX_ROLE, index)
            item.setToolTip(url or title)
            self.list.addItem(item)
        has_items = self.list.count() > 0
        self.list.setVisible(has_items)
        self._empty_label.setVisible(not has_items)
        if has_items:
            self.list.setCurrentRow(0)

    # -- activation -----------------------------------------------------------
    def _activate(self, item: QListWidgetItem) -> None:
        index = item.data(_INDEX_ROLE)
        if index is not None:
            self._tabs.setCurrentIndex(index)
        self.accept()

    # -- multi-tab actions --------------------------------------------------
    def _sync_multi_actions(self) -> None:
        selected = len(self.list.selectedItems())
        self.ask_py_button.setEnabled(self._on_ask_py is not None and selected >= 1)
        self.start_mission_button.setEnabled(
            self._on_start_mission is not None and selected >= 1)

    def _selected_indices(self) -> list[int]:
        indices = [item.data(_INDEX_ROLE) for item in self.list.selectedItems()]
        return [i for i in indices if i is not None]

    def _ask_py_about_selection(self) -> None:
        if self._on_ask_py is None:
            return
        indices = self._selected_indices()
        if not indices:
            return
        self._on_ask_py(indices)
        self.accept()

    def _start_mission_from_selection(self) -> None:
        if self._on_start_mission is None:
            return
        indices = self._selected_indices()
        if not indices:
            return
        self._on_start_mission(indices)
        self.accept()

    # -- duplicate tabs -------------------------------------------------------
    def _detect_duplicates(self) -> None:
        """Two or more open tabs pointed at the exact same address - the
        cheapest, least surprising definition of "duplicate": no fuzzy
        matching of titles or content, just the same URL twice."""
        by_url: dict[str, list[int]] = {}
        for index in range(self._tabs.count()):
            widget = self._tabs.widget(index)
            url = widget.url().toString() if widget is not None else ""
            if not url or is_internal_url(url):
                continue
            by_url.setdefault(url, []).append(index)
        self._duplicate_indices = [i for group in by_url.values() if len(group) > 1
                                   for i in group[1:]]  # keep the first, flag the rest
        count = len(self._duplicate_indices)
        if count and self._on_close_duplicates is not None:
            noun = "tab" if count == 1 else "tabs"
            self._duplicates_label.setText(f"{count} duplicate {noun} open.")
            self._duplicates_label.show()
            self.close_duplicates_button.show()
        else:
            self._duplicates_label.hide()
            self.close_duplicates_button.hide()

    def _close_duplicates(self) -> None:
        if self._on_close_duplicates is None or not self._duplicate_indices:
            return
        self._on_close_duplicates(self._duplicate_indices)
        self._duplicate_indices = []
        self._refresh(self.field.text())
        self._detect_duplicates()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # Arrow keys move through the list even while the text field has
        # focus, and Enter activates whatever row is highlighted - typing
        # to filter and picking a result should not require a Tab keypress
        # in between.
        if obj is self.field and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow()
                count = self.list.count()
                if count:
                    step = 1 if key == Qt.Key.Key_Down else -1
                    self.list.setCurrentRow((row + step) % count)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self._activate(item)
                return True
        return super().eventFilter(obj, event)


def is_internal_url(url: str) -> bool:
    """True for the new-tab page, Mission Library, and anything else that
    is not really "a website" - about:/data: pages included."""
    if not url or url.startswith("about:") or url.startswith("data:"):
        return True
    from PySide6.QtCore import QUrl

    return is_internal(QUrl(url))


def short_host_safe(url: str) -> str:
    """A domain to show next to a tab's title - or "" when there isn't a
    real one, so an internal page or a data:/about: page never shows its
    raw scheme where a website's domain normally goes."""
    if is_internal_url(url):
        return ""
    from PySide6.QtCore import QUrl

    return short_host(QUrl(url))

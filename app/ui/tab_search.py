""""Search tabs" - find an open tab by title or domain, not a command palette.

Deliberately narrow: one field, one list, Enter switches to the highlighted
tab. It reads TabManager the same way VerticalTabList does - by calling its
public methods (count/tabText/tabIcon/tabToolTip/setCurrentIndex) - rather
than keeping its own copy of what tabs exist.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.browser.internal import is_internal
from app.browser.tab_manager import TabManager
from app.ui import theme
from app.utils.urls import short_host

_INDEX_ROLE = Qt.ItemDataRole.UserRole


class TabSearchDialog(QDialog):
    """A small popover list: type to filter open tabs by title or domain."""

    def __init__(self, tab_manager: TabManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tabs = tab_manager
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setWindowTitle("Search Tabs")
        self.setWindowFlag(Qt.WindowType.Popup, False)
        self.resize(420, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_3, m.space_3, m.space_3)
        layout.setSpacing(m.space_2)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Search open tabs by title or domain…")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._refresh)
        layout.addWidget(self.field)

        self.list = QListWidget(self)
        self.list.itemActivated.connect(self._activate)
        layout.addWidget(self.list, 1)

        self._empty_label = QLabel("No open tabs match.", self)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setProperty("kind", "muted")
        self._empty_label.hide()
        layout.addWidget(self._empty_label)

        self.field.installEventFilter(self)
        self._refresh("")
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


def short_host_safe(url: str) -> str:
    """A domain to show next to a tab's title - or "" when there isn't a
    real one, so an internal page (the new-tab page, Mission Library, …) or
    a data:/about: page never shows its raw scheme where a website's domain
    normally goes."""
    if not url or url.startswith("about:") or url.startswith("data:"):
        return ""
    from PySide6.QtCore import QUrl

    target = QUrl(url)
    if is_internal(target):
        return ""
    return short_host(target)

"""History and bookmark manager dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.storage import BookmarkStore, HistoryStore

_URL_ROLE = Qt.ItemDataRole.UserRole
_ID_ROLE = Qt.ItemDataRole.UserRole + 1


class _ListDialog(QDialog):
    """Shared scaffolding: a filter box, a two-column tree and buttons."""

    open_requested = Signal(str)

    def __init__(self, title: str, headers: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 480)

        self.filter_box = QLineEdit(self)
        self.filter_box.setPlaceholderText("Filter…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self.refresh)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(headers)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemActivated.connect(self._on_activated)

        self.open_button = QPushButton("Open", self)
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.clicked.connect(self._delete_selected)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.accept)

        self.button_row = buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_box)
        layout.addWidget(self.tree)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _selected_urls(self) -> list[str]:
        return [item.data(0, _URL_ROLE) for item in self.tree.selectedItems()]

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        url = item.data(0, _URL_ROLE)
        if url:
            self.open_requested.emit(url)

    def _open_selected(self) -> None:
        for url in self._selected_urls():
            self.open_requested.emit(url)

    def _delete_selected(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class HistoryDialog(_ListDialog):
    def __init__(self, history: HistoryStore, parent: QWidget | None = None) -> None:
        self._history = history
        super().__init__("History", ["Title", "URL", "Visited"], parent)
        clear_all = QPushButton("Clear all history", self)
        clear_all.clicked.connect(self._clear_all)
        self.button_row.insertWidget(2, clear_all)

    def refresh(self) -> None:
        term = self.filter_box.text().strip()
        entries = self._history.search(term, 500) if term else self._history.recent(500)
        self.tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.title or entry.url, entry.url, entry.visited_at])
            item.setData(0, _URL_ROLE, entry.url)
            item.setData(0, _ID_ROLE, entry.id)
            self.tree.addTopLevelItem(item)

    def _delete_selected(self) -> None:
        for item in self.tree.selectedItems():
            self._history.delete(item.data(0, _ID_ROLE))
        self.refresh()

    def _clear_all(self) -> None:
        confirm = QMessageBox.question(
            self, "Clear history", "Delete the entire browsing history?"
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._history.clear()
            self.refresh()


class BookmarksDialog(_ListDialog):
    def __init__(self, bookmarks: BookmarkStore, parent: QWidget | None = None) -> None:
        self._bookmarks = bookmarks
        super().__init__("Bookmarks", ["Title", "URL", "Added"], parent)

    def refresh(self) -> None:
        term = self.filter_box.text().strip().lower()
        self.tree.clear()
        for bookmark in self._bookmarks.all():
            haystack = f"{bookmark.title} {bookmark.url}".lower()
            if term and term not in haystack:
                continue
            item = QTreeWidgetItem(
                [bookmark.title or bookmark.url, bookmark.url, bookmark.created_at]
            )
            item.setData(0, _URL_ROLE, bookmark.url)
            item.setData(0, _ID_ROLE, bookmark.id)
            self.tree.addTopLevelItem(item)

    def _delete_selected(self) -> None:
        for item in self.tree.selectedItems():
            self._bookmarks.remove_by_id(item.data(0, _ID_ROLE))
        self.refresh()

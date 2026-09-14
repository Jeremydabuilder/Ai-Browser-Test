"""Highlights Library: search, edit the note, delete, Ask Py, Add to Mission.

Deliberately its own dialog rather than reusing _ListDialog (app/ui/dialogs.py):
that base class's primary action is "Open" a URL, which is not the point of a
highlight - a highlight is meant to be *used* (asked about, added to a
Mission), not navigated to, and its own source page may not even be open, or
exist, any more. See app/storage/highlights.py for why that is fine.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.storage.highlights import Highlight, HighlightStore
from app.ui import theme
from app.ui.dialogs import confirm_destructive

_ID_ROLE = Qt.ItemDataRole.UserRole


def _condensed(text: str, limit: int = 90) -> str:
    """One line, for a tree cell - a highlight's text is often several
    sentences and must not blow out the row height."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _saved_when(iso_timestamp: str) -> str:
    """A glance-friendly moment, the same shape as History/Bookmarks use -
    duplicated rather than imported from app.ui.dialogs, which keeps its
    version private (leading underscore) to that module."""
    try:
        dt = datetime.fromisoformat(iso_timestamp).astimezone()
    except ValueError:
        return iso_timestamp
    today = datetime.now().astimezone().date()
    day = dt.date()
    time_part = dt.strftime("%I:%M %p").lstrip("0")
    if day == today:
        return f"Today · {time_part}"
    if (today - day).days == 1:
        return f"Yesterday · {time_part}"
    if day.year == today.year:
        return f"{dt.strftime('%b')} {dt.day} · {time_part}"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


class HighlightsLibraryDialog(QDialog):
    def __init__(self, highlights: HighlightStore, parent: QWidget | None = None,
                 *, on_ask_py=None, on_add_to_mission=None, knowledge_index=None,
                 knowledge_graph=None) -> None:
        super().__init__(parent)
        self._highlights = highlights
        #: Phase 13 - a deleted highlight's semantic-index entry must not
        #: outlive it. None in tests/contexts with no knowledge index.
        self._knowledge_index = knowledge_index
        #: Phase 19 - same idea, for the research graph's Highlight node.
        self._knowledge_graph = knowledge_graph
        #: Callbacks rather than signals: MainWindow is the only caller,
        #: and both actions need a live Highlight, not just an id - the
        #: same "callback, not a new route into the agent" shape
        #: tab_search.py's on_ask_py already uses.
        self._on_ask_py = on_ask_py
        self._on_add_to_mission = on_add_to_mission

        self.setWindowTitle("Highlights")
        self.resize(760, 480)
        m = theme.METRICS

        self.filter_box = QLineEdit(self)
        self.filter_box.setPlaceholderText("Search highlights…")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self.refresh)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Page", "Highlight", "Note", "Saved"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        colours = theme.palette_for(QApplication.instance())
        self.tree.setStyleSheet(
            f"QTreeWidget {{ alternate-background-color: {colours.surface_alt}; }}")
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self._empty_label = QLabel("", self)
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setProperty("kind", "muted")
        self._empty_label.hide()

        self.ask_py_button = QPushButton("Ask Py", self)
        self.ask_py_button.setProperty("kind", "primary")
        self.ask_py_button.clicked.connect(self._ask_py_selected)
        self.mission_button = QPushButton("Add to Mission", self)
        self.mission_button.clicked.connect(self._add_selected_to_mission)
        self.edit_note_button = QPushButton("Edit Note…", self)
        self.edit_note_button.clicked.connect(self._edit_note_selected)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setProperty("kind", "danger")
        self.delete_button.clicked.connect(self._delete_selected)
        self.close_button = QPushButton("Close", self)
        self.close_button.setProperty("kind", "quiet")
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.setSpacing(m.space_2)
        buttons.addWidget(self.ask_py_button)
        buttons.addWidget(self.mission_button)
        buttons.addWidget(self.edit_note_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)
        layout.addWidget(self.filter_box)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self._empty_label, 1)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        term = self.filter_box.text().strip()
        rows = self._highlights.search(term) if term else self._highlights.all()
        self.tree.clear()
        for highlight in rows:
            when = _saved_when(highlight.created_at)
            item = QTreeWidgetItem([
                highlight.title or "Untitled", _condensed(highlight.text),
                _condensed(highlight.note, 40), when])
            item.setToolTip(0, highlight.url)
            item.setToolTip(1, highlight.text)
            if highlight.note:
                item.setToolTip(2, highlight.note)
            item.setData(0, _ID_ROLE, highlight.id)
            self.tree.addTopLevelItem(item)
        has_items = self.tree.topLevelItemCount() > 0
        self.tree.setVisible(has_items)
        self._empty_label.setVisible(not has_items)
        if not has_items:
            self._empty_label.setText(
                f'No highlights match "{term}".' if term else
                "Nothing saved yet. Select text on a page and choose "
                "“Save Highlight”.")

    def _selected(self) -> list[Highlight]:
        out = []
        for item in self.tree.selectedItems():
            highlight = self._highlights.get(item.data(0, _ID_ROLE))
            if highlight is not None:
                out.append(highlight)
        return out

    def _ask_py_selected(self) -> None:
        selected = self._selected()
        if not selected or self._on_ask_py is None:
            return
        for highlight in selected:
            self._on_ask_py(highlight)

    def _add_selected_to_mission(self) -> None:
        selected = self._selected()
        if not selected or self._on_add_to_mission is None:
            return
        for highlight in selected:
            self._on_add_to_mission(highlight)

    def _edit_note_selected(self) -> None:
        selected = self._selected()
        if len(selected) != 1:
            return
        highlight = selected[0]
        text, ok = QInputDialog.getMultiLineText(
            self, "Edit Note", "Note for this highlight:", highlight.note)
        if ok:
            self._highlights.set_note(highlight.id, text)
            self.refresh()

    def _delete_selected(self) -> None:
        selected = self._selected()
        if not selected:
            return
        noun = "highlight" if len(selected) == 1 else f"{len(selected)} highlights"
        if not confirm_destructive(
                self, "Delete Highlights", f"Delete this {noun}?",
                "Delete", informative="This cannot be undone."):
            return
        for highlight in selected:
            self._highlights.remove(highlight.id)
            if self._knowledge_index is not None:
                self._knowledge_index.remove_highlight(highlight.id)
            if self._knowledge_graph is not None:
                self._knowledge_graph.remove_highlight(highlight.id)
        self.refresh()

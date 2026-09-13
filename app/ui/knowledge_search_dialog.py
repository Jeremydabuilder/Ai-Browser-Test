""""Search History & Knowledge" - semantic + lexical search over the
Phase 13 knowledge index (browsing history, Missions, findings,
highlights, PDFs, files). One field, one ranked list, Enter/double-click
opens the source - the same "small popover, not a command palette" shape
tab_search.py already uses.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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

from app.knowledge.types import SourceType
from app.ui import theme

_RESULT_ROLE = Qt.ItemDataRole.UserRole


class KnowledgeSearchDialog(QDialog):
    """``knowledge_index`` is the Phase 13 KnowledgeIndex. ``on_open`` is an
    optional callback taking the selected SearchResult - the window decides
    what "open" means for each source type (navigate a tab, show the
    Mission Library, ...); this dialog only ever picks a result."""

    def __init__(self, knowledge_index, parent: QWidget | None = None, *,
                 on_open: Callable[[object], None] | None = None) -> None:
        super().__init__(parent)
        self._knowledge = knowledge_index
        self._on_open = on_open
        self._results: list = []
        self.setWindowTitle("Search History & Knowledge")
        self.resize(640, 440)
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        if not knowledge_index.enabled:
            layout.addWidget(QLabel(
                "Semantic history is off. Turn it on in Settings → Privacy / Memory / "
                "Knowledge to search your browsing history, Missions, findings, highlights, "
                "PDFs and files.", self))
            return

        self.search_box = QLineEdit(self)
        self.search_box.setPlaceholderText("Search your history and knowledge…")
        self.search_box.textChanged.connect(self._on_query_changed)
        layout.addWidget(self.search_box)

        self.list_widget = QListWidget(self)
        self.list_widget.itemActivated.connect(self._on_activated)
        layout.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        open_button = QPushButton("Open", self)
        open_button.clicked.connect(self._open_selected)
        buttons.addWidget(open_button)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.search_box.setFocus()

    def _on_query_changed(self, text: str) -> None:
        from app.knowledge.retrieval import search

        self.list_widget.clear()
        self._results = []
        if not text.strip():
            return
        chunks = self._knowledge.store.all_chunks()
        self._results = search(chunks, text, limit=20)
        for result in self._results:
            chunk = result.chunk
            label = SourceType.LABELS.get(chunk.source_type, chunk.source_type)
            freshness = " · may be stale" if result.stale else ""
            percent = round(result.score * 100)
            title = chunk.title or chunk.location or "(untitled)"
            text_line = f"{title}  —  {label} · {chunk.timestamp[:10]}{freshness} · {percent}%"
            item = QListWidgetItem(text_line)
            item.setToolTip(result.excerpt)
            item.setData(_RESULT_ROLE, result)
            self.list_widget.addItem(item)

    def _on_activated(self, item: QListWidgetItem) -> None:
        self._open(item.data(_RESULT_ROLE))

    def _open_selected(self) -> None:
        item = self.list_widget.currentItem()
        if item is not None:
            self._open(item.data(_RESULT_ROLE))

    def _open(self, result) -> None:
        if result is None:
            return
        if self._on_open is not None:
            self._on_open(result)
        self.accept()

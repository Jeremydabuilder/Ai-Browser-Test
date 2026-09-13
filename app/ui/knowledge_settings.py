"""Settings -> Privacy / Memory / Knowledge: the one place Semantic
History / Local RAG (Phase 13) is turned on, explained, and managed.

Off by default. Turning it on only affects indexing GOING FORWARD -
enabling and disabling never touches what is already stored; "Clear"
is the separate, explicit action for that (see the Phase 13 spec's
deletion/privacy section).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme
from app.ui.dialogs import confirm_destructive

_WHAT_GETS_INDEXED = (
    "When on, PyBrowser indexes on this device only: page titles and URLs you visit, "
    "your Missions and their findings, saved highlights, PDFs and local files you "
    "explicitly add. It never indexes passwords, form field contents, authentication "
    "tokens, clipboard contents, or the full body of every page you visit."
)


class KnowledgeSettingsPanel(QWidget):
    def __init__(self, knowledge_index, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._knowledge = knowledge_index
        self._settings = settings
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_4, m.space_5, m.space_4)
        layout.setSpacing(m.space_3)

        self.enable_check = QCheckBox("Enable semantic history", self)
        self.enable_check.setChecked(settings.semantic_history_enabled)
        self.enable_check.toggled.connect(self._on_toggled)
        layout.addWidget(self.enable_check)

        explanation = QLabel(_WHAT_GETS_INDEXED, self)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.status_label = QLabel(self)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.rebuild_button = QPushButton("Rebuild", self)
        self.rebuild_button.setToolTip(
            "Re-index everything currently indexable. Existing entries for content "
            "that no longer exists are left as-is unless you Clear first.")
        self.rebuild_button.clicked.connect(self._on_rebuild)
        button_row.addWidget(self.rebuild_button)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.clicked.connect(self._on_clear)
        button_row.addWidget(self.clear_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        #: Set by MainWindow to a callable that re-indexes history/Missions/
        #: highlights from their live stores - kept out of this panel and
        #: out of KnowledgeIndex itself, since gathering "everything that
        #: currently exists" needs the window's own store references, and
        #: this panel should not need to import all of them just to offer
        #: a Rebuild button.
        self.rebuild_callback = None

        self._refresh_status()

    def _on_toggled(self, checked: bool) -> None:
        """Enabling/disabling never touches what is already stored - see
        this module's docstring. Only future indexing calls change."""
        self._settings.semantic_history_enabled = checked
        self._refresh_status()

    def _refresh_status(self) -> None:
        stats = self._knowledge.stats()
        size_kb = stats["storage_bytes"] / 1024
        last = stats["last_indexed_at"] or "Never"
        self.status_label.setText(
            f"Semantic history: {'On' if self._settings.semantic_history_enabled else 'Off'}\n"
            f"Indexed items: {stats['chunk_count']} chunks from {stats['source_count']} sources\n"
            f"Last indexed: {last}\n"
            f"Storage used: ~{size_kb:.1f} KB")

    def _on_rebuild(self) -> None:
        if not self._settings.semantic_history_enabled:
            QMessageBox.information(
                self, "Semantic history is off",
                "Turn on \"Enable semantic history\" first - Rebuild only re-indexes "
                "while the feature is on.")
            return
        if self.rebuild_callback is not None:
            self.rebuild_callback()
        self._refresh_status()

    def _on_clear(self) -> None:
        if not confirm_destructive(
                self, "Clear Semantic Index",
                "Delete everything in the semantic history index?",
                "Clear", informative="This cannot be undone. Your actual browsing "
                "history, Missions, highlights, PDFs and files are not affected - "
                "only the search index built from them."):
            return
        self._knowledge.clear()
        self._refresh_status()

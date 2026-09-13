"""Workstreams: the UI for a Multi-Agent Mission (Phase 9).

A compact activity view of what each worker is doing - never a multi-chat
transcript. Each row is exactly what MissionCoordinator's WorkerTask
already carries: nothing here is computed or inferred, it is the same
structured task/result record the coordinator itself works from - see
app/missions/coordinator.py's own docstring on why workers do not talk to
each other in prose.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.missions.coordinator import WorkerRole, WorkerState
from app.ui import theme

_STATE_GLYPHS = {
    WorkerState.QUEUED: "·",             # ·
    WorkerState.RUNNING: "…",            # …
    WorkerState.WAITING_FOR_APPROVAL: "!",
    WorkerState.DONE: "✓",               # ✓
    WorkerState.FAILED: "✕",             # ✕
    WorkerState.SKIPPED: "–",            # –
}
_STATE_LABELS = {
    WorkerState.QUEUED: "Waiting", WorkerState.RUNNING: "Running…",
    WorkerState.WAITING_FOR_APPROVAL: "Waiting for approval",
    WorkerState.DONE: "Done", WorkerState.FAILED: "Failed", WorkerState.SKIPPED: "Skipped",
}

_COLUMNS = ["Role", "Task", "State", "Findings added", "Result / Error"]


class WorkstreamsDialog(QDialog):
    """Shows every worker in the current Multi-Agent Mission run, and lets
    the user answer a worker's approval request the moment it appears."""

    def __init__(self, coordinator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Workstreams")
        self.resize(760, 420)
        self._coordinator = coordinator
        self._rows_by_id: dict[int, int] = {}
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        self.summary_label = QLabel("Planning…", self)
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        coordinator.worker_added.connect(self._on_worker_added)
        coordinator.worker_changed.connect(self._on_worker_changed)
        coordinator.worker_confirmation_required.connect(self._on_confirmation_required)
        coordinator.result_ready.connect(self._on_result_ready)
        coordinator.failed.connect(self._on_failed)

    def _row_for(self, task) -> int:
        if task.id in self._rows_by_id:
            return self._rows_by_id[task.id]
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows_by_id[task.id] = row
        return row

    def _on_worker_added(self, task) -> None:
        row = self._row_for(task)
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        self.table.setItem(row, 0, QTableWidgetItem(role_label))
        self.table.setItem(row, 1, QTableWidgetItem(task.title))
        self._refresh_row(row, task)

    def _on_worker_changed(self, task) -> None:
        row = self._row_for(task)
        self._refresh_row(row, task)

    def _refresh_row(self, row: int, task) -> None:
        glyph = _STATE_GLYPHS.get(task.state, "")
        label = _STATE_LABELS.get(task.state, task.state)
        self.table.setItem(row, 2, QTableWidgetItem(f"{glyph} {label}"))
        self.table.setItem(row, 3, QTableWidgetItem(str(task.findings_added)))
        detail = task.error or task.result
        self.table.setItem(row, 4, QTableWidgetItem(detail))

    def _on_confirmation_required(self, task, request, session) -> None:
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        choice = QMessageBox.question(
            self, f"{role_label} needs approval",
            getattr(request, "prompt", "A worker wants to perform a sensitive action."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        session.resolve_confirmation(choice == QMessageBox.StandardButton.Yes)

    def _on_result_ready(self, result: str) -> None:
        self.summary_label.setText("Done - result recorded on the Mission.")

    def _on_failed(self, message: str) -> None:
        self.summary_label.setText(f"Could not complete: {message}")

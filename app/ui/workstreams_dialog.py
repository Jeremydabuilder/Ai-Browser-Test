"""Workstreams / Mission Plan: the UI for a Multi-Agent Mission (Phase 9,
extended by Phase 10's execution graph).

A compact activity view of what each node is doing - never a multi-chat
transcript, never raw chain-of-thought. Each row is exactly what
MissionCoordinator's WorkerTask already carries: the same structured
task/result record the coordinator itself works from. Selecting a row
shows its detail (role, status, findings contributed, error) and offers
Retry/Skip/Cancel where the coordinator would actually accept them - see
app/missions/coordinator.py's retry_node/skip_node/cancel.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.missions.coordinator import WorkerRole, WorkerState
from app.ui import theme

#: A glyph per state, chosen to read as a plain checklist at a glance -
#: "✓ Research product specs / ● Compare options / ○ Verify recommendation"
#: - never a developer-workflow-editor look.
_STATE_GLYPHS = {
    WorkerState.QUEUED: "○",              # ○
    WorkerState.RUNNING: "●",              # ●
    WorkerState.WAITING_FOR_APPROVAL: "!",
    WorkerState.DONE: "✓",                 # ✓
    WorkerState.FAILED: "✕",                # ✕
    WorkerState.SKIPPED: "–",                # –
    WorkerState.CANCELLED: "–",
    WorkerState.NEEDS_REVIEW: "?",
}
_STATE_LABELS = {
    WorkerState.QUEUED: "Waiting", WorkerState.RUNNING: "Running…",
    WorkerState.WAITING_FOR_APPROVAL: "Waiting for approval",
    WorkerState.DONE: "Done", WorkerState.FAILED: "Failed", WorkerState.SKIPPED: "Skipped",
    WorkerState.CANCELLED: "Cancelled", WorkerState.NEEDS_REVIEW: "Needs review",
}

_COLUMNS = ["Role", "Task", "State", "Findings added", "Result / Error"]


class WorkstreamsDialog(QDialog):
    """Shows every node in the current Multi-Agent Mission run as a
    compact checklist, plus a detail panel for whichever row is selected
    and Retry/Skip/Cancel actions where the coordinator would accept
    them. Also answers a worker's approval request the moment it appears."""

    def __init__(self, coordinator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mission Plan")
        self.resize(820, 480)
        self._coordinator = coordinator
        self._rows_by_id: dict[int, int] = {}
        self._tasks_by_id: dict[int, object] = {}
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
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._update_detail)
        layout.addWidget(self.table, 1)

        layout.addWidget(QLabel("Details", self))
        self.detail_text = QPlainTextEdit(self)
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(120)
        layout.addWidget(self.detail_text)

        buttons = QHBoxLayout()
        self.retry_button = QPushButton("Retry", self)
        self.retry_button.clicked.connect(self._on_retry)
        buttons.addWidget(self.retry_button)
        self.skip_button = QPushButton("Skip", self)
        self.skip_button.clicked.connect(self._on_skip)
        buttons.addWidget(self.skip_button)
        self.cancel_button = QPushButton("Cancel Mission", self)
        self.cancel_button.clicked.connect(self._on_cancel_mission)
        buttons.addWidget(self.cancel_button)
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
        self._update_detail()

    def _row_for(self, task) -> int:
        if task.id in self._rows_by_id:
            return self._rows_by_id[task.id]
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows_by_id[task.id] = row
        return row

    def _on_worker_added(self, task) -> None:
        self._tasks_by_id[task.id] = task
        row = self._row_for(task)
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        self.table.setItem(row, 0, QTableWidgetItem(role_label))
        self.table.setItem(row, 1, QTableWidgetItem(task.title))
        self._refresh_row(row, task)

    def _on_worker_changed(self, task) -> None:
        self._tasks_by_id[task.id] = task
        row = self._row_for(task)
        self._refresh_row(row, task)
        self._update_detail()

    def _refresh_row(self, row: int, task) -> None:
        glyph = _STATE_GLYPHS.get(task.state, "")
        label = _STATE_LABELS.get(task.state, task.state)
        self.table.setItem(row, 2, QTableWidgetItem(f"{glyph} {label}"))
        self.table.setItem(row, 3, QTableWidgetItem(str(task.findings_added)))
        detail = task.error or task.result
        self.table.setItem(row, 4, QTableWidgetItem(detail))

    def _selected_task(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        node_id = next((nid for nid, r in self._rows_by_id.items() if r == row), None)
        return self._tasks_by_id.get(node_id) if node_id is not None else None

    def _update_detail(self) -> None:
        task = self._selected_task()
        has = task is not None
        self.retry_button.setEnabled(
            has and task.state in (WorkerState.FAILED, WorkerState.NEEDS_REVIEW,
                                   WorkerState.SKIPPED))
        self.skip_button.setEnabled(has and task.state == WorkerState.QUEUED)
        if not has:
            self.detail_text.setPlainText("")
            return
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        lines = [
            f"Role: {role_label}", f"Status: {_STATE_LABELS.get(task.state, task.state)}",
            f"Findings contributed: {task.findings_added}",
            f"Attempts: {task.attempt_count}",
        ]
        if task.result:
            lines.append(f"Result: {task.result}")
        if task.error:
            lines.append(f"Error: {task.error}")
        self.detail_text.setPlainText("\n".join(lines))

    def _on_retry(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if not self._coordinator.retry_node(task.id):
            QMessageBox.information(self, "Cannot retry",
                                    "This step cannot be retried right now.")

    def _on_skip(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if not self._coordinator.skip_node(task.id):
            QMessageBox.information(self, "Cannot skip",
                                    "This step cannot be skipped right now.")

    def _on_cancel_mission(self) -> None:
        self._coordinator.cancel()

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

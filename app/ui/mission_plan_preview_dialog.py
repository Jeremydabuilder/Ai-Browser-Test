""""Py plans to do N steps" - shown before a large Mission actually runs,
when the coordinator was built with require_plan_approval=True. Simple
editing only (rename a step, drop an optional one) - never a full visual
DAG editor, per the phase's own PLAN TRANSPARENCY section.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.missions.coordinator import WorkerRole
from app.ui import theme


class MissionPlanPreviewDialog(QDialog):
    def __init__(self, coordinator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mission Plan")
        self.resize(480, 420)
        self._coordinator = coordinator
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        self.summary_label = QLabel("Py is planning…", self)
        layout.addWidget(self.summary_label)

        self.list_widget = QListWidget(self)
        layout.addWidget(self.list_widget, 1)

        edit_row = QHBoxLayout()
        self.rename_button = QPushButton("Rename Step", self)
        self.rename_button.clicked.connect(self._on_rename)
        edit_row.addWidget(self.rename_button)
        self.remove_button = QPushButton("Remove Step", self)
        self.remove_button.clicked.connect(self._on_remove)
        edit_row.addWidget(self.remove_button)
        edit_row.addStretch(1)
        layout.addLayout(edit_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Cancel", self)
        cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(cancel_button)
        self.start_button = QPushButton("Start", self)
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._on_start)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

        coordinator.worker_added.connect(self._on_worker_added)
        coordinator.plan_ready.connect(self._on_plan_ready)

    def _on_worker_added(self, task) -> None:
        if task.role == WorkerRole.PLANNER:
            return
        item = QListWidgetItem(self._label_for(task))
        item.setData(1, task.id)
        self.list_widget.addItem(item)

    def _label_for(self, task) -> str:
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        return f"{role_label}: {task.title}"

    def _on_plan_ready(self) -> None:
        count = self.list_widget.count()
        self.summary_label.setText(
            f"Py plans to do {count} step{'s' if count != 1 else ''}:")

    def _selected_node_id(self) -> int | None:
        item = self.list_widget.currentItem()
        return item.data(1) if item is not None else None

    def _on_rename(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        item = self.list_widget.currentItem()
        current = item.text().split(": ", 1)[-1]
        title, ok = QInputDialog.getText(self, "Rename Step", "New title:", text=current)
        if not ok or not title.strip():
            return
        if self._coordinator.rename_task(node_id, title.strip()):
            task = next((t for t in self._coordinator.tasks if t.id == node_id), None)
            if task is not None:
                item.setText(self._label_for(task))

    def _on_remove(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        if not self._coordinator.remove_task(node_id):
            QMessageBox.information(
                self, "Cannot remove this step",
                "Another step in the plan needs this one's result, so it cannot be "
                "removed - and the final Write step is always kept.")
            return
        row = self.list_widget.currentRow()
        self.list_widget.takeItem(row)

    def _on_start(self) -> None:
        self._coordinator.start_execution()
        self.accept()

    def _on_cancel(self) -> None:
        self._coordinator.reject_plan()
        self.reject()

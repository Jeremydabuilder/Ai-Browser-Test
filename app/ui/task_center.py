"""Task Center: the UI for Scheduled Missions (Phase 7).

Pure presentation over app/storage/scheduled_tasks.py and
app/missions/task_runner.py - creating a schedule here never runs anything
itself; it only writes a ScheduledTask row and lets the TaskRunner's own
timer (or "Run now") decide when it actually fires through the one
AgentSession the window owns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.missions.scheduler import ScheduleKind, TaskState, compute_next_run, utc_now
from app.ui import theme

_STATE_LABELS = {
    TaskState.QUEUED: "Queued",
    TaskState.RUNNING: "Running",
    TaskState.WAITING_FOR_APPROVAL: "Waiting for approval",
    TaskState.PAUSED: "Paused",
    TaskState.COMPLETED: "Completed",
    TaskState.FAILED: "Failed",
}

_COLUMNS = ["Mission", "State", "Next run", "Last run", "Duration", "Error"]


def _fmt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


class NewScheduleDialog(QDialog):
    """Create a schedule: what to do, and when. What to do is always a
    goal - the same free-text a typed message to Py already is - and firing
    it later creates or resumes a Mission exactly the way MainWindow's own
    _maybe_start_mission would for that same text, see TaskRunner._fire."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Scheduled Mission")
        self.resize(460, 420)
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        layout.addWidget(QLabel("What should Py do?", self))
        self.goal_edit = QPlainTextEdit(self)
        self.goal_edit.setPlaceholderText(
            "e.g. Check the flight price for SFO-> NRT and tell me if it drops")
        self.goal_edit.setMaximumHeight(90)
        layout.addWidget(self.goal_edit)

        layout.addWidget(QLabel("When", self))
        self.kind_combo = QComboBox(self)
        self.kind_combo.addItem("Once, at a specific time", ScheduleKind.ONCE)
        self.kind_combo.addItem("Daily, at a time of day", ScheduleKind.DAILY)
        self.kind_combo.addItem("Weekly, on a day and time", ScheduleKind.WEEKLY)
        self.kind_combo.addItem("Every N minutes/hours", ScheduleKind.INTERVAL)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        layout.addWidget(self.kind_combo)

        self.once_edit = QDateTimeEdit(self)
        self.once_edit.setCalendarPopup(True)
        self.once_edit.setDateTime(datetime.now() + timedelta(hours=1))
        layout.addWidget(self.once_edit)

        self.time_edit = QTimeEdit(self)
        self.time_edit.setDisplayFormat("HH:mm")
        layout.addWidget(self.time_edit)

        weekday_row = QHBoxLayout()
        weekday_row.addWidget(QLabel("Day of week", self))
        self.weekday_combo = QComboBox(self)
        for name in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
            self.weekday_combo.addItem(name)
        weekday_row.addWidget(self.weekday_combo, 1)
        self._weekday_row = QWidget(self)
        self._weekday_row.setLayout(weekday_row)
        layout.addWidget(self._weekday_row)

        interval_row = QHBoxLayout()
        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(1, 10_000)
        self.interval_spin.setValue(30)
        interval_row.addWidget(self.interval_spin)
        self.interval_unit_combo = QComboBox(self)
        self.interval_unit_combo.addItem("minutes", 60)
        self.interval_unit_combo.addItem("hours", 3600)
        interval_row.addWidget(self.interval_unit_combo, 1)
        self._interval_row = QWidget(self)
        self._interval_row.setLayout(interval_row)
        layout.addWidget(self._interval_row)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        create = QPushButton("Create", self)
        create.setDefault(True)
        create.clicked.connect(self._on_create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

        self._on_kind_changed()

    def _on_kind_changed(self) -> None:
        kind = self.kind_combo.currentData()
        self.once_edit.setVisible(kind == ScheduleKind.ONCE)
        self.time_edit.setVisible(kind in (ScheduleKind.DAILY, ScheduleKind.WEEKLY))
        self._weekday_row.setVisible(kind == ScheduleKind.WEEKLY)
        self._interval_row.setVisible(kind == ScheduleKind.INTERVAL)

    def _on_create(self) -> None:
        if not self.goal_edit.toPlainText().strip():
            QMessageBox.warning(self, "Nothing to schedule",
                                "Say what Py should do when this fires.")
            return
        self.accept()

    def result_fields(self) -> dict:
        """The plain values NewScheduleDialog collected - kept separate from
        ScheduledTaskStore.create() so this widget never needs to import the
        storage layer."""
        kind = self.kind_combo.currentData()
        fields = {
            "goal": self.goal_edit.toPlainText().strip(),
            "schedule_kind": kind,
            "schedule_at": None, "time_of_day": None, "weekday": None,
            "interval_seconds": None,
        }
        if kind == ScheduleKind.ONCE:
            dt = self.once_edit.dateTime().toPython()
            fields["schedule_at"] = dt.replace(tzinfo=timezone.utc).isoformat()
        elif kind == ScheduleKind.DAILY:
            t = self.time_edit.time()
            fields["time_of_day"] = f"{t.hour():02d}:{t.minute():02d}"
        elif kind == ScheduleKind.WEEKLY:
            t = self.time_edit.time()
            fields["time_of_day"] = f"{t.hour():02d}:{t.minute():02d}"
            fields["weekday"] = self.weekday_combo.currentIndex()
        elif kind == ScheduleKind.INTERVAL:
            unit = self.interval_unit_combo.currentData()
            fields["interval_seconds"] = self.interval_spin.value() * unit
        return fields


class TaskCenterDialog(QDialog):
    def __init__(
        self, store, task_runner, missions, parent: QWidget | None = None,
        current_workspace_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Task Center")
        self.resize(760, 420)
        self._store = store
        self._runner = task_runner
        self._missions = missions
        #: Phase 17: a task scheduled here fires bound to whatever workspace
        #: was current at scheduling time - see TaskRunner._fire /
        #: app/workspaces/. Never guessed from the goal text.
        self._current_workspace_id = current_workspace_id
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        self.table = QTableWidget(0, len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        new_button = QPushButton("New Schedule…", self)
        new_button.clicked.connect(self._on_new)
        buttons.addWidget(new_button)
        self.run_now_button = QPushButton("Run Now", self)
        self.run_now_button.clicked.connect(self._on_run_now)
        buttons.addWidget(self.run_now_button)
        self.pause_button = QPushButton("Pause", self)
        self.pause_button.clicked.connect(self._on_pause)
        buttons.addWidget(self.pause_button)
        self.resume_button = QPushButton("Resume", self)
        self.resume_button.clicked.connect(self._on_resume)
        buttons.addWidget(self.resume_button)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.clicked.connect(self._on_delete)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self._update_button_state)
        self.refresh()
        self._update_button_state()

    def refresh(self) -> None:
        tasks = self._store.all()
        self.table.setRowCount(len(tasks))
        self._task_ids = [t.id for t in tasks]
        for row, task in enumerate(tasks):
            label = task.mission_title or task.goal[:60]
            values = [
                label,
                _STATE_LABELS.get(task.state, task.state),
                _fmt(task.next_run_at),
                _fmt(task.last_run_at),
                f"{task.last_duration_s:.1f}s" if task.last_duration_s is not None else "—",
                task.last_error or "—",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 5 and task.last_error:
                    item.setToolTip(task.last_error)
                self.table.setItem(row, col, item)

    def _selected_task_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._task_ids):
            return None
        return self._task_ids[row]

    def _selected_task(self):
        task_id = self._selected_task_id()
        return self._store.get(task_id) if task_id is not None else None

    def _update_button_state(self) -> None:
        task = self._selected_task()
        has = task is not None
        self.delete_button.setEnabled(has)
        self.run_now_button.setEnabled(
            has and task.state in (TaskState.QUEUED, TaskState.PAUSED))
        self.pause_button.setEnabled(has and task.state == TaskState.QUEUED)
        self.resume_button.setEnabled(has and task.state == TaskState.PAUSED)

    def _on_new(self) -> None:
        dialog = NewScheduleDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fields = dialog.result_fields()
        now = utc_now()
        next_run = compute_next_run(
            fields["schedule_kind"], now=now, schedule_at=fields["schedule_at"],
            time_of_day=fields["time_of_day"], weekday=fields["weekday"],
            interval_seconds=fields["interval_seconds"])
        self._store.create(
            goal=fields["goal"], schedule_kind=fields["schedule_kind"],
            schedule_at=fields["schedule_at"], time_of_day=fields["time_of_day"],
            weekday=fields["weekday"], interval_seconds=fields["interval_seconds"],
            next_run_at=next_run.isoformat() if next_run else None,
            workspace_id=self._current_workspace_id)
        self.refresh()

    def _on_run_now(self) -> None:
        task_id = self._selected_task_id()
        if task_id is None:
            return
        if not self._runner.run_now(task_id):
            QMessageBox.information(
                self, "Py is busy",
                "Wait for the current task to finish before running another one now.")
        self.refresh()

    def _on_pause(self) -> None:
        task_id = self._selected_task_id()
        if task_id is not None:
            self._runner.pause(task_id)
            self.refresh()

    def _on_resume(self) -> None:
        task_id = self._selected_task_id()
        if task_id is not None:
            self._runner.resume(task_id)
            self.refresh()

    def _on_delete(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        from app.ui.dialogs import confirm_destructive

        label = task.mission_title or task.goal[:60]
        if confirm_destructive(self, "Delete schedule?",
                               f'Delete the schedule for "{label}"? This cannot be undone.',
                               "Delete"):
            self._store.remove(task.id)
            self.refresh()

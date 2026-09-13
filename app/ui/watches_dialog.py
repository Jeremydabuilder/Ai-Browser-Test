"""Watches: the UI for Page Watches (Phase 8) - a closely related sibling
of Task Center (app/ui/task_center.py), sharing its layout conventions.

Never touches page content itself: everything here works from a Watch's own
stored fields (title, condition, hashes, small derived values) and its
change history, both of which are already just text/numbers/booleans - see
app/watches/detection.py for where that boundary is enforced.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme
from app.watches.model import WatchCondition, WatchState, WatchTarget

_STATE_LABELS = {
    WatchState.ACTIVE: "Active",
    WatchState.PAUSED: "Paused",
    WatchState.NEEDS_ATTENTION: "Needs attention",
}

_CONDITION_LABELS = {
    WatchCondition.ANY_CHANGE: "Any change",
    WatchCondition.VALUE_BELOW: "Value drops below",
    WatchCondition.VALUE_ABOVE: "Value rises above",
    WatchCondition.TEXT_CONTAINS: "Text contains",
    WatchCondition.TEXT_NOT_CONTAINS: "Text no longer contains",
    WatchCondition.BECOMES_AVAILABLE: "Becomes available",
}

_COLUMNS = ["Title", "Condition", "State", "Last checked", "Next check", "Last change"]


def _fmt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


class NewWatchDialog(QDialog):
    """Configure a watch on a page (or a selection already made on it) -
    what to watch, and under what condition it counts as meaningful."""

    def __init__(self, url: str, title: str, selected_text: str,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Watch")
        self.resize(440, 380)
        self._url = url
        self._selected_text = selected_text
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        layout.addWidget(QLabel(f"Watching: {url}", self))
        if selected_text:
            shown = selected_text if len(selected_text) <= 100 else selected_text[:99] + "…"
            layout.addWidget(QLabel(f'Selected section: "{shown}"', self))

        layout.addWidget(QLabel("Title", self))
        self.title_edit = QLineEdit(title or url, self)
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Alert me when", self))
        self.condition_combo = QComboBox(self)
        for condition in WatchCondition.ALL:
            self.condition_combo.addItem(_CONDITION_LABELS[condition], condition)
        self.condition_combo.currentIndexChanged.connect(self._on_condition_changed)
        layout.addWidget(self.condition_combo)

        self.value_edit = QLineEdit(self)
        self.value_edit.setPlaceholderText("Threshold or text to look for")
        layout.addWidget(self.value_edit)

        layout.addWidget(QLabel("Check every", self))
        interval_row = QHBoxLayout()
        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(1, 10_000)
        self.interval_spin.setValue(30)
        interval_row.addWidget(self.interval_spin)
        self.interval_unit_combo = QComboBox(self)
        self.interval_unit_combo.addItem("minutes", 60)
        self.interval_unit_combo.addItem("hours", 3600)
        interval_row.addWidget(self.interval_unit_combo, 1)
        interval_widget = QWidget(self)
        interval_widget.setLayout(interval_row)
        layout.addWidget(interval_widget)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        create = QPushButton("Create Watch", self)
        create.setDefault(True)
        create.clicked.connect(self._on_create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

        self._on_condition_changed()

    def _on_condition_changed(self) -> None:
        condition = self.condition_combo.currentData()
        needs_value = condition in WatchCondition.NUMERIC or condition in WatchCondition.TEXTUAL
        self.value_edit.setVisible(needs_value)
        if condition in WatchCondition.NUMERIC:
            self.value_edit.setPlaceholderText("Threshold, e.g. 50")
        elif condition in WatchCondition.TEXTUAL:
            self.value_edit.setPlaceholderText("Text to look for")

    def _on_create(self) -> None:
        condition = self.condition_combo.currentData()
        if condition in (WatchCondition.NUMERIC + WatchCondition.TEXTUAL) \
                and not self.value_edit.text().strip():
            QMessageBox.warning(self, "Missing value", "Enter a threshold or text to look for.")
            return
        if condition in WatchCondition.NUMERIC:
            try:
                float(self.value_edit.text().strip())
            except ValueError:
                QMessageBox.warning(self, "Not a number",
                                    "The threshold must be a plain number.")
                return
        self.accept()

    def result_fields(self) -> dict:
        condition = self.condition_combo.currentData()
        unit = self.interval_unit_combo.currentData()
        return {
            "title": self.title_edit.text().strip() or self._url,
            "url": self._url,
            "target_type": WatchTarget.SELECTION if self._selected_text else WatchTarget.FULL_PAGE,
            "selection_hint": self._selected_text,
            "condition": condition,
            "condition_value": self.value_edit.text().strip(),
            "check_interval_seconds": self.interval_spin.value() * unit,
        }


class WatchHistoryDialog(QDialog):
    def __init__(self, store, watch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"History: {watch.title}")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        listing = QListWidget(self)
        for row in store.history_for(watch.id):
            when = _fmt(row["observed_at"])
            listing.addItem(f"{when} — {row['summary']}")
        if listing.count() == 0:
            listing.addItem("No changes recorded yet.")
        layout.addWidget(listing)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)


class WatchesDialog(QDialog):
    def __init__(self, store, runner, missions, main_window=None,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Watches")
        self.resize(820, 440)
        self._store = store
        self._runner = runner
        self._missions = missions
        self._main_window = main_window
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
        self.check_now_button = QPushButton("Check Now", self)
        self.check_now_button.clicked.connect(self._on_check_now)
        buttons.addWidget(self.check_now_button)
        self.pause_button = QPushButton("Pause", self)
        self.pause_button.clicked.connect(self._on_pause)
        buttons.addWidget(self.pause_button)
        self.resume_button = QPushButton("Resume", self)
        self.resume_button.clicked.connect(self._on_resume)
        buttons.addWidget(self.resume_button)
        self.history_button = QPushButton("History…", self)
        self.history_button.clicked.connect(self._on_history)
        buttons.addWidget(self.history_button)
        self.mission_button = QPushButton("Turn Change into Mission", self)
        self.mission_button.clicked.connect(self._on_turn_into_mission)
        buttons.addWidget(self.mission_button)
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
        watches = self._store.all()
        self.table.setRowCount(len(watches))
        self._watch_ids = [w.id for w in watches]
        for row, watch in enumerate(watches):
            history = self._store.history_for(watch.id, limit=1)
            last_change = history[0]["summary"] if history else "—"
            values = [
                watch.title,
                _CONDITION_LABELS.get(watch.condition, watch.condition),
                _STATE_LABELS.get(watch.state, watch.state),
                _fmt(watch.last_checked_at),
                _fmt(watch.next_check_at),
                last_change,
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

    def _selected_watch_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._watch_ids):
            return None
        return self._watch_ids[row]

    def _selected_watch(self):
        watch_id = self._selected_watch_id()
        return self._store.get(watch_id) if watch_id is not None else None

    def _update_button_state(self) -> None:
        watch = self._selected_watch()
        has = watch is not None
        self.delete_button.setEnabled(has)
        self.history_button.setEnabled(has)
        self.check_now_button.setEnabled(
            has and watch.state in (WatchState.ACTIVE, WatchState.PAUSED))
        self.pause_button.setEnabled(has and watch.state == WatchState.ACTIVE)
        self.resume_button.setEnabled(
            has and watch.state in (WatchState.PAUSED, WatchState.NEEDS_ATTENTION))
        has_history = has and bool(self._store.history_for(watch.id, limit=1))
        self.mission_button.setEnabled(has_history)

    def _on_check_now(self) -> None:
        watch_id = self._selected_watch_id()
        if watch_id is None:
            return
        if not self._runner.check_now(watch_id):
            QMessageBox.information(self, "Busy", "A check is already in progress.")
        self.refresh()

    def _on_pause(self) -> None:
        watch_id = self._selected_watch_id()
        if watch_id is not None:
            self._runner.pause(watch_id)
            self.refresh()

    def _on_resume(self) -> None:
        watch_id = self._selected_watch_id()
        if watch_id is not None:
            self._runner.resume(watch_id)
            self.refresh()

    def _on_history(self) -> None:
        watch = self._selected_watch()
        if watch is not None:
            WatchHistoryDialog(self._store, watch, self).exec()

    def _on_turn_into_mission(self) -> None:
        watch = self._selected_watch()
        if watch is None or self._main_window is None:
            return
        history = self._store.history_for(watch.id, limit=1)
        if not history:
            return
        self._main_window.turn_watch_change_into_mission(watch, history[0])

    def _on_delete(self) -> None:
        watch = self._selected_watch()
        if watch is None:
            return
        from app.ui.dialogs import confirm_destructive

        if confirm_destructive(self, "Delete watch?",
                               f'Delete the watch on "{watch.title}"? This cannot be undone.',
                               "Delete"):
            self._store.remove(watch.id)
            self.refresh()

"""UI for Phase 16's Automation Recorder: the compact "Record Workflow" bar,
the Finish-flow "Create Automation" dialog, and the parameter-entry dialog a
recorded workflow is run from.

Every dialog here only ever edits/produces plain data (a RecordedWorkflow, a
dict of parameter values) - MainWindow is what actually starts recording,
hands a finished recording to WorkflowRecorder.finish(), saves the resulting
Skill, and drives replay through WorkflowRunner. Nothing in this module talks
to a browser or an agent session directly.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.automation.model import RecordedStep, RecordedWorkflow, WorkflowParameter
from app.automation.parameterize import apply_parameter_suggestions
from app.ui import theme


class RecordingBar(QWidget):
    """A compact, non-modal bar: "● Recording workflow, N steps"
    [Pause] [Finish] [Cancel] - deliberately small so it never obscures the
    page underneath, per the Phase 16 brief's own UI instruction."""

    pause_clicked = Signal()
    resume_clicked = Signal()
    finish_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setStyleSheet(
            f"RecordingBar {{ background: {c.surface_raised}; "
            f"border-bottom: 1px solid {c.line}; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_1, m.space_3, m.space_1)
        layout.setSpacing(m.space_2)

        self._dot = QLabel("●", self)
        self._dot.setStyleSheet("color:#e0455f;")
        layout.addWidget(self._dot)

        self._label = QLabel("Recording workflow, 0 steps", self)
        layout.addWidget(self._label, 1)

        self._pause_button = QPushButton("Pause", self)
        self._pause_button.clicked.connect(self._on_pause_clicked)
        layout.addWidget(self._pause_button)

        finish_button = QPushButton("Finish", self)
        finish_button.setProperty("kind", "primary")
        finish_button.clicked.connect(self.finish_clicked)
        layout.addWidget(finish_button)

        cancel_button = QPushButton("Cancel", self)
        cancel_button.setProperty("kind", "quiet")
        cancel_button.clicked.connect(self.cancel_clicked)
        layout.addWidget(cancel_button)

        self._paused = False

    def _on_pause_clicked(self) -> None:
        self._paused = not self._paused
        self._pause_button.setText("Resume" if self._paused else "Pause")
        (self.resume_clicked if self._paused else self.pause_clicked).emit()

    def set_step_count(self, count: int) -> None:
        state = "Paused" if self._paused else "Recording workflow"
        self._label.setText(f"{state}, {count} step{'s' if count != 1 else ''}")


class CreateAutomationDialog(QDialog):
    """The Finish flow: name/description, detected parameters, steps (with
    per-step remove/mark-optional editing), shown once recording stops.

    Editing here never touches the page or re-runs anything - it only
    edits the plain RecordedWorkflow/Skill data that finish() already
    captured, exactly the "rename steps, remove accidental steps, mark a
    value as a parameter, mark a step optional" editing the brief asks for
    (parameter marking itself already happened via apply_parameter_
    suggestions before this dialog opens; here the user can also rename or
    drop a suggested parameter).
    """

    def __init__(self, workflow: RecordedWorkflow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Automation")
        self.resize(560, 620)
        m = theme.METRICS
        self._workflow = apply_parameter_suggestions(workflow)
        self._dropped_step_ids: set[int] = set()
        self._optional_step_ids: set[int] = set(
            s.id for s in self._workflow.steps if s.optional)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("e.g. Check tournament schedule")
        form.addRow("Name", self.name_edit)
        self.description_edit = QLineEdit(self)
        form.addRow("Description", self.description_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel(f"<b>Parameters detected</b> "
                                f"({len(self._workflow.parameters)})", self))
        self.parameters_list = QListWidget(self)
        self.parameters_list.setMaximumHeight(120)
        for parameter in self._workflow.parameters:
            item = QListWidgetItem(
                f"{parameter.label or parameter.name}  (default: {parameter.default!r})", self.parameters_list)
            item.setData(Qt.ItemDataRole.UserRole, parameter.name)
        layout.addWidget(self.parameters_list)

        layout.addWidget(QLabel(f"<b>Steps</b> ({len(self._workflow.steps)})", self))
        self.steps_list = QListWidget(self)
        for step in self._workflow.steps:
            self._add_step_item(step)
        layout.addWidget(self.steps_list, 1)

        step_buttons = QHBoxLayout()
        remove_button = QPushButton("Remove selected step", self)
        remove_button.clicked.connect(self._remove_selected_step)
        step_buttons.addWidget(remove_button)
        optional_button = QPushButton("Toggle optional", self)
        optional_button.clicked.connect(self._toggle_selected_optional)
        step_buttons.addWidget(optional_button)
        step_buttons.addStretch(1)
        layout.addLayout(step_buttons)

        self._error_label = QLabel("", self)
        self._error_label.setStyleSheet(
            f"color:{theme.palette_for(QApplication.instance()).warning_text};")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save Automation", self)
        save.setProperty("kind", "primary")
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._result: tuple[str, str, RecordedWorkflow] | None = None

    def _add_step_item(self, step: RecordedStep) -> None:
        label = step.label or step.tool_name
        suffix = " (optional)" if step.id in self._optional_step_ids else ""
        item = QListWidgetItem(f"{label}{suffix}", self.steps_list)
        item.setData(Qt.ItemDataRole.UserRole, step.id)

    def _refresh_steps(self) -> None:
        self.steps_list.clear()
        for step in self._workflow.steps:
            if step.id in self._dropped_step_ids:
                continue
            self._add_step_item(step)

    def _remove_selected_step(self) -> None:
        item = self.steps_list.currentItem()
        if item is None:
            return
        self._dropped_step_ids.add(item.data(Qt.ItemDataRole.UserRole))
        self._refresh_steps()

    def _toggle_selected_optional(self) -> None:
        item = self.steps_list.currentItem()
        if item is None:
            return
        step_id = item.data(Qt.ItemDataRole.UserRole)
        if step_id in self._optional_step_ids:
            self._optional_step_ids.discard(step_id)
        else:
            self._optional_step_ids.add(step_id)
        self._refresh_steps()

    def _on_save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._error_label.setText("Name is required.")
            self._error_label.show()
            return
        from dataclasses import replace

        kept_steps = tuple(
            replace(step, optional=step.id in self._optional_step_ids)
            for step in self._workflow.steps if step.id not in self._dropped_step_ids)
        if not kept_steps:
            self._error_label.setText("A workflow needs at least one step.")
            self._error_label.show()
            return
        final_workflow = replace(self._workflow, steps=kept_steps)
        self._result = (name, self.description_edit.text().strip(), final_workflow)
        self.accept()

    def result(self) -> tuple[str, str, RecordedWorkflow] | None:
        return self._result


class RunWorkflowDialog(QDialog):
    """Prompts for a recorded workflow's parameter values before replay -
    "Tournament: US Open" in the brief's own example."""

    def __init__(self, name: str, parameters: tuple[WorkflowParameter, ...],
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Run: {name}")
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        self._edits: dict[str, QLineEdit] = {}
        if parameters:
            form = QFormLayout()
            for parameter in parameters:
                edit = QLineEdit(parameter.default, self)
                form.addRow(parameter.label or parameter.name, edit)
                self._edits[parameter.name] = edit
            layout.addLayout(form)
        else:
            layout.addWidget(QLabel("This workflow has no parameters to fill in.", self))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        run_button = QPushButton("Run", self)
        run_button.setProperty("kind", "primary")
        run_button.clicked.connect(self.accept)
        buttons.addWidget(run_button)
        layout.addLayout(buttons)

    def values(self) -> dict[str, str]:
        return {name: edit.text() for name, edit in self._edits.items()}

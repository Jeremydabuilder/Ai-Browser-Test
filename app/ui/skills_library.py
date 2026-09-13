"""Skills Library: browse built-in and custom Skills, create/edit/duplicate/
delete a custom one, and run any of them.

A Skill here is pure configuration (app/agent/skills.py) - this module only
ever builds and edits that data and hands a chosen Skill back to the caller
(MainWindow._run_skill) to actually run. It contains no agent loop, no tool
dispatch, and no approval logic of its own.
"""

from __future__ import annotations

import json
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.agent.config import PROVIDERS
from app.agent.skills import BUILTIN_SKILLS, Skill
from app.agent.tools import TOOL_NAMES
from app.storage.skills import SkillStore
from app.ui import theme
from app.ui.dialogs import confirm_destructive


def _tool_scope_summary(skill: Skill) -> str:
    if skill.allowed_tools is None:
        return "All tools"
    if not skill.allowed_tools:
        return "No tools"
    if len(skill.allowed_tools) <= 3:
        return ", ".join(sorted(skill.allowed_tools))
    return f"{len(skill.allowed_tools)} tools"


class SkillEditDialog(QDialog):
    """Create or edit a custom Skill. A built-in is never opened here -
    MainWindow only ever hands this a custom Skill, or nothing (create)."""

    def __init__(self, parent: QWidget | None = None, *, skill: Skill | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Skill" if skill is not None else "Create Skill")
        self.resize(560, 620)
        m = theme.METRICS
        self._editing_id = skill.id if skill is not None else None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        layout.addWidget(QLabel("Name", self))
        self.name_edit = QLineEdit(skill.name if skill else "", self)
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Description", self))
        self.description_edit = QLineEdit(skill.description if skill else "", self)
        layout.addWidget(self.description_edit)

        layout.addWidget(QLabel("Instructions", self))
        self.instructions_edit = QPlainTextEdit(skill.instructions if skill else "", self)
        self.instructions_edit.setPlaceholderText(
            "What should Py do every time this Skill runs?")
        layout.addWidget(self.instructions_edit, 1)

        layout.addWidget(QLabel("Allowed tools", self))
        self.unrestricted_checkbox = QCheckBox("Allow every tool (no restriction)", self)
        self.unrestricted_checkbox.setChecked(skill is None or skill.allowed_tools is None)
        self.unrestricted_checkbox.toggled.connect(self._on_unrestricted_toggled)
        layout.addWidget(self.unrestricted_checkbox)

        self.tools_list = QListWidget(self)
        self.tools_list.setMaximumHeight(160)
        allowed = set(skill.allowed_tools) if skill and skill.allowed_tools is not None else set()
        for name in sorted(TOOL_NAMES):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in allowed
                              else Qt.CheckState.Unchecked)
            self.tools_list.addItem(item)
        self.tools_list.setEnabled(not self.unrestricted_checkbox.isChecked())
        layout.addWidget(self.tools_list)

        layout.addWidget(QLabel("Output schema (JSON, optional)", self))
        self.output_schema_edit = QPlainTextEdit(self)
        self.output_schema_edit.setMaximumHeight(90)
        self.output_schema_edit.setPlaceholderText(
            '{"type": "object", "required": ["summary"]}')
        if skill and skill.output_schema:
            self.output_schema_edit.setPlainText(json.dumps(skill.output_schema, indent=2))
        layout.addWidget(self.output_schema_edit)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Preferred provider", self))
        self.provider_combo = QComboBox(self)
        self.provider_combo.addItem("(no preference)", "")
        for info in PROVIDERS:
            self.provider_combo.addItem(info.label, info.id)
        if skill and skill.preferred_provider:
            index = self.provider_combo.findData(skill.preferred_provider)
            if index != -1:
                self.provider_combo.setCurrentIndex(index)
        provider_row.addWidget(self.provider_combo, 1)
        layout.addLayout(provider_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Preferred model", self))
        self.model_edit = QLineEdit(skill.preferred_model if skill else "", self)
        self.model_edit.setPlaceholderText("Optional - e.g. claude-opus-5")
        model_row.addWidget(self.model_edit, 1)
        layout.addLayout(model_row)

        self._error_label = QLabel("", self)
        self._error_label.setStyleSheet(f"color:{theme.palette_for(QApplication.instance()).warning_text};")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save", self)
        save.setProperty("kind", "primary")
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._result: Skill | None = None

    def _on_unrestricted_toggled(self, checked: bool) -> None:
        self.tools_list.setEnabled(not checked)

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()

    def _on_save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._show_error("Name is required.")
            return
        output_schema = None
        schema_text = self.output_schema_edit.toPlainText().strip()
        if schema_text:
            try:
                output_schema = json.loads(schema_text)
            except ValueError as exc:
                self._show_error(f"Output schema is not valid JSON: {exc}")
                return
            if not isinstance(output_schema, dict):
                self._show_error("Output schema must be a JSON object.")
                return
        if self.unrestricted_checkbox.isChecked():
            allowed_tools = None
        else:
            allowed_tools = tuple(
                self.tools_list.item(i).text() for i in range(self.tools_list.count())
                if self.tools_list.item(i).checkState() == Qt.CheckState.Checked)
        skill_id = self._editing_id or f"custom-{uuid.uuid4().hex[:12]}"
        self._result = Skill(
            id=skill_id, name=name, description=self.description_edit.text().strip(),
            instructions=self.instructions_edit.toPlainText().strip(),
            allowed_tools=allowed_tools, output_schema=output_schema,
            preferred_provider=self.provider_combo.currentData() or "",
            preferred_model=self.model_edit.text().strip(),
        )
        self.accept()

    def result_skill(self) -> Skill | None:
        return self._result


class _SkillRow(QFrame):
    def __init__(self, skill: Skill, parent: QWidget | None = None, *,
                on_run, on_edit=None, on_duplicate=None, on_delete=None) -> None:
        super().__init__(parent)
        m = theme.METRICS
        c = theme.palette_for(QApplication.instance())
        self.skill = skill
        self.setStyleSheet(f"QFrame {{ border-bottom: 1px solid {c.line}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_2, m.space_2, m.space_2, m.space_2)
        layout.setSpacing(2)

        title = QLabel(f"<b>{skill.name}</b>" + ("" if not skill.builtin else " · built-in"), self)
        layout.addWidget(title)
        if skill.description:
            desc = QLabel(skill.description, self)
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color:{c.muted};")
            layout.addWidget(desc)
        scope = QLabel(f"Tools: {_tool_scope_summary(skill)}", self)
        scope.setStyleSheet(f"color:{c.disabled}; font-size:{m.text_xs}px;")
        layout.addWidget(scope)

        buttons = QHBoxLayout()
        buttons.setSpacing(m.space_1)
        run_button = QPushButton("Run", self)
        run_button.setProperty("kind", "primary")
        run_button.clicked.connect(lambda: on_run(skill))
        buttons.addWidget(run_button)
        if on_edit is not None and not skill.builtin:
            edit_button = QPushButton("Edit", self)
            edit_button.clicked.connect(lambda: on_edit(skill))
            buttons.addWidget(edit_button)
        if on_duplicate is not None:
            dup_button = QPushButton("Duplicate", self)
            dup_button.clicked.connect(lambda: on_duplicate(skill))
            buttons.addWidget(dup_button)
        if on_delete is not None and not skill.builtin:
            delete_button = QPushButton("Delete", self)
            delete_button.setProperty("kind", "danger")
            delete_button.clicked.connect(lambda: on_delete(skill))
            buttons.addWidget(delete_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)


class SkillsLibraryDialog(QDialog):
    def __init__(self, store: SkillStore, parent: QWidget | None = None, *, on_run=None) -> None:
        super().__init__(parent)
        self._store = store
        self._on_run = on_run
        self.setWindowTitle("Skills")
        self.resize(640, 620)
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Built-in Skills</b> and <b>My Skills</b>", self), 1)
        create_button = QPushButton("Create Skill", self)
        create_button.setProperty("kind", "primary")
        create_button.clicked.connect(self._create_skill)
        header.addWidget(create_button)
        layout.addLayout(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._container = QWidget(self._scroll)
        self._rows_layout = QVBoxLayout(self._container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.setProperty("kind", "quiet")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        self.refresh()

    def refresh(self) -> None:
        while self._rows_layout.count() > 1:
            taken = self._rows_layout.takeAt(0)
            widget = taken.widget() if taken is not None else None
            if widget is not None:
                widget.deleteLater()
        custom = self._store.all()
        for skill in list(BUILTIN_SKILLS) + custom:
            row = _SkillRow(
                skill, self._container, on_run=self._run,
                on_edit=self._edit_skill, on_duplicate=self._duplicate_skill,
                on_delete=self._delete_skill)
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    def _run(self, skill: Skill) -> None:
        if self._on_run is not None:
            self._on_run(skill)
        self.accept()

    def _create_skill(self) -> None:
        dialog = SkillEditDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            skill = dialog.result_skill()
            if skill is not None:
                self._store.save(skill)
                self.refresh()

    def _edit_skill(self, skill: Skill) -> None:
        dialog = SkillEditDialog(self, skill=skill)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.result_skill()
            if updated is not None:
                self._store.save(updated)
                self.refresh()

    def _duplicate_skill(self, skill: Skill) -> None:
        new_id = f"custom-{uuid.uuid4().hex[:12]}"
        copy = skill.duplicated(new_id=new_id)
        self._store.save(copy)
        self.refresh()

    def _delete_skill(self, skill: Skill) -> None:
        if not confirm_destructive(
                self, "Delete Skill", f'Delete "{skill.name}"?',
                "Delete", informative="This cannot be undone."):
            return
        self._store.remove(skill.id)
        self.refresh()

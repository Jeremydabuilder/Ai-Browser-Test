"""The dialog for filling in a Routine's variables before running it."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.routines.model import Routine


class RoutineRunDialog(QDialog):
    """One field per editable argument across the Routine's steps.

    Pre-filled with what was recorded, so running it unchanged reproduces the
    taught sequence exactly; changing a field is how "same routine, different
    input" - a different origin city, a different search term - actually works.
    """

    def __init__(self, routine: Routine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Run “{routine.name}”")
        self._fields: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{len(routine.steps)} step{'s' if len(routine.steps) != 1 else ''}. "
            "Review the inputs below, then run.", self))

        form = QFormLayout()
        for step in routine.steps:
            for key in step.variable_keys():
                slot = step.slot(key)
                field = QLineEdit(str(step.args.get(key, "")), self)
                self._fields[slot] = field
                form.addRow(f"Step {step.position + 1} · {key}", field)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def overrides(self) -> dict[str, str]:
        return {slot: field.text() for slot, field in self._fields.items()}

    @staticmethod
    def ask(parent: QWidget, routine: Routine) -> dict[str, str] | None:
        """Show the dialog; return the overrides, or None if cancelled."""
        dialog = RoutineRunDialog(routine, parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.overrides()
        return None

""""Suggest Groups": review AI/heuristic tab-group suggestions before any
tab actually moves. Nothing here touches a tab - it only ever hands the
window a list of accepted suggestions; the window is what calls
TabManager.create_group/move_tab_to_group, and only for what was checked.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.browser.tab_grouping import GroupSuggestion
from app.ui import theme


class SuggestGroupsDialog(QDialog):
    """One checkbox per suggested group, each showing the tab titles it
    would contain. Unchecked by default is deliberately not the choice
    here - a suggestion the user opened this dialog to see is assumed
    wanted unless they say otherwise, but nothing is created until they
    click Create; closing or Cancel leaves every tab exactly as it was.
    """

    def __init__(self, suggestions: list[GroupSuggestion], titles_by_index: dict[int, str],
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setWindowTitle("Suggested Tab Groups")
        self.resize(420, 420)
        self._checks: list[tuple[QCheckBox, GroupSuggestion]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        if not suggestions:
            layout.addWidget(QLabel(
                "Py couldn't find any clear groups among your open tabs.", self))
        else:
            heading = QLabel(
                f"{len(suggestions)} suggested "
                f"{'group' if len(suggestions) == 1 else 'groups'}. "
                "Nothing is created until you click Create.", self)
            heading.setWordWrap(True)
            heading.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
            layout.addWidget(heading)

            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            box = QWidget(scroll)
            box_layout = QVBoxLayout(box)
            box_layout.setSpacing(m.space_3)
            for suggestion in suggestions:
                titles = ", ".join(titles_by_index.get(i, f"tab {i}")
                                  for i in suggestion.tab_indices)
                check = QCheckBox(suggestion.name, box)
                check.setChecked(True)
                sub = QLabel(titles, box)
                sub.setWordWrap(True)
                sub.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px; "
                                 f"margin-left:{m.space_4}px;")
                box_layout.addWidget(check)
                box_layout.addWidget(sub)
                self._checks.append((check, suggestion))
            box_layout.addStretch(1)
            scroll.setWidget(box)
            layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel, self)
        self.create_button = buttons.addButton(
            "Create Selected Groups" if suggestions else "Close",
            QDialogButtonBox.ButtonRole.AcceptRole)
        self.create_button.setProperty("kind", "primary")
        self.create_button.setEnabled(bool(suggestions))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accepted_suggestions(self) -> list[GroupSuggestion]:
        return [suggestion for check, suggestion in self._checks if check.isChecked()]

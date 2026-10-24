"""The command palette (Ctrl+Shift+P): one keyboard-driven place to reach what the browser can do.

It owns no behaviour. Every entry is either an existing menu action (``QAction.trigger()``) or a call to the same
handler a menu, toolbar button or dialog already uses (switch tab, switch workspace, run a Skill, open a Mission...).
So there is no second execution path: the approval prompts, the AI-off switch and "this feature is unavailable" all
behave exactly as they do from the menus. The window decides which commands exist right now (AI entries are simply
absent while AI is off).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from app.ui import a11y, theme


@dataclass
class Command:
    title: str
    run: Callable[[], None]
    group: str = ""
    shortcut: str = ""
    keywords: str = ""


def score(query: str, command: Command) -> int:
    """0 = no match. Every word of the query must appear (as a prefix of a word, else anywhere); earlier,
    title-based and prefix matches rank higher."""
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    if not words:
        return 1
    title = command.title.lower()
    haystack = f"{title} {command.group.lower()} {command.keywords.lower()}"
    total = 1
    for word in words:
        if word not in haystack:
            return 0
        if word in title:
            total += 10 + (6 if re.search(rf"\b{re.escape(word)}", title) else 0)
            total += 5 if title.startswith(word) else 0
        else:
            total += 2
    return total


def rank(query: str, commands: list[Command], limit: int = 60) -> list[Command]:
    scored = [(score(query, c), index, c) for index, c in enumerate(commands)]
    kept = [t for t in scored if t[0] > 0]
    if query.strip():
        kept.sort(key=lambda t: (-t[0], t[1]))
    return [c for _s, _i, c in kept[:limit]]


class _KeyForwarder(QObject):
    """Up/Down/PageUp/PageDown in the search box move the list selection, so the keyboard never leaves the box."""

    def __init__(self, palette: "CommandPalette") -> None:
        super().__init__(palette)
        self._palette = palette

    def eventFilter(self, obj, event):  # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 8, Qt.Key.Key_PageUp: -8}.get(event.key())
            if step is not None:
                self._palette.move(step)
                return True
        return False


class CommandPalette(QDialog):
    def __init__(self, provider: Callable[[str], list[Command]], parent: QWidget | None = None) -> None:
        """``provider(query)`` returns the commands for the current text (the window adds typed-text commands)."""
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self._provider = provider
        self._shown: list[Command] = []
        self.chosen: Command | None = None
        self.setWindowTitle("Command Palette")
        self.setModal(True)
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_3, m.space_3, m.space_3)
        layout.setSpacing(m.space_2)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Type a command, a tab, a workspace or a question…")
        a11y.describe(self.field, "Command palette search",
                      "Type to filter. Up and Down choose a result, Enter runs it, Escape closes.")
        a11y.enable_clear(self.field, "search")
        self.field.textChanged.connect(self.refresh)
        self.field.returnPressed.connect(self.run_selected)
        self.field.installEventFilter(_KeyForwarder(self))
        layout.addWidget(self.field)

        self.results = QListWidget(self)
        a11y.describe(self.results, "Command palette results", "Press Enter to run the highlighted command")
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)         # focus stays in the box; arrows are forwarded
        self.results.itemActivated.connect(lambda _item: self.run_selected())
        self.results.itemClicked.connect(lambda _item: self.run_selected())
        layout.addWidget(self.results, 1)

        self.status = QLabel("", self)
        self.status.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
        a11y.describe(self.status, "Number of results")
        layout.addWidget(self.status)
        self.setFocusProxy(self.field)
        self.refresh()

    # -- the list
    def refresh(self, _text: str = "") -> None:
        query = self.field.text()
        self._shown = rank(query, self._provider(query))
        self.results.clear()
        for command in self._shown:
            tail = f"    {command.shortcut}" if command.shortcut else ""
            label = f"{command.title}{tail}"
            item = QListWidgetItem(label)
            if command.group:
                item.setToolTip(command.group)
                item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, command.group)
            self.results.addItem(item)
        if self._shown:
            self.results.setCurrentRow(0)
        self.status.setText(f"{len(self._shown)} result{'s' if len(self._shown) != 1 else ''}" if self._shown
                            else "Nothing matches")

    def move(self, step: int) -> None:
        if not self._shown:
            return
        row = max(0, min(len(self._shown) - 1, self.results.currentRow() + step))
        self.results.setCurrentRow(row)

    def selected(self) -> Command | None:
        row = self.results.currentRow()
        return self._shown[row] if 0 <= row < len(self._shown) else None

    def run_selected(self) -> None:
        command = self.selected()
        if command is None:
            return
        self.chosen = command
        self.accept()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().showEvent(event)
        self.field.setFocus()


def open_palette(parent: QWidget, provider: Callable[[str], list[Command]]) -> Command | None:
    """Show the palette; once it has closed, run the chosen command on the next event-loop turn (so the palette is
    gone first and any dialog/prompt the command opens is parented correctly). Returns the chosen command."""
    palette = CommandPalette(provider, parent)
    palette.exec()
    command = palette.chosen
    if command is not None:
        QTimer.singleShot(0, command.run)
    return command

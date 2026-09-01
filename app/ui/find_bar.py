"""Find-in-page: the strip that appears on Ctrl+F.

Deliberately small. Qt WebEngine does the searching and the highlighting; this
widget only collects the query, reports how many matches there are, and steps
through them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


class FindBar(QFrame):
    """Emits search intent; the window drives the tab."""

    search_requested = Signal(str, bool)   # text, backward
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.field = QLineEdit(self)
        self.field.setPlaceholderText("Find in page")
        self.field.setClearButtonEnabled(True)
        # Search as you type, like every other browser.
        self.field.textChanged.connect(lambda text: self.search_requested.emit(text, False))
        self.field.returnPressed.connect(self._next)
        layout.addWidget(self.field, 1)

        self.status = QLabel("", self)
        self.status.setMinimumWidth(90)
        self.status.setStyleSheet("color:#666;")
        layout.addWidget(self.status)

        # Drawn icons, not text glyphs: "▲▼✕" render as blank boxes on any
        # system whose UI font lacks them, which is how these buttons looked
        # here - three empty pills.
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QApplication

        from app.ui import icons, theme

        colours = theme.palette_for(QApplication.instance())

        def step_button(name: str, tip: str, backward: bool | None) -> QPushButton:
            button = QPushButton(self)
            button.setIcon(icons.icon(name, colours.text, size=32, weight=2.2))
            button.setIconSize(QSize(15, 15))
            button.setFlat(True)
            button.setFixedWidth(30)
            button.setToolTip(tip)
            if backward is None:
                button.clicked.connect(self.close_bar)
            else:
                button.clicked.connect(
                    lambda _checked=False, back=backward: self._step(back))
            layout.addWidget(button)
            return button

        step_button("up", "Previous match (Shift+Enter)", True)
        step_button("down", "Next match (Enter)", False)
        step_button("close", "Close (Esc)", None)

        self.hide()

    # -- driving ---------------------------------------------------------
    def _next(self) -> None:
        self._step(False)

    def _step(self, backward: bool) -> None:
        text = self.field.text()
        if text:
            self.search_requested.emit(text, backward)

    def open_bar(self) -> None:
        self.show()
        self.field.setFocus()
        self.field.selectAll()
        if self.field.text():
            self.search_requested.emit(self.field.text(), False)

    def close_bar(self) -> None:
        self.hide()
        self.status.clear()
        self.closed.emit()

    def report(self, active: int, total: int) -> None:
        if not self.field.text():
            self.status.clear()
        elif total == 0:
            self.status.setText("No results")
        else:
            self.status.setText(f"{active} of {total}")

    # -- keys ------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._step(True)
            return
        super().keyPressEvent(event)

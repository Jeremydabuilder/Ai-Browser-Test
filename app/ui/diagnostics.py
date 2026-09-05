"""Tools -> Agent Diagnostics: a developer-facing view of what the agent did.

Separate on purpose from the user-facing activity log in AgentPanel. That log
answers "what is Py doing" for someone deciding whether to trust the result -
plain sentences, no tool names, no timings. This answers "why did the task
end the way it did" for someone debugging it: every event tracing.py records,
in order, with the reason a call was rejected or a tool failed. It reads
directly from AgentSession.trace, which already exists and is already
recording everything below - this dialog is the one thing that was missing:
somewhere to look at it.

Nothing here is shown unless a person opens it from the Tools menu, so it
adds no noise to the normal browsing or agent experience.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.agent.session import AgentSession, AgentState


#: How a raw session state reads to someone who did not just watch it happen.
_STATE_LABELS = {
    AgentState.IDLE: "idle - no task in progress",
    AgentState.THINKING: "thinking - waiting on the model",
    AgentState.ACTING: "acting - running a tool",
    AgentState.AWAITING_CONFIRMATION: "paused - waiting for the user to approve an action",
    AgentState.CANCELLING: "cancelling",
}


class DiagnosticsDialog(QDialog):
    """Read-only. Reflects the trace at the moment it was opened; Refresh
    re-reads it, useful while a task is still running."""

    def __init__(self, session: AgentSession | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agent Diagnostics")
        self.resize(640, 480)
        self._session = session

        layout = QVBoxLayout(self)
        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self._text.font()
        font.setFamily("monospace")
        self._text.setFont(font)
        layout.addWidget(self._text, 1)

        buttons = QHBoxLayout()
        refresh_button = QPushButton("Refresh", self)
        refresh_button.clicked.connect(self._refresh)
        buttons.addWidget(refresh_button)
        copy_button = QPushButton("Copy", self)
        copy_button.clicked.connect(self._copy)
        buttons.addWidget(copy_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.setDefault(True)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._refresh()

    def _refresh(self) -> None:
        if self._session is None:
            self._summary.setText(
                "No AI agent session yet - open the AI panel (Ctrl+Shift+A) first.")
            self._text.setPlainText("")
            return
        events = self._session.trace.events
        state_label = _STATE_LABELS.get(self._session.state, self._session.state)
        if events:
            self._summary.setText(
                f"{len(events)} event(s) recorded for the current or most recent task. "
                f"Agent state: {state_label}.")
        else:
            self._summary.setText(
                f"No task has run yet this session. Agent state: {state_label}.")
        self._text.setPlainText(self._session.trace.export() or "(nothing recorded yet)")

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._text.toPlainText())

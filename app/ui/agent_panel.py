"""The agent panel: conversation, activity, input, and the confirmation prompt.

Deliberately plain. It shows what the agent said, what it is doing, and asks
for approval when the browser's safety layer demands it. It owns no agent
logic - every decision belongs to AgentSession, which this panel only watches.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.agent.session import AgentSession, AgentState, ConfirmationRequest


class _MessageBox(QPlainTextEdit):
    """Input where Enter sends and Shift+Enter makes a new line."""

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("What can I do for you?")
        self.setMaximumHeight(84)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ConfirmationBar(QFrame):
    """Allow / Deny strip shown when a sensitive action is pending."""

    answered = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "background:#fdf1d6; color:#5c3d00; border:1px solid #e6c56b; border-radius:4px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        buttons = QHBoxLayout()
        self.allow_button = QPushButton("Allow", self)
        self.deny_button = QPushButton("Deny", self)
        self.deny_button.setDefault(True)   # the safe answer is the default
        self.allow_button.clicked.connect(lambda: self.answered.emit(True))
        self.deny_button.clicked.connect(lambda: self.answered.emit(False))
        buttons.addStretch(1)
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.allow_button)
        layout.addLayout(buttons)
        self.hide()

    def ask(self, request: ConfirmationRequest) -> None:
        self._label.setText(request.prompt)
        self.show()
        self.deny_button.setFocus()


class AgentPanel(QWidget):
    """The right-hand panel. Install it with MainWindow.set_side_panel()."""

    def __init__(self, session: AgentSession | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(320)
        self._session = session

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        model = ""
        if session is not None:
            model = session.config.model_choice.label
        header = QLabel(
            "<b>AI Agent</b>" + (f" <span style='color:#777'>{model}</span>" if model else ""),
            self)
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        self.transcript = QTextBrowser(self)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setOpenLinks(False)
        layout.addWidget(self.transcript, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#666;")
        layout.addWidget(self.status)

        # Spend, shown while it is still possible to do something about it.
        # Hidden entirely until the first task, so an idle panel is not
        # cluttered with zeroes.
        self.usage = QLabel("", self)
        self.usage.setWordWrap(True)
        self.usage.setStyleSheet("color:#888; font-size:11px;")
        self.usage.hide()
        layout.addWidget(self.usage)

        self.confirmation = ConfirmationBar(self)
        layout.addWidget(self.confirmation)

        self.input = _MessageBox(self)
        layout.addWidget(self.input)

        buttons = QHBoxLayout()
        self.send_button = QPushButton("Send", self)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(False)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

        self.input.submitted.connect(self._send)
        self.send_button.clicked.connect(self._send)
        self.stop_button.clicked.connect(self._stop)
        self.confirmation.answered.connect(self._answer_confirmation)

        if session is None:
            self._show_unconfigured()
        else:
            self._connect(session)

    # -- wiring ----------------------------------------------------------
    def _connect(self, session: AgentSession) -> None:
        session.assistant_message.connect(self._on_assistant)
        session.activity.connect(self._on_activity)
        session.error.connect(self._on_error)
        session.state_changed.connect(self._on_state)
        session.confirmation_required.connect(self.confirmation.ask)
        session.finished.connect(self._on_finished)
        session.usage_updated.connect(self._on_usage)

    def _show_unconfigured(self) -> None:
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self._append(
            "system",
            "The agent is not configured. Add an Anthropic API key "
            "(Tools → Configure AI Agent…) and reopen this panel.",
        )

    # -- user actions ----------------------------------------------------
    def _send(self) -> None:
        if self._session is None:
            return
        text = self.input.toPlainText().strip()
        if not text or self._session.busy:
            return
        self.input.clear()
        self._append("user", text)
        self._session.send(text)

    def _stop(self) -> None:
        if self._session is not None:
            self._session.cancel()

    def _answer_confirmation(self, allowed: bool) -> None:
        self.confirmation.hide()
        if self._session is not None:
            self._session.resolve_confirmation(allowed)

    # -- session events --------------------------------------------------
    def _on_assistant(self, text: str) -> None:
        self._append("assistant", text)

    def _on_activity(self, text: str) -> None:
        self._append("activity", text)

    def _on_error(self, text: str) -> None:
        self._append("error", text)

    def _on_state(self, state: str) -> None:
        busy = state != AgentState.IDLE
        self.stop_button.setEnabled(busy)
        self.send_button.setEnabled(not busy)
        self.status.setText({
            AgentState.THINKING: "Claude is thinking…",
            AgentState.ACTING: "Working in the browser…",
            AgentState.AWAITING_CONFIRMATION: "Waiting for your approval…",
            AgentState.CANCELLING: "Stopping…",
            AgentState.IDLE: "",
        }.get(state, ""))

    def _on_usage(self, usage) -> None:
        """Show what this task has cost so far.

        Token counts are exact; any money figure is an estimate from published
        list prices and is labelled as one. It is shown per task rather than as
        a running total because a per-task number is the one a person can act
        on - it tells them whether the way they phrased the request was
        expensive.
        """
        model = self._session.config.model if self._session is not None else ""
        line = usage.summary(model)
        if not line:
            self.usage.hide()
            return
        self.usage.setText(f"This task: {line}")
        self.usage.show()

    def _on_finished(self) -> None:
        self.confirmation.hide()
        self.status.setText("")
        self.input.setFocus()

    # -- transcript ------------------------------------------------------
    def _append(self, kind: str, text: str) -> None:
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        html = {
            "user": f'<p style="margin:6px 0"><b>You:</b> {escaped}</p>',
            "assistant": f'<p style="margin:6px 0"><b>Claude:</b> {escaped}</p>',
            # Actions are shown, reasoning is not: the user should be able to
            # see what the agent did to their browser, not read its thoughts.
            "activity": f'<p style="margin:2px 0 2px 12px; color:#555">→ {escaped}</p>',
            "error": f'<p style="margin:6px 0; color:#a11">{escaped}</p>',
            "system": f'<p style="margin:6px 0; color:#666"><i>{escaped}</i></p>',
        }.get(kind, f"<p>{escaped}</p>")
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.insertHtml(html)
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

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

#: The prompts behind the quick-action buttons. Written as things a person
#: would actually say, because they go through the same path as anything the
#: user types - there is no second, hard-coded summarising system.
QUICK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("Summarise", "Summarise the page I am looking at."),
    ("Key points", "What are the main points on this page? Answer as a short list."),
    ("Explain", "Explain what this page is and who it is for, in plain language."),
)


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
        #: True while an answer is being written into the transcript piece by
        #: piece, so the finished message is not appended a second time.
        self._streaming = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        model = ""
        if session is not None:
            model = session.config.model_choice.label
        top = QHBoxLayout()
        header = QLabel(
            "<b>Py AI</b>" + (f" <span style='color:#777'>{model}</span>" if model else ""),
            self)
        header.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(header, 1)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.setToolTip("Forget this conversation and start again")
        self.clear_button.setFlat(True)
        self.clear_button.clicked.connect(self._clear)
        top.addWidget(self.clear_button)
        layout.addLayout(top)

        # Quick actions. These are not a separate system: each one sends an
        # ordinary message through the same session, so whatever the agent can
        # do by being asked, it does here too.
        self.quick = QHBoxLayout()
        self.quick.setSpacing(4)
        for label, prompt in QUICK_ACTIONS:
            button = QPushButton(label, self)
            button.setToolTip(prompt)
            button.clicked.connect(lambda _checked=False, text=prompt: self._ask(text))
            self.quick.addWidget(button)
        self.quick.addStretch(1)
        layout.addLayout(self.quick)

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
        session.assistant_delta.connect(self._on_delta)
        session.cleared.connect(self._on_cleared)

    def _show_unconfigured(self) -> None:
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        for index in range(self.quick.count()):
            widget = self.quick.itemAt(index).widget()
            if widget is not None:
                widget.setEnabled(False)
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

    def _clear(self) -> None:
        if self._session is None:
            return
        if self._session.busy:
            self._append("system", "Stop the current task before clearing.")
            return
        self._session.clear()

    def _ask(self, text: str) -> None:
        """Send a prepared message, exactly as if the user had typed it."""
        if self._session is None or self._session.busy:
            return
        self._append("user", text)
        self._session.send(text)

    def _answer_confirmation(self, allowed: bool) -> None:
        self.confirmation.hide()
        if self._session is not None:
            self._session.resolve_confirmation(allowed)

    # -- session events --------------------------------------------------
    def _on_assistant(self, text: str) -> None:
        if self._streaming:
            # Already on screen, written as it arrived.
            self._end_stream()
            return
        self._append("assistant", text)

    def _on_delta(self, fragment: str) -> None:
        """Write a fragment of the answer as it arrives.

        Inserted as plain text so a page that streams back something looking
        like markup is displayed, not rendered - the transcript shows what
        Claude said, and Claude is quoting untrusted pages.
        """
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._streaming:
            self._streaming = True
            self.transcript.setTextCursor(cursor)
            self.transcript.insertHtml('<p style="margin:6px 0"><b>Claude:</b> </p>')
            cursor = self.transcript.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(fragment)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _end_stream(self) -> None:
        self._streaming = False

    def _on_cleared(self) -> None:
        self.transcript.clear()
        self.usage.hide()
        self._streaming = False
        self._append("system", "Conversation cleared.")

    def _on_activity(self, text: str) -> None:
        self._append("activity", text)

    def _on_error(self, text: str) -> None:
        self._append("error", text)

    def _on_state(self, state: str) -> None:
        busy = state != AgentState.IDLE
        self.stop_button.setEnabled(busy)
        self.send_button.setEnabled(not busy)
        for index in range(self.quick.count()):
            widget = self.quick.itemAt(index).widget()
            if widget is not None:
                widget.setEnabled(not busy)
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
        self._end_stream()
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

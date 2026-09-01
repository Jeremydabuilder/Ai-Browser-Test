"""The agent panel: conversation, activity, input, and the confirmation prompt.

Deliberately plain. It shows what the agent said, what it is doing, and asks
for approval when the browser's safety layer demands it. It owns no agent
logic - every decision belongs to AgentSession, which this panel only watches.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ui import icons, theme
from app.ui.mascot import Mascot, MascotState, state_for_agent

from app.agent.tools import READ_ONLY_TOOLS
from app.agent.session import (
    AgentSession,
    AgentState,
    ConfirmationRequest,
    Step,
    StepState,
)

#: How each step state reads: a mark, and which palette colour names it. A step
#: is a thing the agent did to the browser, never a thing it thought - the panel
#: shows actions, not reasoning.
#:
#: The marks are drawn from the geometric-shapes block rather than from emoji,
#: which render inconsistently and sometimes not at all.
_STEP_MARKS = {
    StepState.RUNNING: ("&#9679;", "accent"),    # filled dot: happening now
    StepState.DONE: ("&#10003;", "success"),     # tick
    StepState.FAILED: ("&#10007;", "danger"),    # cross
    StepState.WAITING: ("&#9679;", "warning"),
    StepState.SKIPPED: ("&#9675;", "disabled"),  # hollow dot: never happened
}

#: The prompts behind the quick-action buttons. Written as things a person
#: would actually say, because they go through the same path as anything the
#: user types - there is no second, hard-coded summarising system.
QUICK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("Summarise", "Summarise the page I am looking at."),
    ("Key points", "What are the main points on this page? Answer as a short list."),
    ("Explain", "Explain what this page is and who it is for, in plain language."),
)


class _MessageBox(QPlainTextEdit):
    """Input where Enter sends and Shift+Enter makes a new line.

    Grows with what you type, up to a few lines, then scrolls. A box that is
    always four lines tall is four lines of empty space for the 95% of messages
    that are one line long.
    """

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Ask Py about this page\u2026")
        self.setAccessibleName("Message to Py")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._min_height = 38
        self._max_height = 132
        self.setFixedHeight(self._min_height)
        self.document().contentsChanged.connect(self._fit_to_content)

    def _fit_to_content(self) -> None:
        wanted = int(self.document().size().height()) + 16
        self.setFixedHeight(max(self._min_height, min(self._max_height, wanted)))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if enter and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ConfirmationBar(QFrame):
    """The approval card: what would happen, where, and why we are asking.

    Deliberately the most legible thing in the panel. A person reads this while
    deciding whether to let software spend their money, so it states the action,
    the site and the reason as three separate lines rather than one sentence,
    and Deny is the default - the safe answer should be the one you get by
    pressing Enter without reading carefully.
    """

    answered = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.ui import theme

        self._colours = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        c = self._colours
        self.setObjectName("approval")
        # Scoped to the object name, so the labels inside do not inherit a
        # border and background of their own - which is what made this look
        # like a box inside a box.
        self.setStyleSheet(
            f"#approval {{ background: {c.warning_soft};"
            f" border: 1px solid {c.warning};"
            f" border-radius: {m.radius_lg}px; }}"
            f"#approval QLabel {{ background: transparent; border: none;"
            f" color: {c.warning_text}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_3, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        heading = QHBoxLayout()
        heading.setSpacing(m.space_2)
        self._glyph = QLabel(self)
        self._glyph.setPixmap(
            icons.icon("sparkle", c.warning, size=32).pixmap(m.icon, m.icon))
        heading.addWidget(self._glyph, 0, Qt.AlignmentFlag.AlignTop)
        self._title = QLabel("", self)
        self._title.setWordWrap(True)
        self._title.setTextFormat(Qt.TextFormat.RichText)
        heading.addWidget(self._title, 1)
        layout.addLayout(heading)

        self._detail = QLabel("", self)
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._detail)

        buttons = QHBoxLayout()
        buttons.setSpacing(m.space_2)
        self.deny_button = QPushButton("Deny", self)
        self.deny_button.setDefault(True)      # the safe answer is the default
        self.allow_button = QPushButton("Allow", self)
        self.allow_button.setProperty("kind", "primary")
        self.allow_button.clicked.connect(lambda: self.answered.emit(True))
        self.deny_button.clicked.connect(lambda: self.answered.emit(False))
        buttons.addStretch(1)
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.allow_button)
        layout.addLayout(buttons)
        self.hide()

    def ask(self, request: ConfirmationRequest) -> None:
        """Show what would happen, where, and what would be sent.

        Three things decide whether this is safe to allow, and all three are
        stated rather than left to be inferred: the action, the site it lands
        on, and - for a form - which fields go with it. Field names only; a
        confirmation prompt is not a place to display someone's password back
        to them.
        """
        def escape(text: str) -> str:
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        action = escape(request.description)
        site = f' on <b>{escape(request.site)}</b>' if request.site else ""
        self._title.setText(
            f'<span style="font-size:11px;letter-spacing:.04em;opacity:.75">'
            f"PY WANTS TO</span><br>"
            f'<span style="font-size:14px;font-weight:600">{action}</span>{site}')

        lines = []
        if request.reasons:
            lines.append(f"This {escape(', '.join(request.reasons))}.")
        if request.submits:
            shown = ", ".join(escape(name) for name in request.submits[:6])
            more = f" and {len(request.submits) - 6} more" if len(request.submits) > 6 else ""
            lines.append(f"<b>Sends:</b> {shown}{more}.")
        if request.sensitive_fields:
            lines.append("<b>One of those is a password or payment field.</b>")
        self._detail.setText("<br>".join(lines))
        self._detail.setVisible(bool(lines))
        self.show()
        self.deny_button.setFocus()


class AgentPanel(QWidget):
    """The right-hand panel. Install it with MainWindow.set_side_panel()."""

    def __init__(self, session: AgentSession | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        m = theme.METRICS
        self._colours = theme.palette_for(QApplication.instance())
        c = self._colours
        self.setMinimumWidth(m.panel_min)
        self._session = session
        #: True while an answer is being written into the transcript piece by
        #: piece, so the finished message is not appended a second time.
        self._streaming = False
        #: The step checklist for the task in progress.
        self._steps_by_index: dict[int, Step] = {}
        #: True while the transcript is showing the invitation rather than a
        #: conversation, so the first message replaces it instead of following it.
        self._empty = True
        #: How the task in progress is going. Together these decide what Py
        #: shows when it ends: only a task that actually answered, and was
        #: neither stopped nor broken, gets the finished face.
        self._answered = False
        self._failed = False
        self._stopped = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        model = ""
        if session is not None:
            model = session.config.model_choice.label
        # -- header: who is answering, and how to start over ---------------
        top = QHBoxLayout()
        top.setSpacing(m.space_2)
        # Py anchors the header: one place that says how the task is going,
        # readable without reading. The line underneath says the same thing in
        # words, for when a glance is not enough.
        self.mascot = Mascot(m.mascot_panel, self)
        self.mascot.clicked.connect(lambda: self.input.setFocus())
        top.addWidget(self.mascot)

        name_block = QVBoxLayout()
        name_block.setSpacing(0)
        header = QLabel("Py", self)
        header.setStyleSheet(f"font-size:{m.text_lg}px; font-weight:600;")
        name_block.addWidget(header)
        self.companion = QLabel(self.mascot.companion_text(), self)
        self.companion.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
        name_block.addWidget(self.companion)
        top.addLayout(name_block)
        if model:
            badge = QLabel(model.replace(" (default)", ""), self)
            badge.setToolTip(f"Answers come from {model}. Change it in "
                             "Tools \u2192 Configure AI Agent.")
            badge.setStyleSheet(
                f"color:{c.muted}; font-size:{m.text_xs}px;"
                f" background:{c.surface_alt}; border-radius:{m.radius_sm}px;"
                f" padding:2px {m.space_2}px;")
            top.addWidget(badge)
        top.addStretch(1)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.setProperty("kind", "quiet")
        self.clear_button.setToolTip("Forget this conversation and start again")
        self.clear_button.clicked.connect(self._clear)
        top.addWidget(self.clear_button)
        layout.addLayout(top)

        # Quick actions. These are not a separate system: each one sends an
        # ordinary message through the same session, so whatever the agent can
        # do by being asked, it does here too.
        self.quick = QHBoxLayout()
        self.quick.setSpacing(m.space_1)
        for label, prompt in QUICK_ACTIONS:
            button = QPushButton(label, self)
            button.setProperty("kind", "chip")
            button.setToolTip(prompt)
            button.clicked.connect(lambda _checked=False, text=prompt: self._ask(text))
            self.quick.addWidget(button)
        self.quick.addStretch(1)
        layout.addLayout(self.quick)

        self.transcript = QTextBrowser(self)
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setOpenLinks(False)
        # Flat: the conversation is the panel's content, not a widget sitting
        # in it. A bordered box around a mostly-empty transcript is the single
        # thing that made this panel look like a form.
        self.transcript.setProperty("kind", "flat")
        self.transcript.setAccessibleName("Conversation with Py")
        layout.addWidget(self.transcript, 1)

        # What the agent is doing, as a checklist that updates in place rather
        # than a log that scrolls. Sized to its contents and hidden between
        # tasks, so an idle panel is just a conversation.
        # A hairline above the checklist, so the space between the last
        # message and the steps reads as separation rather than as a gap
        # someone forgot to fill.
        self.steps_rule = QFrame(self)
        self.steps_rule.setFrameShape(QFrame.Shape.HLine)
        self.steps_rule.setFixedHeight(1)
        self.steps_rule.setStyleSheet(f"background:{c.line}; border:none;")
        self.steps_rule.hide()
        layout.addWidget(self.steps_rule)

        self.steps = QTextBrowser(self)
        self.steps.setProperty("kind", "flat")
        self.steps.setOpenLinks(False)
        self.steps.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.steps.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.steps.setAccessibleName("What Py is doing")
        self.steps.hide()
        layout.addWidget(self.steps)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setProperty("kind", "muted")
        self.status.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        layout.addWidget(self.status)

        # Spend, shown while it is still possible to do something about it.
        # Hidden entirely until the first task, so an idle panel is not
        # cluttered with zeroes.
        self.usage = QLabel("", self)
        self.usage.setWordWrap(True)
        self.usage.setStyleSheet(f"color:{c.disabled}; font-size:{m.text_xs}px;")
        self.usage.hide()
        layout.addWidget(self.usage)

        self.confirmation = ConfirmationBar(self)
        layout.addWidget(self.confirmation)

        self.input = _MessageBox(self)
        layout.addWidget(self.input)

        buttons = QHBoxLayout()
        buttons.setSpacing(m.space_2)
        self.hint = QLabel("Enter to send", self)
        self.hint.setStyleSheet(f"color:{c.disabled}; font-size:{m.text_xs}px;")
        buttons.addWidget(self.hint)
        buttons.addStretch(1)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setProperty("kind", "danger")
        self.stop_button.setToolTip("Stop the current task")
        self.stop_button.setEnabled(False)
        self.stop_button.hide()          # only shown while something is running
        self.send_button = QPushButton("Send", self)
        self.send_button.setProperty("kind", "primary")
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
            self._show_empty_state()

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
        session.step_changed.connect(self._on_step)
        self.mascot.state_changed.connect(self._on_mascot_state)

    def _show_empty_state(self) -> None:
        """What the panel says before it has been asked anything.

        An invitation with two concrete examples, not a blank box: the hardest
        part of using an assistant is knowing what it can be asked.
        """
        self._empty = True
        c = self._colours
        m = theme.METRICS
        # <p>, not <div>: Qt's rich text lays consecutive divs out inline
        # here, which ran the heading and the body together on one line.
        self.transcript.setHtml(
            f'<p style="color:{c.text};font-size:{m.text}px;font-weight:600;'
            f'margin:{m.space_5}px 0 {m.space_2}px">Ask about the page you are on.</p>'
            f'<p style="color:{c.muted};font-size:{m.text_sm}px;margin:0 0 {m.space_4}px">'
            "Py can read this page, look across your tabs, and follow links "
            "to find things out.</p>"
            f'<p style="color:{c.disabled};font-size:{m.text_sm}px;margin:0">'
            "Try &ldquo;summarise this&rdquo; or &ldquo;compare my two tabs&rdquo;.</p>")

    def _show_unconfigured(self) -> None:
        """The panel with no credential: explain, and point at the fix.

        Everything is disabled rather than hidden, so the panel still looks
        like itself - a disabled control says "not yet", a missing one says
        "never".
        """
        c = self._colours
        m = theme.METRICS
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        for index in range(self.quick.count()):
            widget = self.quick.itemAt(index).widget()
            if widget is not None:
                widget.setEnabled(False)
        self.transcript.setHtml(
            f'<p style="color:{c.text};font-size:{m.text}px;font-weight:600;'
            f'margin:{m.space_5}px 0 {m.space_2}px">Py is not set up yet.</p>'
            f'<p style="color:{c.muted};font-size:{m.text_sm}px;margin:0 0 {m.space_3}px">'
            "Open <b>Tools \u2192 Configure AI Agent</b> to connect it. You can sign "
            "in with the Anthropic CLI, use cloud credentials you already have, "
            "or paste an API key.</p>"
            f'<p style="color:{c.disabled};font-size:{m.text_sm}px;margin:0">'
            "The rest of the browser works exactly as normal without it.</p>")

    # -- user actions ----------------------------------------------------
    def _send(self) -> None:
        if self._session is None:
            return
        text = self.input.toPlainText().strip()
        if not text or self._session.busy:
            return
        self.input.clear()
        self._answered = self._failed = self._stopped = False
        self._begin_conversation()
        self._append("user", text)
        self._session.send(text)

    def _stop(self) -> None:
        if self._session is not None:
            self._stopped = True
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
        self._answered = self._failed = self._stopped = False
        self._begin_conversation()
        self._append("user", text)
        self._session.send(text)

    def _begin_conversation(self) -> None:
        """Clear the invitation the first time something is actually asked."""
        self._steps_by_index.clear()
        if self._empty:
            self.transcript.clear()
            self._empty = False

    def _answer_confirmation(self, allowed: bool) -> None:
        self.confirmation.hide()
        if self._session is not None:
            self._session.resolve_confirmation(allowed)

    # -- session events --------------------------------------------------
    def _on_assistant(self, text: str) -> None:
        self._answered = True
        if self._streaming:
            # Already on screen, written as it arrived.
            self._end_stream()
            return
        self._append("assistant", text)

    def _on_delta(self, fragment: str) -> None:  # noqa: D401
        self._answered = True
        return self._write_delta(fragment)

    def _write_delta(self, fragment: str) -> None:
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
            self.transcript.insertHtml(
                f'<div style="margin:{theme.METRICS.space_2}px 0"></div>')
            cursor = self.transcript.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(fragment)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _end_stream(self) -> None:
        self._streaming = False

    def _on_cleared(self) -> None:
        self._steps_by_index.clear()
        self.steps.hide()
        self.steps_rule.hide()
        self.usage.hide()
        self._streaming = False
        self._show_empty_state()

    def _on_activity(self, text: str) -> None:
        # Activity now lives in the step list; the transcript keeps only the
        # conversation, which is what makes a long task readable afterwards.
        pass

    def _on_mascot_state(self, _state: str) -> None:
        """Say in words what Py is showing.

        Driven off the mascot rather than off the session, so the face and the
        line can never disagree - there is one source for both.
        """
        self.companion.setText(self.mascot.companion_text())

    def _on_step(self, step: Step) -> None:
        self._steps_by_index[step.index] = step
        if step.state == StepState.WAITING:
            # Waiting for the user outranks everything: Py must not look busy
            # while it is actually blocked on a decision.
            self.mascot.set_state(MascotState.APPROVAL)
        elif step.state == StepState.RUNNING and step.tool:
            # The session announces a gated step as RUNNING before marking it
            # WAITING, so without this guard the step would overwrite the
            # approval face with "On it." - Py claiming to be working while
            # actually asking permission.
            if self.mascot.state() != MascotState.APPROVAL:
                # Reading a page and clicking through one look different from
                # the outside, so they look different here too.
                self.mascot.set_state(
                    MascotState.READING if step.tool in READ_ONLY_TOOLS
                    else MascotState.WORKING)
        self._render_steps()

    def _render_steps(self) -> None:
        """Draw the checklist, and size the box to exactly what it holds.

        A fixed-height step box is empty space under a two-step task and a
        scrollbar under a ten-step one. Measuring the document is a few lines
        and removes both.
        """
        if not self._steps_by_index:
            self.steps.hide()
            self.steps_rule.hide()
            return
        c = self._colours
        rows = []
        for index in sorted(self._steps_by_index):
            step = self._steps_by_index[index]
            mark, role = _STEP_MARKS.get(step.state, ("&#9675;", "disabled"))
            colour = getattr(c, role, c.muted)
            body = "color:%s;" % c.disabled if step.state == StepState.SKIPPED else ""
            detail = (f' <span style="color:{c.muted}">&mdash; '
                      f"{self._escape(step.detail)}</span>" if step.detail else "")
            rows.append(
                f'<div style="margin:3px 0;{body}">'
                f'<span style="color:{colour}">{mark}</span>&nbsp;&nbsp;'
                f"{self._escape(step.description)}{detail}</div>")
        self.steps.setHtml(
            f'<div style="font-size:{theme.METRICS.text_sm}px">' + "".join(rows) + "</div>")
        self.steps_rule.show()
        self.steps.show()
        self.steps.document().setTextWidth(self.steps.viewport().width())
        wanted = int(self.steps.document().size().height()) + 4
        self.steps.setFixedHeight(min(wanted, 190))
        self.steps.verticalScrollBar().setValue(
            self.steps.verticalScrollBar().maximum())

    @staticmethod
    def _escape(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _on_error(self, text: str) -> None:
        self._failed = True
        self._append("error", text)

    def _on_state(self, state: str) -> None:
        # A finished task shows the "complete" face only if it actually
        # produced something - ending because it was stopped or because it
        # failed is not a success, and the character should not claim it was.
        self.mascot.set_state(state_for_agent(
            state, answered=self._answered, failed=self._failed or self._stopped))
        busy = state != AgentState.IDLE
        # Stop replaces Send rather than sitting next to it: only one of them
        # is ever the thing you want, and two live buttons is a decision the
        # user should not have to make.
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        self.send_button.setVisible(not busy)
        self.send_button.setEnabled(not busy)
        self.hint.setVisible(not busy)
        for index in range(self.quick.count()):
            widget = self.quick.itemAt(index).widget()
            if widget is not None:
                widget.setEnabled(not busy)
        # Py's line says what is happening, so the status bar only carries
        # what Py cannot: the one transitional state with no face for it.
        self.status.setText("Stopping\u2026" if state == AgentState.CANCELLING else "")

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
        """Add one message to the conversation.

        The user's words sit in a tinted bubble and Claude's run full width.
        Two bubble styles facing each other would halve the reading width in a
        panel this narrow, and Claude's answers are the long ones.
        """
        c = self._colours
        m = theme.METRICS
        escaped = self._escape(text).replace("\n", "<br>")
        html = {
            "user": (f'<table width="100%" cellpadding="0" cellspacing="0"'
                     f' style="margin:{m.space_2}px 0"><tr><td></td><td'
                     f' style="background:{c.accent_soft};border-radius:{m.radius_lg}px;'
                     f'padding:{m.space_2}px {m.space_3}px">{escaped}</td></tr></table>'),
            "assistant": f'<div style="margin:{m.space_2}px 0">{escaped}</div>',
            "error": (f'<div style="margin:{m.space_2}px 0;padding:{m.space_2}px '
                      f'{m.space_3}px;background:{c.danger_soft};'
                      f'border-radius:{m.radius_md}px;color:{c.danger}">{escaped}</div>'),
            "system": (f'<div style="margin:{m.space_2}px 0;color:{c.muted};'
                       f'font-size:{m.text_sm}px">{escaped}</div>'),
        }.get(kind, f"<div>{escaped}</div>")
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.insertHtml(html)
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.ensureCursorVisible()

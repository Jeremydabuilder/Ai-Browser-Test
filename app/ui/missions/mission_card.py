"""The active Mission, as it appears at the top of Py's panel.

Compact on purpose: a Mission that is running should tell you what you are
working on and what has been gathered, then get out of the way of the
conversation. Title, status, goal, pages, two actions.

This widget owns no state. It renders whatever MissionService says is active
and calls back into the service - which is what lets the whole panel be thrown
away and rebuilt without a Mission noticing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.missions.model import (
    MAX_FINDING_CHARS,
    Mission,
    MissionFinding,
    MissionPage,
    MissionStatus,
)
from app.ui import theme

#: Findings listed before the rest are summarised. Findings lead the card:
#: they are what the Mission is actually for.
VISIBLE_FINDINGS = 4

#: Pages listed before the rest are summarised. Fewer than the findings above
#: them, on purpose - a Mission panel is a reminder, not a file manager.
VISIBLE_PAGES = 3

#: A finding is folded to this many lines in the panel. The whole text stays in
#: the tooltip and in the editor - this is display, never storage.
FINDING_LINES = 2


class _ClickableLabel(QLabel):
    """A label that can be clicked.

    Used instead of a QPushButton because these need to wrap: a finding is a
    sentence, and a button will not break one across lines.
    """

    clicked = Signal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:      # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _TwoLineLabel(_ClickableLabel):
    """A clickable label that occupies exactly one or two lines, never more.

    Qt's own word wrap was the obvious choice and the wrong one here: a wrapped
    label's height depends on its width, a plain QWidget does not forward that
    question to its layout, and the result was findings drawn on top of each
    other. Wrapping the text here instead makes the height something this
    widget decides rather than something the layout has to discover - so four
    findings always fit, at any panel width, and a long one is folded with an
    ellipsis instead of pushing the card into a scrolling dashboard.
    """

    LINES = FINDING_LINES

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full = " ".join((text or "").split())
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(60)
        self._apply()

    def _apply(self) -> None:
        metrics = QFontMetrics(self.font())
        width = max(self.width(), 60)
        lines: list[str] = []
        remaining = self._full
        while remaining and len(lines) < self.LINES:
            if metrics.horizontalAdvance(remaining) <= width:
                lines.append(remaining)
                remaining = ""
                break
            if len(lines) == self.LINES - 1:
                lines.append(metrics.elidedText(
                    remaining, Qt.TextElideMode.ElideRight, width))
                remaining = ""
                break
            # Longest prefix that fits, broken at a space where one exists.
            cut = len(remaining)
            while cut > 1 and metrics.horizontalAdvance(remaining[:cut]) > width:
                cut -= 1
            space = remaining.rfind(" ", 0, cut + 1)
            if space > 0:
                cut = space
            lines.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        self.setText("\n".join(lines))
        self.setFixedHeight(metrics.lineSpacing() * max(len(lines), 1) + 2)

    def resizeEvent(self, event) -> None:            # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply()


class _FindingRow(QWidget):
    """One discovery, with where it came from.

    Two click targets and no other chrome: the text opens the editor, the
    domain opens the page. A hover-revealed delete button would be a 16px
    target in a 300px panel, and a permanent one would put a column of crosses
    down the side of the user's own notes.
    """

    edit_requested = Signal(object)      # MissionFinding
    source_requested = Signal(object)    # MissionFinding

    def __init__(self, finding: MissionFinding, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.finding = finding
        c = parent._colours
        m = theme.METRICS
        # A word-wrapped QLabel reports its height as a function of its width,
        # and a plain QWidget does not pass that question on to its layout.
        # Without this the row keeps the height of a single line and the second
        # line is drawn over the row below it.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(m.space_2)

        tick = QFrame(self)
        tick.setFixedWidth(2)
        tick.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        tick.setStyleSheet(f"background:{c.accent}; border:none;"
                           f" border-radius:1px;")
        row.addWidget(tick)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(1)

        self.text = _TwoLineLabel(finding.text, self)
        self.text.setStyleSheet(f"color:{c.text}; font-size:{m.text_sm}px;")
        self.text.setToolTip(f"{finding.text}\n\nClick to edit or delete")
        self.text.clicked.connect(lambda: self.edit_requested.emit(self.finding))
        body.addWidget(self.text)

        domain = finding.source_domain
        if domain:
            # Secondary by design: the discovery is the content, the source is
            # the footnote.
            self.source = _ClickableLabel(_elide(domain, 30), self)
            self.source.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
            self.source.setToolTip(f"{finding.source_title or domain}\n"
                                   f"{finding.source_url}\n\nClick to open this page")
            self.source.clicked.connect(
                lambda: self.source_requested.emit(self.finding))
            body.addWidget(self.source)
        row.addLayout(body, 1)


class _ElidedLabel(QLabel):
    """A label that shortens itself to fit, instead of pushing its neighbours.

    Page titles come from web pages and can be any length. Counting characters
    is not good enough here: the panel is resizable, and at its minimum width a
    long title was squeezing the domain down to "tennis-warehouse.co", which
    tells the user nothing. Qt knows the actual pixel width, so let it decide.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self._apply()

    def _apply(self) -> None:
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight,
                                        max(self.width(), 40)))

    def resizeEvent(self, event) -> None:   # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply()


class _PageRow(QPushButton):
    """One page. Clicking it focuses the tab, or reopens the page."""

    def __init__(self, page: MissionPage, live: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page = page
        c = (parent._colours if parent is not None
             else theme.palette_for(QApplication.instance()))
        m = theme.METRICS
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        # Not kind="quiet": that style hovers to surface_alt, which is this
        # card's own background, so the row would light up invisibly.
        self.setStyleSheet(
            "QPushButton { background:transparent; border:none; text-align:left;"
            f" border-radius:{m.radius_sm}px; padding:0; }}"
            f"QPushButton:hover {{ background:{c.surface}; }}")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(m.space_2)

        # A dot rather than the word "open": the state is glanceable and the
        # tooltip carries the sentence for anyone who needs it.
        dot = QLabel("●" if live else "○", self)
        dot.setFixedWidth(10)
        dot.setStyleSheet(
            f"color:{c.success if live else c.disabled}; font-size:{m.text_xs}px;")
        row.addWidget(dot)

        title = _ElidedLabel(page.display_title, self)
        title.setStyleSheet(f"color:{c.text}; font-size:{m.text_sm}px;")
        row.addWidget(title, 1)

        # The domain must not be the thing that gets squeezed: at the panel's
        # minimum width a long page title would otherwise clip it in half, and
        # half a domain tells you nothing.
        domain = QLabel(_elide(page.domain, 20), self)
        domain.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
        domain.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        row.addWidget(domain)

        self.setToolTip(("Showing in a tab — click to focus it"
                         if live else "Click to open this page again")
                        + f"\n{page.url}")
        self.setMinimumHeight(22)


class MissionCard(QFrame):
    """The active Mission. Hidden entirely when there is not one."""

    #: The user asked to leave/pause/finish. The panel decides what to say.
    changed = Signal()

    def __init__(self, service, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        # The application palette, not None: palette_for(None) silently
        # falls back to the light theme, which paints dark text on a dark
        # panel for every user on a dark desktop.
        self._colours = theme.palette_for(QApplication.instance())
        self._mission: Mission | None = None
        c = self._colours
        m = theme.METRICS

        self.setProperty("kind", "card")
        # An accent edge rather than a full border: it reads as "this is the
        # thing you are working on" without drawing a box inside a box.
        self.setStyleSheet(
            f"QFrame[kind='card'] {{ background:{c.surface_alt};"
            f" border:none; border-left:3px solid {c.accent};"
            f" border-radius:{m.radius_sm}px; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(m.space_3, m.space_3, m.space_3, m.space_3)
        outer.setSpacing(m.space_2)

        head = QHBoxLayout()
        head.setSpacing(m.space_2)
        self.title = QPushButton("", self)
        self.title.setProperty("kind", "quiet")
        self.title.setFlat(True)
        self.title.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title.setToolTip("Click to rename this mission")
        self.title.setStyleSheet(
            f"QPushButton {{ color:{c.text}; font-size:{m.text_lg}px; font-weight:600;"
            " background:transparent; border:none; padding:0; text-align:left; }"
            f"QPushButton:hover {{ color:{c.accent}; }}")
        self.title.clicked.connect(self._rename)
        head.addWidget(self.title)
        head.addStretch(1)
        self.status = QLabel("", self)
        head.addWidget(self.status)
        outer.addLayout(head)

        self.goal = QLabel("", self)
        self.goal.setWordWrap(True)
        self.goal.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        outer.addWidget(self.goal)

        # Findings first: the discoveries are what the Mission is for, the
        # pages are how it got them.
        self.findings_label = QLabel("", self)
        self.findings_label.setStyleSheet(
            f"color:{c.disabled}; font-size:{m.text_xs}px; font-weight:600;"
            " letter-spacing:0.06em;")
        outer.addWidget(self.findings_label)

        self._findings_box = QVBoxLayout()
        self._findings_box.setSpacing(0)
        outer.addLayout(self._findings_box)

        self.more_findings = QLabel("", self)
        self.more_findings.setStyleSheet(f"color:{c.disabled}; font-size:{m.text_xs}px;")
        self.more_findings.hide()
        outer.addWidget(self.more_findings)

        self.pages_label = QLabel("", self)
        self.pages_label.setStyleSheet(
            f"color:{c.disabled}; font-size:{m.text_xs}px; font-weight:600;"
            " letter-spacing:0.06em;")
        outer.addWidget(self.pages_label)

        self._pages_box = QVBoxLayout()
        self._pages_box.setSpacing(0)
        outer.addLayout(self._pages_box)

        self.more = QLabel("", self)
        self.more.setStyleSheet(f"color:{c.disabled}; font-size:{m.text_xs}px;")
        self.more.hide()
        outer.addWidget(self.more)

        actions = QHBoxLayout()
        actions.setSpacing(m.space_2)
        # Same reasoning as the page rows: styled here so hover is visible
        # against the card rather than against the panel.
        button_style = (
            f"QPushButton {{ background:{c.surface}; color:{c.muted}; border:none;"
            f" border-radius:{m.radius_sm}px; min-height:{m.control_sm}px;"
            f" padding:0 {m.space_3}px; font-size:{m.text_sm}px; }}"
            f"QPushButton:hover {{ color:{c.text}; }}")
        self.pause_button = QPushButton("Pause", self)
        self.pause_button.setStyleSheet(button_style)
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.setToolTip("Leave this mission. Browsing carries on as normal.")
        self.pause_button.clicked.connect(self._pause)
        self.complete_button = QPushButton("Mark complete", self)
        self.complete_button.setStyleSheet(button_style)
        self.complete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.complete_button.setToolTip("Finish this mission. It stays in your list.")
        self.complete_button.clicked.connect(self._complete)
        actions.addWidget(self.pause_button)
        actions.addWidget(self.complete_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        self.hide()

    # -- rendering -------------------------------------------------------
    def show_mission(self, mission: Mission | None) -> None:
        self._mission = mission
        if mission is None:
            self.hide()
            return
        c = self._colours
        m = theme.METRICS
        self.title.setText(_elide(mission.title, 28))
        self.title.setToolTip(f"{mission.title}\nClick to rename this mission")
        self.goal.setText(mission.goal)

        tone = {MissionStatus.ACTIVE: c.accent,
                MissionStatus.PAUSED: c.muted,
                MissionStatus.COMPLETED: c.success}.get(mission.status, c.muted)
        self.status.setText(mission.status_label)
        self.status.setStyleSheet(
            f"color:{tone}; font-size:{m.text_xs}px; font-weight:700;"
            " letter-spacing:0.08em;")

        self._render_findings(mission)
        self._render_pages(mission)
        self.show()

    @staticmethod
    def _clear(box) -> None:
        while box.count():
            item = box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _render_findings(self, mission: Mission) -> None:
        self._clear(self._findings_box)
        findings = list(mission.findings)
        if not findings:
            self.findings_label.setText("FINDINGS")
            empty = QLabel("What Py works out for this mission will be "
                           "collected here.", self)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color:{self._colours.disabled}; font-size:{theme.METRICS.text_sm}px;")
            self._findings_box.addWidget(empty)
            self.more_findings.hide()
            return

        self.findings_label.setText(f"FINDINGS \u00b7 {len(findings)}")
        for finding in findings[:VISIBLE_FINDINGS]:
            row = _FindingRow(finding, self)
            row.edit_requested.connect(self._edit_finding)
            row.source_requested.connect(self._open_source)
            self._findings_box.addWidget(row)

        hidden = len(findings) - VISIBLE_FINDINGS
        if hidden > 0:
            self.more_findings.setText(f"and {hidden} more")
            self.more_findings.show()
        else:
            self.more_findings.hide()

    def _render_pages(self, mission: Mission) -> None:
        self._clear(self._pages_box)
        pages = list(mission.pages)
        if not pages:
            self.pages_label.setText("PAGES")
            empty = QLabel("Pages Py opens or reads for this mission "
                           "will collect here.", self)
            empty.setWordWrap(True)
            empty.setStyleSheet(
                f"color:{self._colours.disabled}; font-size:{theme.METRICS.text_sm}px;")
            self._pages_box.addWidget(empty)
            self.more.hide()
            return

        self.pages_label.setText(f"PAGES · {len(pages)}")
        live = self._service.open_keys()
        for page in pages[:VISIBLE_PAGES]:
            row = _PageRow(page, page.key in live, self)
            row.clicked.connect(lambda _checked=False, p=page: self._service.show(p))
            self._pages_box.addWidget(row)

        hidden = len(pages) - VISIBLE_PAGES
        if hidden > 0:
            self.more.setText(f"and {hidden} more")
            self.more.show()
        else:
            self.more.hide()

    # -- actions ---------------------------------------------------------
    def _rename(self) -> None:
        """Let the user fix the title.

        title_from_goal() is a local heuristic, so a wrong title is a normal
        outcome rather than a bug - and it is the name the user will look for
        later. Renaming has to be one click away.
        """
        mission = self._mission
        if mission is None:
            return
        title, ok = QInputDialog.getText(self, "Rename mission", "Mission name:",
                                         text=mission.title)
        if ok and title.strip():
            self._service.rename(mission.id, title)

    def _edit_finding(self, finding: MissionFinding) -> None:
        """Reword or remove one finding.

        A dialog rather than inline editing: at 300px an inline editor has to
        solve focus, commit and escape in a row that is already two lines, and
        the payoff is one saved click on an action nobody performs often.
        """
        dialog = _FindingDialog(finding, self)
        outcome = dialog.exec()
        if outcome == _FindingDialog.DELETE:
            self._service.delete_finding(finding.id)
            return
        if outcome != QDialog.DialogCode.Accepted:
            return
        text = dialog.text().strip()
        if not text or text == finding.text:
            return
        result = self._service.edit_finding(finding.id, text)
        if result == "duplicate":
            QMessageBox.information(
                self, "Already recorded",
                "This mission already has a finding that says the same thing, "
                "so this one was left as it was.")
        elif result == "too_long":
            QMessageBox.information(
                self, "Too long",
                f"A finding can be up to {MAX_FINDING_CHARS} characters.")

    def _open_source(self, finding: MissionFinding) -> None:
        page = self._service.source_page(finding)
        if page is not None:
            self._service.show(page)

    def _pause(self) -> None:
        self._service.pause()
        self.changed.emit()

    def _complete(self) -> None:
        self._service.complete()
        self.changed.emit()


class _FindingDialog(QDialog):
    """Edit one finding, or delete it. Delete lives here rather than as a
    second control in the row - see the note on _FindingRow."""

    DELETE = 2      # a third outcome alongside Accepted and Rejected

    def __init__(self, finding: MissionFinding, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Finding")
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setSpacing(m.space_3)

        self._edit = QPlainTextEdit(finding.text, self)
        self._edit.setMinimumWidth(360)
        self._edit.setFixedHeight(90)
        layout.addWidget(self._edit)

        if finding.source_url:
            source = QLabel(f"From {finding.source_domain}", self)
            source.setToolTip(finding.source_url)
            source.setStyleSheet(f"color:{theme.palette_for(QApplication.instance()).muted};"
                                 f" font-size:{m.text_xs}px;")
            layout.addWidget(source)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self)
        remove = buttons.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
        remove.setProperty("kind", "danger")
        remove.clicked.connect(lambda: self.done(self.DELETE))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self._edit.toPlainText()


def _elide(text: str, limit: int) -> str:
    """Shorten for display. Page titles come from web pages: never trust the
    length, and never let one push the panel's layout around."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

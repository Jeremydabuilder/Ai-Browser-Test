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
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.missions.model import Mission, MissionPage, MissionStatus
from app.ui import theme

#: Pages listed before the rest are summarised. A Mission panel is a reminder,
#: not a file manager.
VISIBLE_PAGES = 6


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

        self._render_pages(mission)
        self.show()

    def _render_pages(self, mission: Mission) -> None:
        while self._pages_box.count():
            item = self._pages_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

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

    def _pause(self) -> None:
        self._service.pause()
        self.changed.emit()

    def _complete(self) -> None:
        self._service.complete()
        self.changed.emit()


def _elide(text: str, limit: int) -> str:
    """Shorten for display. Page titles come from web pages: never trust the
    length, and never let one push the panel's layout around."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

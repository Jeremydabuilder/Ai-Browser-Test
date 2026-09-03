"""Starting a Mission, and picking up an old one.

This is what Py's panel leads with when nothing is active, and it is
deliberately the largest thing on the screen at that moment. A Mission is the
idea the product is built around - "stop managing tabs, start completing
missions" - so the invitation to start one is the invitation to use PyBrowser,
not a secondary control tucked into a corner.

It still has to stay calm: one question, one button, and the missions you were
already working on. No onboarding, no illustrations, no empty-state art.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.missions.model import Mission, MissionStatus
from app.ui import theme

#: Past missions offered without asking. Enough to recognise last week's work,
#: not so many that the panel becomes a list view.
RECENT_LIMIT = 5


class _RecentRow(QPushButton):
    """One earlier Mission, ready to resume."""

    def __init__(self, mission: Mission, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mission = mission
        c = parent._colours
        m = theme.METRICS
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet(
            "QPushButton { background:transparent; border:none; text-align:left;"
            f" border-radius:{m.radius_sm}px; padding:0 {m.space_1}px; }}"
            f"QPushButton:hover {{ background:{c.surface_alt}; }}")
        self.setMinimumHeight(26)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(m.space_2)

        title = QLabel(_elide(mission.title, 26), self)
        title.setStyleSheet(f"color:{c.text}; font-size:{m.text_sm}px;")
        row.addWidget(title)
        row.addStretch(1)

        if mission.status != MissionStatus.ACTIVE:
            tone = c.success if mission.is_complete else c.muted
            state = QLabel(mission.status_label.title(), self)
            state.setStyleSheet(f"color:{tone}; font-size:{m.text_xs}px;")
            row.addWidget(state)

        self.setToolTip(f"{mission.title}\n{mission.goal}\n\nClick to resume")


class MissionPicker(QWidget):
    """The "what are you trying to accomplish?" state."""

    #: A Mission was started or resumed.
    started = Signal(object)      # Mission

    def __init__(self, service, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service
        # The application palette, not None: palette_for(None) silently
        # falls back to the light theme, which paints dark text on a dark
        # panel for every user on a dark desktop.
        self._colours = theme.palette_for(QApplication.instance())
        c = self._colours
        m = theme.METRICS

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(m.space_2)

        prompt = QLabel("What are you trying to accomplish?", self)
        prompt.setWordWrap(True)
        prompt.setStyleSheet(
            f"color:{c.text}; font-size:{m.text_lg}px; font-weight:600;")
        outer.addWidget(prompt)

        self.goal = QLineEdit(self)
        self.goal.setPlaceholderText("Find the best tennis shoes under $140…")
        self.goal.setAccessibleName("Mission goal")
        self.goal.setMinimumHeight(m.control)
        self.goal.returnPressed.connect(self._start)
        outer.addWidget(self.goal)

        self.start_button = QPushButton("Start a Mission", self)
        self.start_button.setProperty("kind", "primary")
        self.start_button.setMinimumHeight(m.control)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setToolTip(
            "Py keeps the pages for this goal together, and you can come back "
            "to it later.")
        self.start_button.clicked.connect(self._start)
        outer.addWidget(self.start_button)

        self.explainer = QLabel(
            "Py keeps the pages for one goal together, so you can leave and "
            "pick it up later.", self)
        self.explainer.setWordWrap(True)
        self.explainer.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        outer.addWidget(self.explainer)

        self.rule = QFrame(self)
        self.rule.setFrameShape(QFrame.Shape.HLine)
        self.rule.setFixedHeight(1)
        self.rule.setStyleSheet(f"background:{c.line}; border:none;")
        outer.addWidget(self.rule)

        self.recent_label = QLabel("RECENT MISSIONS", self)
        self.recent_label.setStyleSheet(
            f"color:{c.disabled}; font-size:{m.text_xs}px; font-weight:600;"
            " letter-spacing:0.06em;")
        outer.addWidget(self.recent_label)

        self._recent_box = QVBoxLayout()
        self._recent_box.setSpacing(0)
        outer.addLayout(self._recent_box)

        # The panel shows the last few; the library shows all of them, with
        # search, in a page with room to read.
        self.all_button = QPushButton("All missions", self)
        self.all_button.setProperty("kind", "quiet")
        self.all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_button.setStyleSheet(
            f"QPushButton {{ background:transparent; border:none; color:{c.accent};"
            f" font-size:{m.text_sm}px; text-align:left; padding:{m.space_1}px 0; }}")
        self.all_button.clicked.connect(self._open_library)
        outer.addWidget(self.all_button)

        self.refresh()

    # -- rendering -------------------------------------------------------
    def refresh(self) -> None:
        """Re-read the Mission list. Cheap, and always right."""
        while self._recent_box.count():
            item = self._recent_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        missions = self._service.recent(RECENT_LIMIT)
        shown = bool(missions)
        self.rule.setVisible(shown)
        self.recent_label.setVisible(shown)
        self.all_button.setVisible(shown)
        for mission in missions:
            row = _RecentRow(mission, self)
            row.clicked.connect(lambda _checked=False, m=mission: self._resume(m.id))
            self._recent_box.addWidget(row)

    def offered(self) -> list[Mission]:
        """The missions currently on offer, in the order they are shown."""
        return [self._recent_box.itemAt(i).widget().mission
                for i in range(self._recent_box.count())
                if self._recent_box.itemAt(i).widget() is not None]

    # -- actions ---------------------------------------------------------
    def _start(self) -> None:
        goal = self.goal.text().strip()
        if not goal:
            self.goal.setFocus()
            return
        mission = self._service.start(goal)
        if mission is None:
            return
        self.goal.clear()
        self.started.emit(mission)

    def _open_library(self) -> None:
        """Show the Mission Library. Found by walking up to the window rather
        than by holding a reference, so this widget stays a view."""
        window = self.window()
        opener = getattr(window, "_show_mission_library", None)
        if callable(opener):
            opener()

    def _resume(self, mission_id: int) -> None:
        mission = self._service.resume(mission_id)
        if mission is not None:
            self.started.emit(mission)


def _elide(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

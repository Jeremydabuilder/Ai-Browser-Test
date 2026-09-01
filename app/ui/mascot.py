"""Py AI's visual presence: one small character with a handful of states.

What this is for
----------------
An agent that is working should look like it is working. A step list says what
happened; a face says *how it is going* at a glance, from across the room,
without reading. That is the whole job - it is a status indicator with a
personality, not a character system.

Replacing the artwork
---------------------
Drop SVG (or PNG) files into `app/ui/assets/mascot/` named after the states in
`MascotState`:

    idle.svg  reading.svg  thinking.svg  working.svg  complete.svg  approval.svg

Any state without a file falls back to `idle`, and if there is no artwork at
all the built-in placeholder below is drawn instead - so the UI works before
the character exists and needs no code change when it arrives. A file named
`<state>@2x.png` is preferred on a high-DPI screen if present.

Deliberately not built yet: animation frames, expressions, speech, sound. The
state enum is the contract; everything else can come later without the panel
or the new-tab page knowing about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "mascot")


class MascotState:
    """What Py AI is doing, in the only terms the mascot needs to know.

    These map onto AgentState, but they are not the same list: the mascot has
    no opinion about the difference between "cancelling" and "idle", and it
    does have an opinion about the difference between "acting" and "reading".
    """

    IDLE = "idle"
    READING = "reading"
    THINKING = "thinking"
    WORKING = "working"
    COMPLETE = "complete"
    APPROVAL = "approval"


ALL_STATES = (MascotState.IDLE, MascotState.READING, MascotState.THINKING,
              MascotState.WORKING, MascotState.COMPLETE, MascotState.APPROVAL)

#: How long `COMPLETE` shows before falling back to `IDLE`. Long enough to
#: notice, short enough not to look stuck.
COMPLETE_MS = 2600


@dataclass(frozen=True)
class _Look:
    """The built-in placeholder's appearance for one state.

    Drawn rather than shipped as files so the repository carries no invented
    artwork for a character that does not exist yet: this is a stand-in, and it
    is meant to look like one.
    """

    #: Which palette colour the character takes.
    tone: str
    #: Eye shape: open, half (reading), or closed (thinking).
    eyes: str = "open"
    #: A small mark beside the face, or "".
    badge: str = ""


_LOOKS: dict[str, _Look] = {
    MascotState.IDLE: _Look("accent"),
    MascotState.READING: _Look("accent", eyes="half"),
    MascotState.THINKING: _Look("accent", eyes="closed"),
    MascotState.WORKING: _Look("accent", eyes="open", badge="spin"),
    MascotState.COMPLETE: _Look("success", eyes="happy", badge="tick"),
    MascotState.APPROVAL: _Look("warning", eyes="open", badge="ask"),
}


def asset_for(state: str) -> str | None:
    """The artwork file for a state, or None if there is none.

    Falls back to `idle` so a partial set of artwork still works: shipping one
    good idle drawing should be enough to replace the placeholder everywhere.
    """
    for candidate in (state, MascotState.IDLE):
        for extension in (".svg", ".png"):
            path = os.path.join(ASSET_DIR, candidate + extension)
            if os.path.isfile(path):
                return path
    return None


def has_artwork() -> bool:
    return asset_for(MascotState.IDLE) is not None


class Mascot(QLabel):
    """The character itself: a fixed-size image that changes with the state.

    A QLabel rather than a custom widget, so it can be dropped into any layout
    and costs nothing when the panel is closed.
    """

    clicked = Signal()

    def __init__(self, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.ui import theme

        self._colours = theme.palette_for(None)
        self._size = size
        self._state = MascotState.IDLE
        self._phase = 0
        self.setFixedSize(QSize(size, size))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAccessibleName("Py AI")
        self.setToolTip(_TOOLTIPS[MascotState.IDLE])

        # One timer, only running when the state actually animates. An idle
        # mascot ticking in the background is a battery cost for a still image.
        self._timer = QTimer(self)
        self._timer.setInterval(140)
        self._timer.timeout.connect(self._tick)
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(lambda: self.set_state(MascotState.IDLE))
        self._render()

    # -- state ------------------------------------------------------------
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state not in ALL_STATES or state == self._state:
            return
        self._state = state
        self._phase = 0
        if state in (MascotState.WORKING, MascotState.THINKING):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        # "Complete" is a moment, not a resting state.
        self._revert.stop()
        if state == MascotState.COMPLETE:
            self._revert.start(COMPLETE_MS)
        self.setToolTip(_TOOLTIPS.get(state, "Py AI"))
        self.setAccessibleDescription(_TOOLTIPS.get(state, ""))
        self._render()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 8
        self._render()

    # -- drawing ----------------------------------------------------------
    def _render(self) -> None:
        artwork = asset_for(self._state)
        self.setPixmap(self._from_file(artwork) if artwork else self._placeholder())

    def _from_file(self, path: str) -> QPixmap:
        scale = self.devicePixelRatioF() or 1.0
        retina = path.rsplit(".", 1)[0] + "@2x." + path.rsplit(".", 1)[1]
        if scale > 1.5 and os.path.isfile(retina):
            path = retina
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return self._placeholder()
        return pixmap.scaled(
            self._size, self._size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)

    def _placeholder(self) -> QPixmap:
        """The stand-in character: a rounded square with a face.

        Simple on purpose. It should read as "something goes here", not
        compete with the artwork that will replace it.
        """
        look = _LOOKS.get(self._state, _LOOKS[MascotState.IDLE])
        c = self._colours
        tone = getattr(c, look.tone, c.accent)
        size = self._size
        # A gentle bob while working, so the character is alive without moving
        # enough to pull the eye off the page.
        bob = (0, -1, -1, 0, 1, 1, 0, 0)[self._phase] if self._timer.isActive() else 0

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        svg = _placeholder_svg(look, tone, c.surface, self._phase,
                               spinning=self._timer.isActive())
        from PySide6.QtSvg import QSvgRenderer

        QSvgRenderer(QByteArray(svg.encode())).render(
            painter, QRectF(0, bob, size, size))
        painter.end()
        return pixmap


_TOOLTIPS = {
    MascotState.IDLE: "Py AI is ready",
    MascotState.READING: "Py AI is reading the page",
    MascotState.THINKING: "Py AI is thinking",
    MascotState.WORKING: "Py AI is working in the browser",
    MascotState.COMPLETE: "Py AI has finished",
    MascotState.APPROVAL: "Py AI needs your approval",
}


def _placeholder_svg(look: _Look, tone: str, face: str, phase: int,
                     *, spinning: bool) -> str:
    eyes = {
        "open": (f'<circle cx="12" cy="19" r="2.1" fill="{tone}"/>'
                 f'<circle cx="24" cy="19" r="2.1" fill="{tone}"/>'),
        "half": (f'<path d="M10 19h4M22 19h4" stroke="{tone}" stroke-width="2.4"'
                 ' stroke-linecap="round"/>'),
        "closed": (f'<path d="M10 19.5q2-2.4 4 0M22 19.5q2-2.4 4 0" stroke="{tone}"'
                   ' stroke-width="2.2" stroke-linecap="round" fill="none"/>'),
        "happy": (f'<path d="M10 18.5q2 2.4 4 0M22 18.5q2 2.4 4 0" stroke="{tone}"'
                  ' stroke-width="2.2" stroke-linecap="round" fill="none"/>'),
    }[look.eyes]

    badge = ""
    if look.badge == "tick":
        badge = (f'<circle cx="28.5" cy="8.5" r="6" fill="{tone}"/>'
                 f'<path d="m25.8 8.6 1.9 1.9 3.4-3.6" stroke="{face}"'
                 ' stroke-width="1.9" fill="none" stroke-linecap="round"'
                 ' stroke-linejoin="round"/>')
    elif look.badge == "ask":
        badge = (f'<circle cx="28.5" cy="8.5" r="6" fill="{tone}"/>'
                 f'<path d="M26.9 6.9a1.7 1.7 0 1 1 1.7 2.2v.9" stroke="{face}"'
                 ' stroke-width="1.7" fill="none" stroke-linecap="round"/>'
                 f'<circle cx="28.6" cy="11.6" r=".95" fill="{face}"/>')
    elif look.badge == "spin" and spinning:
        angle = phase * 45
        badge = (f'<g transform="rotate({angle} 28.5 8.5)">'
                 f'<path d="M28.5 3.5a5 5 0 1 0 5 5" stroke="{tone}"'
                 ' stroke-width="2" fill="none" stroke-linecap="round"/></g>')

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36" fill="none">'
        f'<rect x="3" y="7" width="30" height="26" rx="9" fill="{tone}" fill-opacity=".14"/>'
        f'<rect x="3" y="7" width="30" height="26" rx="9" stroke="{tone}"'
        ' stroke-opacity=".45" stroke-width="1.4"/>'
        f'<path d="M18 3v4" stroke="{tone}" stroke-width="1.8" stroke-linecap="round"/>'
        f'<circle cx="18" cy="2.6" r="1.7" fill="{tone}"/>'
        f"{eyes}"
        f'<path d="M14.5 26q3.5 2.4 7 0" stroke="{tone}" stroke-width="1.8"'
        ' stroke-linecap="round" fill="none" opacity=".75"/>'
        f"{badge}</svg>"
    )


def state_for_agent(agent_state: str, *, finished_well: bool = False) -> str:
    """Translate an AgentState into what the mascot should show.

    Kept here rather than in the panel so both the panel and the new-tab page
    tell the same story about the same agent.
    """
    from app.agent.session import AgentState

    return {
        AgentState.THINKING: MascotState.THINKING,
        AgentState.ACTING: MascotState.WORKING,
        AgentState.AWAITING_CONFIRMATION: MascotState.APPROVAL,
        AgentState.CANCELLING: MascotState.IDLE,
        AgentState.IDLE: MascotState.COMPLETE if finished_well else MascotState.IDLE,
    }.get(agent_state, MascotState.IDLE)

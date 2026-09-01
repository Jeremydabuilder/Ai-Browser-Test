"""Py: PyBrowser's companion, and the browser's honest status indicator.

Py is not decoration. What Py is doing IS what the agent is doing - reading,
thinking, working, waiting for you - so a glance at the character answers "how
is this going" without reading a word. That is the whole design: a status
indicator that happens to have a face.

Where the artwork comes from
----------------------------
Three places, in order, per state:

1. ``app/ui/assets/mascot/<state>.<ext>``  - the real Py artwork
2. ``app/ui/assets/mascot/placeholder/<state>.<ext>`` - the stand-in shipped
   with the source, so the browser looks right before the artwork lands
3. a shape drawn in code - the last resort if both folders are empty

Extensions are tried in the order of ``FORMATS``. A missing state falls back to
``idle`` at the same level before dropping to the next level, so one good idle
drawing is enough to replace the placeholder everywhere. ``<state>@2x.png`` is
preferred on a high-DPI screen.

**Dropping the final artwork into ``assets/mascot/`` is the whole integration.**
No code changes, no registration, no rebuild.

Animation
---------
Two kinds, and the widget does not care which it has:

* **Animated artwork** - a GIF or APNG plays through ``QMovie``. Whatever the
  artist put in the file is what you see.
* **Still artwork** - the widget adds its own restrained motion: a slow breath,
  an occasional blink, a small lean for thinking. Enough that Py looks alive,
  little enough that it is never the thing you are looking at.

Either way, reduced-motion settings stop all of it and the still frame shows.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QMovie, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QLabel, QWidget

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(_HERE, "assets", "mascot")
PLACEHOLDER_DIR = os.path.join(ASSET_DIR, "placeholder")

#: Tried in order. Animated formats come first so an animated Py wins over a
#: still one of the same name.
FORMATS = (".gif", ".webp", ".apng", ".png", ".svg")
ANIMATED_FORMATS = (".gif", ".webp", ".apng")


class Variant:
    """How much of Py is shown.

    The same character in two crops, because the two places Py appears want
    different things. The new-tab page is where you meet Py and there is room
    for a pose; the agent panel is a 40px slot beside a conversation, where a
    full-body figure would be an unreadable smudge.
    """

    #: Head and shoulders. The agent panel.
    PANEL = "panel"
    #: The whole character. The new-tab page.
    FULL = "full"


VARIANTS = (Variant.PANEL, Variant.FULL)


class MascotState:
    """What Py is doing, in the only terms the character needs to know.

    These map onto AgentState but are not the same list: Py has no opinion
    about the difference between "cancelling" and "idle", and does have one
    about the difference between reading a page and clicking through it.
    """

    IDLE = "idle"
    READING = "reading"
    THINKING = "thinking"
    WORKING = "working"
    APPROVAL = "approval"
    COMPLETE = "complete"
    #: A task that ended without an answer. Deliberately its own state: showing
    #: the celebrating face after a failure would be a small lie told often.
    STUCK = "stuck"


ALL_STATES = (MascotState.IDLE, MascotState.READING, MascotState.THINKING,
              MascotState.WORKING, MascotState.APPROVAL, MascotState.COMPLETE,
              MascotState.STUCK)

#: How long a reaction shows before Py settles back to idle. Long enough to
#: notice, short enough that it never looks stuck on.
REACTION_MS = 2600

#: What Py says. Presentation strings, chosen from the state alone - never the
#: model's words, never anything derived from a page, and never reasoning.
COMPANION_TEXT: dict[str, str] = {
    MascotState.IDLE: "Ready when you are.",
    MascotState.READING: "I'm looking through the page\u2026",
    MascotState.THINKING: "Let me figure this out\u2026",
    MascotState.WORKING: "On it.",
    MascotState.APPROVAL: "I need your okay for this.",
    MascotState.COMPLETE: "Done!",
    MascotState.STUCK: "Looks like I got stuck.",
}

#: The same thing, for a screen reader and a tooltip.
TOOLTIPS: dict[str, str] = {
    MascotState.IDLE: "Py is ready",
    MascotState.READING: "Py is reading the page",
    MascotState.THINKING: "Py is thinking",
    MascotState.WORKING: "Py is working in the browser",
    MascotState.APPROVAL: "Py needs your approval",
    MascotState.COMPLETE: "Py has finished",
    MascotState.STUCK: "Py could not finish this one",
}


def reduced_motion() -> bool:
    """Has the user asked for less animation?

    Qt has no cross-platform query for this, so we read the variables the
    freedesktop and Qt tooling use, plus our own. Someone who asked once
    should not have to ask again.
    """
    for name in ("PYBROWSER_REDUCED_MOTION", "QT_REDUCED_MOTION", "NO_ANIMATIONS"):
        if (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


# ---------------------------------------------------------------------------
# Finding the artwork
# ---------------------------------------------------------------------------


def asset_for(state: str, variant: str = Variant.PANEL, *,
              high_dpi: bool = False) -> str | None:
    """The best artwork file for a state and crop, or None if there is none.

    The search runs widest-first through four names, then repeats the whole
    thing in the placeholder folder:

    1. ``<state>-<variant>``  - this state, this crop
    2. ``<state>``            - this state, uncropped (the original naming,
                                still supported so existing sets keep working)
    3. ``idle-<variant>``     - the fallback state, this crop
    4. ``idle``               - the fallback state, uncropped

    Real artwork beats the placeholder at every one of those, which is what
    makes dropping in the final Py feel immediate: one ``idle-full.png`` and
    the new-tab page is the new character, placeholders and all.
    """
    for directory in (ASSET_DIR, PLACEHOLDER_DIR):
        for name in _candidates(state, variant):
            found = _in_directory(directory, name, high_dpi)
            if found:
                return found
    return None


def _candidates(state: str, variant: str) -> tuple[str, ...]:
    """Every filename worth trying for a state and crop, best first."""
    if variant not in VARIANTS:
        variant = Variant.PANEL
    names = [f"{state}-{variant}", state]
    if state != MascotState.IDLE:
        names += [f"{MascotState.IDLE}-{variant}", MascotState.IDLE]
    return tuple(names)


def _in_directory(directory: str, name: str, high_dpi: bool) -> str | None:
    """The first existing file for one exact name, preferring @2x on retina."""
    for extension in FORMATS:
        if high_dpi and extension != ".svg":
            retina = os.path.join(directory, f"{name}@2x{extension}")
            if os.path.isfile(retina):
                return retina
        path = os.path.join(directory, name + extension)
        if os.path.isfile(path):
            return path
    return None


def is_animated(path: str) -> bool:
    return path.lower().endswith(ANIMATED_FORMATS)


def has_final_artwork() -> bool:
    """True once real artwork has been dropped in - placeholders do not count.

    Used for more than a status line: the synthetic animation below is toned
    down once there is a real illustration to animate, because a squash that
    reads as a blink on a flat placeholder reads as a distortion on a drawing.
    """
    for name in (f"{MascotState.IDLE}-{Variant.PANEL}",
                 f"{MascotState.IDLE}-{Variant.FULL}", MascotState.IDLE):
        if _in_directory(ASSET_DIR, name, False) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Motion:
    """How a still Py moves in one state. Amplitudes are in pixels or degrees.

    Split by what the transform does to the drawing, not by what it looks like:

    * ``bob``, ``lean`` and ``pulse`` are **rigid** - a translation, a rotation
      and a *uniform* scale. None of them can change Py's proportions or
      distort his face, so they are safe on finished artwork.
    * ``blinks`` is a **squash**, a non-uniform scale. It reads as a blink on a
      flat placeholder and as a rendering fault on an illustration, so it is
      only ever applied to the placeholder.

    ``entry_*`` is a one-shot on arriving in a state rather than a loop - a
    reaction is a moment, and a celebration that never stops is wallpaper.
    """

    bob: float = 0.0        # vertical drift, the "breath"
    period_ms: int = 0      # how long one breath takes
    lean: float = 0.0       # degrees of rotation - a weight shift
    blinks: bool = False
    pulse: float = 0.0      # uniform scale change, for attention
    entry_rise: float = 0.0   # px, once, on entering the state
    entry_pop: float = 0.0    # uniform scale, once, on entering the state
    entry_ms: int = 0


#: Chosen to be barely perceptible. If you can see Py moving without looking
#: for it, the numbers are too big.
_MOTION: dict[str, _Motion] = {
    # Breathing, and nothing else to look at. The whisper of pulse is not
    # decoration: a sub-pixel translation on its own gets quantised to whole
    # pixels, so the breath snapped between three positions instead of
    # flowing. A uniform scale forces the frame to be resampled, which is what
    # makes it continuous. Measured, not assumed - 3 distinct frames out of 70
    # before, 60-odd after.
    MascotState.IDLE: _Motion(bob=0.9, period_ms=4200, blinks=True, pulse=0.005),
    # Slower and shallower, with the faintest sway - absorbed in the page.
    MascotState.READING: _Motion(bob=0.55, period_ms=3600, lean=0.35, blinks=True),
    # A held pose. The lean is the thought.
    MascotState.THINKING: _Motion(bob=0.6, period_ms=2600, lean=1.4),
    # The one state that has to read as effort at a glance: a quicker cadence
    # than a resting breath, with a small shift of weight over it, so a look
    # across the room says "he is doing something" rather than "he is idle".
    MascotState.WORKING: _Motion(bob=1.1, period_ms=1500, lean=0.55, pulse=0.008),
    # Waiting on you, and saying so without nagging.
    MascotState.APPROVAL: _Motion(bob=0.5, period_ms=2000, pulse=0.022),
    # A pop on arrival that settles within a second into an ordinary happy
    # breath. Looping a celebration forever turns delight into wallpaper.
    MascotState.COMPLETE: _Motion(bob=1.0, period_ms=2600, blinks=True, pulse=0.006,
                                  entry_rise=5.0, entry_pop=0.06, entry_ms=760),
    # Barely moving, with a small tilt. Stuck should look becalmed, not busy.
    MascotState.STUCK: _Motion(bob=0.4, period_ms=4600, lean=0.8, blinks=True),
}

_FRAME_MS = 50          # 20fps: smooth enough for motion this small


class Mascot(QLabel):
    """Py, at one size, in one state.

    A QLabel so it drops into any layout and costs nothing when hidden. It owns
    its own timers and stops them the moment there is nothing to animate.
    """

    clicked = Signal()
    #: The state changed; carries the state name. The panel uses this to keep
    #: its companion line in step without having to ask.
    state_changed = Signal(str)

    def __init__(self, size: int = 40, parent: QWidget | None = None,
                 *, variant: str = Variant.PANEL, height: int | None = None) -> None:
        super().__init__(parent)
        from app.ui import theme

        self._colours = theme.palette_for(None)
        self._size = size
        self._height = height or size
        self._variant = variant if variant in VARIANTS else Variant.PANEL
        self._state = MascotState.IDLE
        self._elapsed = 0
        self._blink_at = self._next_blink()
        self._blinking = 0
        self._movie: QMovie | None = None
        self._still: QPixmap | None = None

        self.setFixedSize(QSize(self._size, self._height))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(False)
        self.setAccessibleName("Py")
        self.setToolTip(TOOLTIPS[MascotState.IDLE])
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._frames = QTimer(self)
        self._frames.setInterval(_FRAME_MS)
        self._frames.timeout.connect(self._advance)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(lambda: self.set_state(MascotState.IDLE))

        self._load()
        self._render()
        self._sync_motion()

    # -- state -------------------------------------------------------------
    def state(self) -> str:
        return self._state

    def companion_text(self) -> str:
        return COMPANION_TEXT.get(self._state, "")

    def set_state(self, state: str) -> None:
        """Show a different state. Unknown states are ignored, not drawn."""
        if state not in ALL_STATES or state == self._state:
            return
        self._state = state
        self._elapsed = 0
        self._blink_at = self._next_blink()
        self.setToolTip(TOOLTIPS.get(state, "Py"))
        self.setAccessibleDescription(COMPANION_TEXT.get(state, ""))

        # A reaction is a moment, not a resting state.
        self._settle.stop()
        if state in (MascotState.COMPLETE, MascotState.STUCK):
            self._settle.start(REACTION_MS)

        self._load()
        self._render()
        self._sync_motion()
        self.state_changed.emit(state)

    def variant(self) -> str:
        return self._variant

    def set_size(self, size: int, height: int | None = None) -> None:
        """Resize, keeping the state. Used when the window changes shape.

        ``height`` is separate because a full-body Py is taller than it is
        wide, and forcing that into a square either shrinks it to nothing or
        crops its feet off.
        """
        height = height or size
        if size <= 0 or height <= 0 or (size, height) == (self._size, self._height):
            return
        self._size, self._height = size, height
        self.setFixedSize(QSize(size, height))
        self._load()
        self._render()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)

    # -- artwork -----------------------------------------------------------
    def _load(self) -> None:
        """Pick up the artwork for the current state.

        Called on every state change rather than cached, so dropping a file
        into the assets folder while the browser is running takes effect the
        next time Py changes state - handy while an artist is iterating.
        """
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        self._still = None

        high_dpi = (self.devicePixelRatioF() or 1.0) > 1.5
        path = asset_for(self._state, self._variant, high_dpi=high_dpi)
        if path is None:
            self._still = self._drawn()
            return
        if is_animated(path) and not reduced_motion():
            movie = QMovie(path)
            if movie.isValid():
                movie.setScaledSize(QSize(self._size, self._height))
                self._movie = movie
                self.setMovie(movie)
                movie.start()
                return
        self._still = self._from_file(path)

    def _from_file(self, path: str) -> QPixmap:
        if path.lower().endswith(".svg"):
            return self._from_svg(path)
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return self._drawn()
        return pixmap.scaled(self._size, self._height,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)

    def _from_svg(self, path: str) -> QPixmap:
        """Rasterise at the device pixel ratio, so Py is never soft."""
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(path)
        if not renderer.isValid():
            return self._drawn()
        scale = self.devicePixelRatioF() or 1.0
        # Fit the drawing's own aspect into the box rather than stretching it
        # to fill: a full-body Py squeezed into a square is a different
        # character from the one the artist drew.
        art = renderer.defaultSize()
        box_w, box_h = self._size, self._height
        if art.width() > 0 and art.height() > 0:
            ratio = min(box_w / art.width(), box_h / art.height())
            draw_w, draw_h = art.width() * ratio, art.height() * ratio
        else:
            draw_w, draw_h = box_w, box_h
        pixmap = QPixmap(max(1, int(box_w * scale)), max(1, int(box_h * scale)))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(
            (box_w - draw_w) / 2 * scale, (box_h - draw_h) / 2 * scale,
            draw_w * scale, draw_h * scale))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    # -- motion ------------------------------------------------------------
    def _sync_motion(self) -> None:
        """Run the frame timer only when there is something to animate."""
        if self._movie is not None or reduced_motion():
            self._frames.stop()
            return
        motion = _MOTION.get(self._state, _Motion())
        if motion.period_ms or motion.blinks:
            if not self._frames.isActive():
                self._frames.start()
        else:
            self._frames.stop()

    def _advance(self) -> None:
        self._elapsed += _FRAME_MS
        motion = _MOTION.get(self._state, _Motion())
        if motion.blinks:
            if self._blinking:
                self._blinking -= 1
                if self._blinking == 0:
                    self._blink_at = self._elapsed + self._next_blink()
            elif self._elapsed >= self._blink_at:
                self._blinking = 2      # two frames: 100ms, the length of a blink
        self._render()

    @staticmethod
    def _next_blink() -> int:
        """When to blink next.

        Randomised because a blink on a fixed beat reads as a machine, and the
        one thing this character should not look like is a progress bar.
        """
        return random.randint(2600, 6200)

    # -- drawing -----------------------------------------------------------
    def _render(self) -> None:
        if self._movie is not None:
            return                       # QMovie paints itself
        base = self._still
        if base is None:
            return
        self.setPixmap(self._animated_frame(base))

    def _animated_frame(self, base: QPixmap) -> QPixmap:
        """Apply this frame's motion to a still Py.

        Two tiers, by what the transform does to the drawing:

        * **Rigid** - translation, rotation and *uniform* scale. None of these
          can change Py's proportions or distort his face; a rotated drawing is
          a drawing seen from a slightly different angle, not a broken one. Safe
          on any still artwork, placeholder or final.
        * **Squash** - the blink, a non-uniform scale. It reads as expression on
          a flat placeholder and as a rendering fault on an illustration, so it
          stops the moment real artwork is installed. Blinking finished artwork
          means eyes drawn shut, which is an animated asset, not a transform.

        An earlier pass grouped the rotation and the scale with the squash and
        switched all three off for real artwork. That was too cautious: those
        two are rigid, and turning them off is what left finished artwork
        completely inert.

        An animated asset never reaches here at all: QMovie plays it as drawn.
        """
        motion = _MOTION.get(self._state, _Motion())
        if reduced_motion() or not (motion.period_ms or motion.entry_ms
                                    or self._blinking):
            return base

        import math

        phase = ((self._elapsed % motion.period_ms) / motion.period_ms
                 if motion.period_ms else 0)
        wave = math.sin(phase * 2 * math.pi)
        arrival = self._entry_curve(motion)

        offset = motion.bob * wave - motion.entry_rise * arrival
        lean = motion.lean * wave
        scale = 1.0 + motion.pulse * (wave + 1) / 2 + motion.entry_pop * arrival

        canvas = QPixmap(base.size())
        canvas.setDevicePixelRatio(base.devicePixelRatio())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        width = base.width() / base.devicePixelRatio()
        height = base.height() / base.devicePixelRatio()
        painter.translate(width / 2, height / 2 + offset)
        if lean:
            painter.rotate(lean)
        if scale != 1.0:
            painter.scale(scale, scale)
        painter.translate(-width / 2, -height / 2)
        painter.drawPixmap(0, 0, base)

        painter.end()

        if self._blinking and not has_final_artwork():
            return self._blink_frame(canvas, offset)
        return canvas

    def _entry_curve_at(self, elapsed: int) -> float:
        """The arrival curve at an arbitrary moment, for tests and tuning."""
        was, self._elapsed = self._elapsed, elapsed
        try:
            return self._entry_curve(_MOTION.get(self._state, _Motion()))
        finally:
            self._elapsed = was

    def _entry_curve(self, motion: _Motion) -> float:
        """0 -> 1 -> 0 across the first `entry_ms` of a state, then nothing.

        Rises quickly, settles slowly, and is spent for good after one pass:
        set_state resets `_elapsed`, so this fires on arrival and never again
        while the state is held.
        """
        if not motion.entry_ms or self._elapsed >= motion.entry_ms:
            return 0.0
        moment = self._elapsed / motion.entry_ms
        crest = 0.28
        if moment < crest:
            return moment / crest
        return (1 - (moment - crest) / (1 - crest)) ** 2

    def _blink_frame(self, frame: QPixmap, offset: float) -> QPixmap:
        """Squash Py vertically for two frames: a blink, cheaply.

        Only ever applied to the placeholder. It reads as a blink on flat
        vector shapes and as a wobble on a real illustration, so real artwork
        keeps its own expression - see _animated_frame.
        """
        ratio = frame.devicePixelRatio()
        width = frame.width() / ratio
        height = frame.height() / ratio
        canvas = QPixmap(frame.size())
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        squash = 0.94
        painter.translate(0, height * (1 - squash) * 0.55)
        painter.scale(1.0, squash)
        painter.drawPixmap(0, 0, frame)
        painter.end()
        return canvas

    def _drawn(self) -> QPixmap:
        """Last resort: a simple mark, if there is no artwork at all.

        Deliberately plain. It exists so the UI never shows an empty hole, not
        to be a character - the placeholder folder is where the character
        lives until the real one arrives.
        """
        c = self._colours
        tone = {
            MascotState.COMPLETE: c.success,
            MascotState.APPROVAL: c.warning,
            MascotState.STUCK: c.muted,
        }.get(self._state, c.accent)
        scale = self.devicePixelRatioF() or 1.0
        side = min(self._size, self._height)
        pixels = max(1, int(side * scale))
        pixmap = QPixmap(max(1, int(self._size * scale)),
                         max(1, int(self._height * scale)))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        from PySide6.QtSvg import QSvgRenderer

        # A fox head, not a generic round face: this is what is drawn when
        # there is no artwork at all, and it should still be recognisably Py.
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36" fill="none">'
            f'<g fill="{tone}" fill-opacity=".16" stroke="{tone}" stroke-width="1.5"'
            ' stroke-linejoin="round">'
            '<path d="M11.5 11 7 3.5 14.5 7.5Z"/><path d="M24.5 11 29 3.5 21.5 7.5Z"/>'
            '<path d="M18 8.5c6.2 0 10.5 4.3 10.5 10.2 0 5.6-4.3 9.8-10.5 9.8'
            'S7.5 24.3 7.5 18.7C7.5 12.8 11.8 8.5 18 8.5Z"/></g>'
            f'<circle cx="13.6" cy="18" r="1.9" fill="{tone}"/>'
            f'<circle cx="22.4" cy="18" r="1.9" fill="{tone}"/>'
            f'<ellipse cx="18" cy="23" rx="2.1" ry="1.6" fill="{tone}"/>'
            f'<path d="M18 24.6v1.6M15 27.4q3 2 6 0" stroke="{tone}" stroke-width="1.5"'
            ' fill="none" stroke-linecap="round"/></svg>'
        )
        QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(
            (self._size * scale - pixels) / 2, (self._height * scale - pixels) / 2,
            pixels, pixels))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return pixmap


# ---------------------------------------------------------------------------


def state_for_agent(agent_state: str, *, answered: bool = False,
                    failed: bool = False) -> str:
    """Translate an AgentState into what Py should show.

    Lives here rather than in the panel so every surface tells the same story
    about the same agent.

    The end of a task is the part that has to be honest: `complete` is only for
    a task that actually produced an answer. One that was stopped, or that
    failed, gets `stuck` - a browser whose assistant celebrates its own
    failures is one you stop believing.
    """
    from app.agent.session import AgentState

    if agent_state == AgentState.IDLE:
        if failed:
            return MascotState.STUCK
        return MascotState.COMPLETE if answered else MascotState.IDLE
    return {
        AgentState.THINKING: MascotState.THINKING,
        AgentState.ACTING: MascotState.WORKING,
        AgentState.AWAITING_CONFIRMATION: MascotState.APPROVAL,
        AgentState.CANCELLING: MascotState.IDLE,
    }.get(agent_state, MascotState.IDLE)

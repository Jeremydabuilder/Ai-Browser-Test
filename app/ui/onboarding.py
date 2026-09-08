"""The first-run dialog: three short screens, shown once.

Deliberately not a tutorial. Each screen is a sentence or two and one
decision - what PyBrowser is for, whether to set up Py now or later, and one
concrete thing to try. Skip is always visible; nothing here is required to
start browsing.

Distinct from the new-tab page's own onboarding card (app/browser/newtab.py):
that one is ambient and persists on the new-tab page until dismissed, this
one is a single modal shown once at first launch. Showing both back to back
would say the same thing twice, so MainWindow dismisses the new-tab card at
the same time it marks this dialog shown - see show_first_run_if_needed.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

#: (title, body, [(button label, prompt), ...]) - the mission suggestions on
#: the last screen. Each populates the AI panel rather than sending
#: immediately - the same "hand it over, let the user look before it goes"
#: rule every other quick action in the app follows.
_TRY_SUGGESTIONS = (
    ("Research something", "Research a topic across several sources and give me "
                           "the important findings."),
    ("Compare two products", "Compare two products on price, features, and "
                             "reviews."),
    ("Find the best option", "Find the best option based on my requirements."),
)


class _SuggestionCard(QPushButton):
    """One "try this" suggestion: a label and a one-line blurb, styled like
    the new-tab page's own quick-action cards - a mission suggestion here
    should look like the same offer, not a plain dialog button."""

    def __init__(self, label: str, blurb: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QPushButton {{ background:{c.surface}; border:1px solid {c.line};"
            f" border-radius:{m.radius_lg}px; padding:{m.space_2}px {m.space_3}px;"
            f" text-align:left; color:{c.text}; }}"
            f"QPushButton:hover {{ border-color:{c.accent}; background:{c.surface_hover}; }}"
            f"QPushButton:pressed {{ background:{c.surface_alt}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_1, 0, m.space_1, 0)
        layout.setSpacing(1)
        title = QLabel(label, self)
        title.setStyleSheet(f"background:transparent; border:none; color:{c.text};"
                            f" font-size:{m.text}px; font-weight:600;")
        layout.addWidget(title)
        subtitle = QLabel(blurb, self)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"background:transparent; border:none; color:{c.muted};"
                               f" font-size:{m.text_xs}px;")
        layout.addWidget(subtitle)


class FirstRunDialog(QDialog):
    """Welcome -> choose a provider -> try a mission. Shown once."""

    #: The user chose to set up a provider now - MainWindow opens the real
    #: Configure AI Agent dialog for it, since this one has no idea how to.
    configure_provider_requested = Signal()
    #: The user picked a suggested mission - carries the prompt text, handed
    #: to the AI panel exactly like any other quick action.
    try_mission_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colours = c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setWindowTitle("Welcome")
        self.setFixedSize(460, 480)
        self.setStyleSheet(f"QDialog {{ background:{c.bg}; }}")

        # A row of dots naming which of the three screens this is - the one
        # piece of orientation a modal like this needs, so "Back" always has
        # an obvious meaning.
        self._steps_row = QHBoxLayout()
        self._steps_row.setSpacing(m.space_1)
        self._steps_row.addStretch(1)
        self._step_dots: list[QLabel] = []
        for _ in range(3):
            dot = QLabel("●", self)
            dot.setStyleSheet(f"color:{c.line_strong}; font-size:{m.text_xs}px;"
                              " background:transparent; border:none;")
            self._steps_row.addWidget(dot)
            self._step_dots.append(dot)
        self._steps_row.addStretch(1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._welcome_page())
        self.stack.addWidget(self._provider_page())
        self.stack.addWidget(self._try_page())

        self.back_button = QPushButton("Back", self)
        self.back_button.setProperty("kind", "quiet")
        self.back_button.clicked.connect(self._go_back)
        self.skip_button = QPushButton("Skip", self)
        self.skip_button.setProperty("kind", "quiet")
        self.skip_button.clicked.connect(self.accept)
        self.next_button = QPushButton("Next", self)
        self.next_button.setProperty("kind", "primary")
        self.next_button.setMinimumWidth(120)
        self.next_button.clicked.connect(self._go_next)

        footer = QHBoxLayout()
        footer.setSpacing(m.space_2)
        footer.addWidget(self.back_button)
        footer.addWidget(self.skip_button)
        footer.addStretch(1)
        footer.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_5, m.space_5, m.space_4)
        layout.setSpacing(m.space_4)
        layout.addLayout(self._steps_row)
        layout.addWidget(self.stack, 1)
        layout.addLayout(footer)

        self._update_footer()

    # -- pages -------------------------------------------------------------
    def _page(self, title: str, body: str, *, with_mascot: bool = False) -> tuple[QWidget, QVBoxLayout]:
        c, m = self._colours, theme.METRICS
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.space_3)
        if with_mascot:
            from app.ui.mascot import Mascot

            mascot = Mascot(152, page)
            mascot_row = QHBoxLayout()
            mascot_row.addStretch(1)
            mascot_row.addWidget(mascot)
            mascot_row.addStretch(1)
            layout.addLayout(mascot_row)
        heading = QLabel(title, page)
        heading.setAlignment(Qt.AlignmentFlag.AlignHCenter if with_mascot
                            else Qt.AlignmentFlag.AlignLeft)
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"color:{c.text}; font-size:{m.text_lg}px; font-weight:700;")
        layout.addWidget(heading)
        text = QLabel(body, page)
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignHCenter if with_mascot
                          else Qt.AlignmentFlag.AlignLeft)
        text.setStyleSheet(f"color:{c.muted}; font-size:{m.text}px;")
        layout.addWidget(text)
        return page, layout

    def _welcome_page(self) -> QWidget:
        page, _layout = self._page(
            "Welcome to PyBrowser",
            "Browse normally — or give your browser a mission. Tell Py "
            "what you want done, and it researches, compares, and acts across "
            "the web while keeping you informed and in control.",
            with_mascot=True)
        return page

    def _provider_page(self) -> QWidget:
        page, layout = self._page(
            "Choose your AI provider",
            "Py needs an API key to do anything. Set one up now, or skip this "
            "and do it later from Tools → Configure AI Agent.")
        layout.addStretch(1)
        configure = QPushButton("Configure now", page)
        configure.setProperty("kind", "primary")
        configure.setMinimumHeight(theme.METRICS.control)
        configure.clicked.connect(self._request_configure)
        layout.addWidget(configure)
        return page

    def _try_page(self) -> QWidget:
        page, layout = self._page(
            "Try your first mission",
            "A few ideas to get started — pick one, or write your own once "
            "you're browsing:")
        layout.setSpacing(theme.METRICS.space_2)
        for label, prompt in _TRY_SUGGESTIONS:
            card = _SuggestionCard(label, prompt, page)
            card.clicked.connect(
                lambda _checked=False, text=prompt: self._request_mission(text))
            layout.addWidget(card)
        layout.addStretch(1)
        return page

    # -- navigation ----------------------------------------------------------
    def _go_next(self) -> None:
        if self.stack.currentIndex() == self.stack.count() - 1:
            self.accept()
            return
        self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
        self._update_footer()
        self._animate_page_in()

    def _go_back(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._update_footer()
        self._animate_page_in()

    def _animate_page_in(self) -> None:
        """A soft fade for the page that just arrived - three screens shown
        once should feel like a considered sequence, not a slideshow that
        snaps between slides."""
        from app.ui.mascot import reduced_motion

        if reduced_motion():
            return
        page = self.stack.currentWidget()
        if page is None:
            return
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", page)
        anim.setDuration(220)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        page._enter_anim = anim  # noqa: SLF001 - keeping it alive, not private access
        anim.start()

    def _pulse_dot(self, dot: QLabel) -> None:
        """A small flourish on the step dot that just became current - one
        beat, not a loop, so three quiet screens still feel like they are
        responding to you rather than just swapping content underneath."""
        from app.ui.mascot import reduced_motion

        if reduced_motion():
            return
        effect = QGraphicsOpacityEffect(dot)
        dot.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", dot)
        anim.setDuration(280)
        anim.setStartValue(0.25)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        dot._pulse_anim = anim  # noqa: SLF001 - keeping it alive, not private access
        anim.start()

    def _update_footer(self) -> None:
        index = self.stack.currentIndex()
        on_first = index == 0
        on_last = index == self.stack.count() - 1
        self.back_button.setVisible(not on_first)
        self.next_button.setText("Start browsing" if on_last else "Next")
        c = self._colours
        for i, dot in enumerate(self._step_dots):
            if i == index:
                self._pulse_dot(dot)
            dot.setStyleSheet(
                f"color:{c.accent if i == index else c.line_strong};"
                f" font-size:{theme.METRICS.text_xs}px; background:transparent; border:none;")

    def _request_configure(self) -> None:
        self.configure_provider_requested.emit()
        self._go_next()

    def _request_mission(self, prompt: str) -> None:
        self.try_mission_requested.emit(prompt)
        self.accept()

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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

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
        self.setWindowTitle("Welcome")
        self.setMinimumWidth(420)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._welcome_page())
        self.stack.addWidget(self._provider_page())
        self.stack.addWidget(self._try_page())

        self.back_button = QPushButton("Back", self)
        self.back_button.clicked.connect(self._go_back)
        self.skip_button = QPushButton("Skip", self)
        self.skip_button.setProperty("kind", "quiet")
        self.skip_button.clicked.connect(self.accept)
        self.next_button = QPushButton("Next", self)
        self.next_button.setProperty("kind", "primary")
        self.next_button.clicked.connect(self._go_next)

        footer = QHBoxLayout()
        footer.addWidget(self.back_button)
        footer.addWidget(self.skip_button)
        footer.addStretch(1)
        footer.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.stack, 1)
        layout.addLayout(footer)

        self._update_footer()

    # -- pages -------------------------------------------------------------
    def _page(self, title: str, body: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        heading = QLabel(f"<h2>{title}</h2>", page)
        layout.addWidget(heading)
        text = QLabel(body, page)
        text.setWordWrap(True)
        layout.addWidget(text)
        return page, layout

    def _welcome_page(self) -> QWidget:
        page, _layout = self._page(
            "Welcome to PyBrowser",
            "Browse normally — or give your browser a mission. Tell Py "
            "what you want done, and it researches, compares, and acts across "
            "the web while keeping you informed and in control.")
        return page

    def _provider_page(self) -> QWidget:
        page, layout = self._page(
            "Choose your AI provider",
            "Py needs an API key to do anything. Set one up now, or skip this "
            "and do it later from Tools → Configure AI Agent.")
        configure = QPushButton("Configure now", page)
        configure.setProperty("kind", "primary")
        configure.clicked.connect(self._request_configure)
        layout.addWidget(configure)
        layout.addStretch(1)
        return page

    def _try_page(self) -> QWidget:
        page, layout = self._page(
            "Try your first mission",
            "A few ideas to get started — pick one, or write your own once "
            "you're browsing:")
        for label, prompt in _TRY_SUGGESTIONS:
            button = QPushButton(label, page)
            button.setToolTip(prompt)
            button.clicked.connect(
                lambda _checked=False, text=prompt: self._request_mission(text))
            layout.addWidget(button)
        layout.addStretch(1)
        return page

    # -- navigation ----------------------------------------------------------
    def _go_next(self) -> None:
        if self.stack.currentIndex() == self.stack.count() - 1:
            self.accept()
            return
        self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
        self._update_footer()

    def _go_back(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._update_footer()

    def _update_footer(self) -> None:
        on_first = self.stack.currentIndex() == 0
        on_last = self.stack.currentIndex() == self.stack.count() - 1
        self.back_button.setVisible(not on_first)
        self.next_button.setText("Start browsing" if on_last else "Next")

    def _request_configure(self) -> None:
        self.configure_provider_requested.emit()
        self._go_next()

    def _request_mission(self, prompt: str) -> None:
        self.try_mission_requested.emit(prompt)
        self.accept()

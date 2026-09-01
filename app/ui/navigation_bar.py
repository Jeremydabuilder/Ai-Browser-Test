"""The toolbar: back / forward / reload / home, the address bar, bookmark star."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCompleter,
    QLineEdit,
    QSizePolicy,
    QToolBar,
    QWidget,
)

from app.ui import icons, theme


class AddressBar(QLineEdit):
    """Address bar that selects all of its text the first time you click it.

    Carries a leading icon that says what pressing Enter would do: a magnifier
    when the text is a search, a globe-ish page mark when it is an address.
    That is the one piece of feedback an address bar can give before you commit,
    and PyBrowser already knows the answer - `url_utils.is_probably_search` is
    the same function the navigation itself uses.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Search or enter address")
        self.setClearButtonEnabled(True)
        self.setAccessibleName("Address and search bar")
        self._select_on_next_focus = False
        completer = QCompleter(self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.setCompleter(completer)

        from PySide6.QtWidgets import QApplication

        colours = theme.palette_for(QApplication.instance())
        self._search_icon = icons.icon("search_glass", colours.muted, size=32, weight=2.0) \
            if "search_glass" in icons.available() else icons.icon("page", colours.muted)
        self._page_icon = icons.icon("page", colours.muted, size=32, weight=1.8)
        self._mode_action = self.addAction(
            self._search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self._mode_action.setToolTip("")
        self.textChanged.connect(self._update_mode_icon)
        self._update_mode_icon("")

    def _update_mode_icon(self, text: str) -> None:
        from app.utils import urls as url_utils

        searching = url_utils.is_probably_search(text)
        self._mode_action.setIcon(self._search_icon if searching else self._page_icon)
        self._mode_action.setToolTip(
            "Press Enter to search the web" if searching
            else "Press Enter to go to this address")

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._select_on_next_focus = True

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        if self._select_on_next_focus:
            self._select_on_next_focus = False
            self.selectAll()

    def focus_and_select(self) -> None:
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.selectAll()


class NavigationBar(QToolBar):
    """Emits intent signals; it never navigates by itself.

    Keeping the toolbar dumb means the main window stays the single place that
    decides what a click actually does - handy later when the AI agent needs to
    trigger the same actions programmatically.
    """

    back_requested = Signal()
    forward_requested = Signal()
    reload_requested = Signal()
    stop_requested = Signal()
    home_requested = Signal()
    navigate_requested = Signal(str)
    bookmark_toggled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Navigation", parent)
        self.setMovable(False)
        self.setIconSize(QSize(19, 19))

        # Our own icons rather than whatever the desktop happens to provide.
        # Asking the system for "bookmark-new" gave a floppy disk on a machine
        # with no icon theme, and a different browser on every machine that
        # had one.
        from PySide6.QtWidgets import QApplication

        colours = theme.palette_for(QApplication.instance())

        def icon(name: str, filled: bool = False):
            return icons.icon(name, colours.text, disabled_color=colours.muted,
                              filled=filled)

        self.back_action = QAction(icon("back"), "Back", self)
        self.back_action.setShortcut(QKeySequence.StandardKey.Back)
        self.back_action.triggered.connect(self.back_requested)

        self.forward_action = QAction(icon("forward"), "Forward", self)
        self.forward_action.setShortcut(QKeySequence.StandardKey.Forward)
        self.forward_action.triggered.connect(self.forward_requested)

        # One button that flips between Reload and Stop, like every real browser.
        self._reload_icon = icon("reload")
        self._stop_icon = icon("stop")
        self.reload_action = QAction(self._reload_icon, "Reload", self)
        self.reload_action.triggered.connect(self._on_reload_clicked)

        self.home_action = QAction(icon("home"), "Home", self)
        self.home_action.triggered.connect(self.home_requested)

        self.address_bar = AddressBar(self)
        self.address_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.address_bar.returnPressed.connect(self._on_return_pressed)

        # Outline when not bookmarked, filled and accent-coloured when it is -
        # the state has to be readable at a glance, not inferred from a
        # pressed-button shade.
        self._star_icon = icon("star")
        self._star_filled = icons.icon("star", colours.accent, filled=True)
        self.bookmark_action = QAction(self._star_icon, "Bookmark this page", self)
        self.bookmark_action.setCheckable(True)
        self.bookmark_action.triggered.connect(lambda _checked: self.bookmark_toggled.emit())

        for action in (self.back_action, self.forward_action, self.reload_action, self.home_action):
            self.addAction(action)
        self.addWidget(self.address_bar)
        self.addAction(self.bookmark_action)

        self._loading = False

    def _on_reload_clicked(self) -> None:
        if self._loading:
            self.stop_requested.emit()
        else:
            self.reload_requested.emit()

    def _on_return_pressed(self) -> None:
        self.navigate_requested.emit(self.address_bar.text())

    # -- state updates driven by the current tab ------------------------
    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        self.reload_action.setIcon(self._stop_icon if loading else self._reload_icon)
        self.reload_action.setText("Stop" if loading else "Reload")

    def set_url_text(self, text: str) -> None:
        # Don't stomp on what the user is typing.
        if self.address_bar.hasFocus():
            return
        self.address_bar.setText(text)
        self.address_bar.setCursorPosition(0)

    def set_navigation_state(self, can_back: bool, can_forward: bool) -> None:
        self.back_action.setEnabled(can_back)
        self.forward_action.setEnabled(can_forward)

    def set_bookmarked(self, bookmarked: bool) -> None:
        self.bookmark_action.setChecked(bookmarked)
        self.bookmark_action.setIcon(self._star_filled if bookmarked else self._star_icon)
        self.bookmark_action.setText(
            "Remove bookmark" if bookmarked else "Bookmark this page"
        )

    def set_completions(self, urls: list[str]) -> None:
        completer = self.address_bar.completer()
        if completer is None:
            return
        from PySide6.QtCore import QStringListModel

        model = completer.model()
        if isinstance(model, QStringListModel):
            model.setStringList(urls)
        else:
            completer.setModel(QStringListModel(urls, completer))

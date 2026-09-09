"""Preferences: what a new tab opens, and where searches go.

Deliberately small. These are the two settings that decide what the browser
does the moment you open it, and both were previously reachable only by
editing the database, which is not a setting - it is a secret.

The distinction the dialog is built around: **the search provider is where
searches go, not what the browser opens.** Those were the same thing in every
version before this one, and conflating them is what makes a browser feel like
someone else's home page with a window around it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from app.storage.settings import (
    NEW_TAB_CUSTOM,
    NEW_TAB_MODES,
    SettingsStore,
)
from app.ui import theme

_SEARCH_PRESETS = (
    ("DuckDuckGo", "https://duckduckgo.com/?q={query}"),
    ("Google", "https://www.google.com/search?q={query}"),
    ("Bing", "https://www.bing.com/search?q={query}"),
    ("Startpage", "https://www.startpage.com/sp/search?query={query}"),
)


class SettingsDialog(QDialog):
    """Edit the new-tab and search preferences."""

    #: Emitted after a save, so the window can pick the new values up without
    #: reaching into the dialog to find out what changed.
    saved = Signal()

    def __init__(self, settings: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS
        self.setWindowTitle("Settings")
        self.resize(520, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_5, m.space_5, m.space_4)
        layout.setSpacing(m.space_2)

        def section_heading(text: str) -> QLabel:
            label = QLabel(text, self)
            label.setStyleSheet(
                f"color:{c.text}; font-size:{m.text}px; font-weight:600;")
            return label

        def note(text: str) -> QLabel:
            label = QLabel(text, self)
            label.setWordWrap(True)
            label.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
            return label

        def divider() -> QFrame:
            line = QFrame(self)
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet(f"background:{c.line}; max-height:1px; border:none;")
            return line

        heading = section_heading("When I open a new tab or press Home")
        layout.addWidget(heading)

        # Indented under its heading, the same way a menu's sub-items read as
        # belonging to the item above them - without it the radio group and
        # the section title floated at the same depth as everything else.
        modes_layout = QVBoxLayout()
        modes_layout.setContentsMargins(m.space_3, m.space_1, 0, 0)
        modes_layout.setSpacing(0)
        self._modes = QButtonGroup(self)
        current_mode = settings.new_tab_mode
        for index, (mode, label) in enumerate(NEW_TAB_MODES):
            button = QRadioButton(label, self)
            button.setChecked(mode == current_mode)
            self._modes.addButton(button, index)
            modes_layout.addWidget(button)

        self.custom = QLineEdit(settings.new_tab_custom_url, self)
        self.custom.setPlaceholderText("https://example.com/")
        modes_layout.addWidget(self.custom)
        layout.addLayout(modes_layout)
        self._modes.idToggled.connect(self._sync_custom)
        self._sync_custom()

        layout.addWidget(note(
            "PyBrowser New Tab is a page inside the browser. It opens "
            "instantly, works offline, and sends nothing anywhere."))

        layout.addSpacing(m.space_2)
        layout.addWidget(divider())
        layout.addSpacing(m.space_2)
        layout.addWidget(section_heading("Search with"))

        self.search = QLineEdit(settings.search_url, self)
        self.search.setPlaceholderText("https://example.com/search?q={query}")
        layout.addWidget(self.search)
        self.search.textChanged.connect(self._clear_problem)
        self.custom.textChanged.connect(self._clear_problem)

        presets = QLabel(
            "  ".join(f"<a href='{url}' style='color:{c.accent}; "
                      f"text-decoration:none;'>{name}</a>"
                      for name, url in _SEARCH_PRESETS),
            self)
        presets.setTextFormat(Qt.TextFormat.RichText)
        presets.setStyleSheet(f"font-size:{m.text_sm}px;")
        presets.linkActivated.connect(self.search.setText)
        layout.addWidget(presets)

        layout.addWidget(note(
            "Must contain <code>{query}</code>, which is replaced with what you "
            "typed. This is where searches go — it is not the browser's home page."))

        self.problem = QLabel("", self)
        self.problem.setStyleSheet(f"color:{c.danger}; font-size:{m.text_sm}px;")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)

        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setProperty("kind", "primary")
            # Enter should save, from anywhere in the dialog, the same way
            # Enter confirms every other dialog in the app - not just when
            # focus happens to already sit on the button.
            save_button.setDefault(True)
            save_button.setAutoDefault(True)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- behaviour --------------------------------------------------------
    def _selected_mode(self) -> str:
        index = self._modes.checkedId()
        return NEW_TAB_MODES[index][0] if 0 <= index < len(NEW_TAB_MODES) else NEW_TAB_MODES[0][0]

    def _sync_custom(self) -> None:
        self.custom.setEnabled(self._selected_mode() == NEW_TAB_CUSTOM)

    def _clear_problem(self) -> None:
        # A stale "The search address must contain {query}" left on screen
        # after the user has already fixed it reads as the browser not
        # noticing its own error went away.
        self.problem.setText("")

    def _save(self) -> None:
        template = self.search.text().strip()
        if "{query}" not in template:
            # Saving this would break every search silently, so refuse and say
            # why rather than accepting it and leaving the user to work it out.
            self.problem.setText("The search address must contain {query}.")
            return
        mode = self._selected_mode()
        if mode == NEW_TAB_CUSTOM and not self.custom.text().strip():
            self.problem.setText("Enter the address you want new tabs to open.")
            return

        self._settings.search_url = template
        self._settings.new_tab_mode = mode
        self._settings.new_tab_custom_url = self.custom.text()
        self.saved.emit()
        self.accept()

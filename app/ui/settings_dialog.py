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
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
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
        self.setWindowTitle("Settings")
        self.resize(520, 460)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        heading = QLabel("<b>When I open a new tab or press Home</b>", self)
        layout.addWidget(heading)

        self._modes = QButtonGroup(self)
        current_mode = settings.new_tab_mode
        for index, (mode, label) in enumerate(NEW_TAB_MODES):
            button = QRadioButton(label, self)
            button.setChecked(mode == current_mode)
            self._modes.addButton(button, index)
            layout.addWidget(button)

        self.custom = QLineEdit(settings.new_tab_custom_url, self)
        self.custom.setPlaceholderText("https://example.com/")
        layout.addWidget(self.custom)
        self._modes.idToggled.connect(self._sync_custom)
        self._sync_custom()

        note = QLabel(
            "<span style='color:#666'>PyBrowser New Tab is a page inside the "
            "browser. It opens instantly, works offline, and sends nothing "
            "anywhere.</span>", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addSpacing(10)
        layout.addWidget(QLabel("<b>Search with</b>", self))

        self.search = QLineEdit(settings.search_url, self)
        self.search.setPlaceholderText("https://example.com/search?q={query}")
        layout.addWidget(self.search)

        presets = QLabel(
            "  ".join(f"<a href='{url}'>{name}</a>" for name, url in _SEARCH_PRESETS),
            self)
        presets.setTextFormat(Qt.TextFormat.RichText)
        presets.linkActivated.connect(self.search.setText)
        layout.addWidget(presets)

        search_note = QLabel(
            "<span style='color:#666'>Must contain <code>{query}</code>, which is "
            "replaced with what you typed. This is where searches go — it is not "
            "the browser's home page.</span>", self)
        search_note.setWordWrap(True)
        layout.addWidget(search_note)

        self.problem = QLabel("", self)
        self.problem.setStyleSheet("color:#a11;")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)

        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- behaviour --------------------------------------------------------
    def _selected_mode(self) -> str:
        index = self._modes.checkedId()
        return NEW_TAB_MODES[index][0] if 0 <= index < len(NEW_TAB_MODES) else NEW_TAB_MODES[0][0]

    def _sync_custom(self) -> None:
        self.custom.setEnabled(self._selected_mode() == NEW_TAB_CUSTOM)

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

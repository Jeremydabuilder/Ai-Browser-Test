"""Settings -> Privacy & Security: Phase 15's data-egress firewall and
prompt-injection protection, on by default (unlike Semantic History,
which is opt-in indexing of the user's own data - these protect data
already about to leave the browser, so shipping them off by default
would mean shipping the firewall inert).

Toggling a checkbox here updates both the persisted setting and the
live app.security module state (app.security.firewall.set_enabled /
app.security.injection.set_enabled), so a change takes effect on the
very next tool result - no restart needed.
"""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from app.security import firewall, injection
from app.ui import theme

_REDACT_EXPLANATION = (
    "Automatically replace likely secrets - passwords, API keys, access tokens, "
    "private keys, credit card and CVV numbers - with a placeholder like "
    "[REDACTED_API_KEY] before page text, PDFs, files, or knowledge results are "
    "sent to an AI provider or an external MCP tool. The original value is never "
    "logged, only that something was redacted.")

_WARN_EXPLANATION = (
    "Flag likely personal information - email addresses, phone numbers - in "
    "content before it is sent, so this is visible rather than silent.")

_INJECTION_EXPLANATION = (
    "Watch for text on a page, in a file, or in a tool result that tries to look "
    "like an instruction (\"ignore previous instructions\", a fake system message, "
    "a claim that you already approved something). Detected text is logged for "
    "your awareness; it never gains any extra authority either way - a webpage "
    "can never grant itself permission to use a tool, with or without this on.")


class SecuritySettingsPanel(QWidget):
    def __init__(self, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_4, m.space_5, m.space_4)
        layout.setSpacing(m.space_3)

        heading = QLabel("Data protection", self)
        heading.setStyleSheet(f"font-weight:600;")
        layout.addWidget(heading)

        self.redact_check = QCheckBox("Redact likely secrets automatically", self)
        self.redact_check.setChecked(settings.redact_secrets_enabled)
        self.redact_check.toggled.connect(self._on_redact_toggled)
        layout.addWidget(self.redact_check)
        redact_note = QLabel(_REDACT_EXPLANATION, self)
        redact_note.setWordWrap(True)
        layout.addWidget(redact_note)

        self.warn_check = QCheckBox("Warn before sending personal information", self)
        self.warn_check.setChecked(settings.warn_personal_info_enabled)
        self.warn_check.toggled.connect(self._on_warn_toggled)
        layout.addWidget(self.warn_check)
        warn_note = QLabel(_WARN_EXPLANATION, self)
        warn_note.setWordWrap(True)
        layout.addWidget(warn_note)

        injection_heading = QLabel("Prompt injection protection", self)
        injection_heading.setStyleSheet(f"font-weight:600;")
        layout.addWidget(injection_heading)

        self.injection_check = QCheckBox("Protection", self)
        self.injection_check.setChecked(settings.injection_protection_enabled)
        self.injection_check.toggled.connect(self._on_injection_toggled)
        layout.addWidget(self.injection_check)
        injection_note = QLabel(_INJECTION_EXPLANATION, self)
        injection_note.setWordWrap(True)
        layout.addWidget(injection_note)

        layout.addStretch(1)

    def _on_redact_toggled(self, checked: bool) -> None:
        self._settings.redact_secrets_enabled = checked
        firewall.set_enabled(checked)

    def _on_warn_toggled(self, checked: bool) -> None:
        self._settings.warn_personal_info_enabled = checked

    def _on_injection_toggled(self, checked: bool) -> None:
        self._settings.injection_protection_enabled = checked
        injection.set_enabled(checked)


def sync_from_settings(settings) -> None:
    """Apply the persisted settings to the live app.security module state -
    called once at startup (MainWindow.__init__), before this panel has
    even been opened, so the firewall reflects the user's saved choice
    from the very first tool call rather than defaulting to "on" until
    Settings happens to be opened."""
    firewall.set_enabled(settings.redact_secrets_enabled)
    injection.set_enabled(settings.injection_protection_enabled)

"""Configuring the Anthropic API key, and building the agent if one exists.

Kept apart from MainWindow so the browser has exactly one dependency on the
agent package - this module - and starts perfectly well without it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget


class ApiKeyDialog(QDialog):
    """Ask for an API key and store it in the OS keyring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.agent.keys import ApiKeyStore

        self._store = ApiKeyStore()
        from app.agent.credentials import SETUP_HELP, options_summary, resolve

        self.setWindowTitle("Configure AI Agent")
        self.resize(600, 420)

        layout = QVBoxLayout(self)
        active = resolve(self._store)
        layout.addWidget(QLabel(f"<b>Currently using:</b> {active.describe()}", self))

        # An API key is only one of several ways in, and the least good one -
        # say so, rather than implying a key is required.
        rows = []
        for mode, present, help_text in options_summary():
            mark = "\u2713" if present else "\u2013"
            name = {"oauth_profile": "Sign in with the Anthropic CLI",
                    "keyring": "API key in the OS keyring",
                    "env_key": "ANTHROPIC_API_KEY",
                    "auth_token": "ANTHROPIC_AUTH_TOKEN",
                    "bedrock": "Amazon Bedrock",
                    "vertex": "Google Vertex AI"}.get(mode, mode)
            weight = "b" if present else "span"
            rows.append(f"<tr><td>{mark}</td><td><{weight}>{name}</{weight}></td>"
                        f"<td style='color:#555'>{help_text}</td></tr>")
        options = QLabel(
            "<p>You do <b>not</b> need to paste an API key. Any of these works, "
            "and the first is preferred - it stores no secret at all:</p>"
            "<table cellpadding=3>" + "".join(rows) + "</table>",
            self)
        options.setWordWrap(True)
        options.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(options)

        explanation = QLabel(
            "<hr><b>Or paste an API key.</b> It is stored in your operating "
            "system's keyring — never in this project, its database, or any "
            "file in the repository.",
            self,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.field = QLineEdit(self)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("sk-ant-…")
        layout.addWidget(self.field)

        save = QPushButton("Save to keyring", self)
        save.clicked.connect(self._save)
        layout.addWidget(save)

        if self._store.get_keyring_key():
            clear = QPushButton("Remove stored key", self)
            clear.clicked.connect(self._clear)
            layout.addWidget(clear)

    def _save(self) -> None:
        from app.agent.keys import KeyringUnavailable

        try:
            self._store.set_key(self.field.text())
        except ValueError:
            QMessageBox.warning(self, "Configure AI Agent", "Enter a key first.")
            return
        except KeyringUnavailable as exc:
            QMessageBox.warning(
                self, "Configure AI Agent",
                "This system has no usable keyring, so the key was not saved.\n\n"
                "Set the ANTHROPIC_API_KEY environment variable before launching "
                f"the browser instead.\n\nDetail: {exc}")
            return
        finally:
            self.field.clear()   # do not leave the secret in a widget
        QMessageBox.information(
            self, "Configure AI Agent",
            "Key saved. Restart the browser to enable the agent.")
        self.accept()

    def _clear(self) -> None:
        self._store.clear_key()
        QMessageBox.information(self, "Configure AI Agent", "Stored key removed.")
        self.accept()


def build_session(browser, parent=None):
    """Create an AgentSession if the agent can run, else return (None, reason).

    Every failure path here is soft. A missing SDK or credential must leave a
    working browser with an agent panel that explains itself, never a crash on
    startup.
    """
    try:
        from app.agent.claude_client import ClaudeClient
        from app.agent.config import AgentConfig
        from app.agent.credentials import resolve
        from app.agent.session import AgentSession
    except ImportError as exc:
        return None, f"the anthropic SDK is not installed ({exc})"

    try:
        credential = resolve()
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the credential could not be read ({exc})"
    if not credential.available:
        return None, ("no credential is configured - sign in with `ant auth login`, "
                      "set ANTHROPIC_API_KEY, or add a key in "
                      "Tools \u2192 Configure AI Agent")
    try:
        config = AgentConfig.from_environment()
        return AgentSession(browser, ClaudeClient(credential, config), config, parent), ""
    except BaseException as exc:  # noqa: BLE001
        # Nothing the agent does may take the browser down with it.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the agent could not start ({exc})"

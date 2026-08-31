"""Configuring the Anthropic API key, and building the agent if one exists.

Kept apart from MainWindow so the browser has exactly one dependency on the
agent package - this module - and starts perfectly well without it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget


class ApiKeyDialog(QDialog):
    """Ask for an API key and store it in the OS keyring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.agent.keys import ApiKeyStore

        self._store = ApiKeyStore()
        self.setWindowTitle("Configure AI Agent")
        self.resize(520, 200)

        layout = QVBoxLayout(self)
        source = self._store.describe()
        layout.addWidget(QLabel(f"<b>Current status:</b> {source.detail}", self))

        explanation = QLabel(
            "Paste an Anthropic API key. It is stored in your operating system's "
            "keyring — never in this project, its database, or any file in the "
            "repository.<br><br>"
            "If your system has no keyring, set the <code>ANTHROPIC_API_KEY</code> "
            "environment variable before launching instead.",
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

        if source.available:
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

    Every failure path here is soft. A missing SDK or key must leave a working
    browser with an agent panel that explains itself, never a crash on startup.
    """
    try:
        from app.agent.claude_client import ClaudeClient
        from app.agent.config import AgentConfig
        from app.agent.keys import ApiKeyStore
        from app.agent.session import AgentSession
    except ImportError as exc:
        return None, f"the anthropic SDK is not installed ({exc})"

    try:
        key = ApiKeyStore().get_key()
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the API key could not be read ({exc})"
    if not key:
        return None, "no Anthropic API key is configured"
    try:
        config = AgentConfig.from_environment()
        return AgentSession(browser, ClaudeClient(key, config), config, parent), ""
    except BaseException as exc:  # noqa: BLE001
        # Nothing the agent does may take the browser down with it.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the agent could not start ({exc})"

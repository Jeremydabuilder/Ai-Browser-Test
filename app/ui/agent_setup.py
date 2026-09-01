"""Configuring the agent: how it authenticates, which model, how hard it thinks.

Kept apart from MainWindow so the browser has exactly one dependency on the
agent package - this module - and starts perfectly well without it.

The model and effort controls are here rather than buried in a config file
because they are the two settings that decide what a task costs, and a cost
control the user cannot find is not a cost control.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class ApiKeyDialog(QDialog):
    """Credentials, model and effort - everything the agent needs to be told.

    Emits ``saved`` whenever anything changes, so the window can reload the
    agent while the browser keeps running. Nothing here restarts anything.

    ``settings`` is a SettingsStore, or None in which case the model and effort
    choices are shown but cannot be remembered (the environment variables still
    work). The browser always has one, so None is really only for tests.
    """

    #: Something changed that the agent needs to pick up.
    saved = Signal()

    def __init__(self, parent: QWidget | None = None, settings=None) -> None:
        super().__init__(parent)
        from app.agent.keys import ApiKeyStore

        self._store = ApiKeyStore()
        self._settings = settings
        from app.agent.credentials import SETUP_HELP, options_summary, resolve

        self.setWindowTitle("Configure AI Agent")
        self.resize(640, 640)

        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)

        active = resolve(self._store)
        layout.addWidget(QLabel(f"<b>Currently using:</b> {active.describe()}", body))

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
            body)
        options.setWordWrap(True)
        options.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(options)

        layout.addWidget(self._cost_section(body))

        explanation = QLabel(
            "<hr><b>Or paste an API key.</b> It is stored in your operating "
            "system's keyring — never in this project, its database, or any "
            "file in the repository.",
            body,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.field = QLineEdit(body)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("sk-ant-…")
        layout.addWidget(self.field)

        save = QPushButton("Save to keyring", body)
        save.clicked.connect(self._save)
        layout.addWidget(save)

        if self._store.get_keyring_key():
            clear = QPushButton("Remove stored key", body)
            clear.clicked.connect(self._clear)
            layout.addWidget(clear)

    # -- what the task will cost -----------------------------------------
    def _cost_section(self, parent: QWidget) -> QWidget:
        """Model and effort pickers, with the trade-offs stated plainly.

        Neither of these is a free lunch, and the descriptions say so. The
        genuinely free saving - prompt caching - is on by default and has no
        control here, because there is no reason anyone would want it off.
        """
        from app.agent.config import (
            EFFORT_LEVELS,
            KEY_AGENT_EFFORT,
            KEY_AGENT_MODEL,
            MODELS,
            AgentConfig,
        )

        current = AgentConfig.from_environment(self._settings)

        box = QWidget(parent)
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)

        heading = QLabel(
            "<hr><b>Cost and capability</b><br>"
            "<span style='color:#555'>Responses are cached automatically, which "
            "cuts the cost of a multi-step task several-fold on its own and "
            "changes nothing about the answers. The two settings below do "
            "involve a trade-off.</span>", box)
        heading.setWordWrap(True)
        column.addWidget(heading)

        column.addWidget(QLabel("<b>Model</b>", box))
        self.model_box = QComboBox(box)
        for choice in MODELS:
            self.model_box.addItem(choice.label, choice.model_id)
        if self.model_box.findData(current.model) < 0:
            # A model set through the environment that is not in the catalogue.
            self.model_box.addItem(current.model, current.model)
        self.model_box.setCurrentIndex(self.model_box.findData(current.model))
        column.addWidget(self.model_box)
        self._model_note = QLabel("", box)
        self._model_note.setWordWrap(True)
        self._model_note.setStyleSheet("color:#555;")
        column.addWidget(self._model_note)
        self.model_box.currentIndexChanged.connect(self._update_model_note)
        self._update_model_note()

        column.addWidget(QLabel("<b>Effort</b>", box))
        self.effort_box = QComboBox(box)
        for level, description in EFFORT_LEVELS:
            self.effort_box.addItem(description, level)
        index = self.effort_box.findData(current.effort)
        self.effort_box.setCurrentIndex(index if index >= 0 else 0)
        column.addWidget(self.effort_box)

        apply_button = QPushButton("Save model and effort", box)
        apply_button.clicked.connect(
            lambda: self._save_preferences(KEY_AGENT_MODEL, KEY_AGENT_EFFORT))
        column.addWidget(apply_button)
        return box

    def _update_model_note(self) -> None:
        from app.agent.config import describe_model

        self._model_note.setText(describe_model(self.model_box.currentData()).note)

    def _save_preferences(self, model_key: str, effort_key: str) -> None:
        if self._settings is None:
            QMessageBox.warning(
                self, "Configure AI Agent",
                "Settings are unavailable, so this choice cannot be remembered. "
                "Set PYBROWSER_AGENT_MODEL and PYBROWSER_AGENT_EFFORT instead.")
            return
        self._settings.set(model_key, self.model_box.currentData())
        self._settings.set(effort_key, self.effort_box.currentData())
        QMessageBox.information(
            self, "Configure AI Agent",
            "Saved. Py picks this up as soon as you close this dialog, "
            "which begins a fresh conversation.\n\n"
            "The model is not changed mid-conversation on purpose - the prompt "
            "cache is per-model, so switching part-way through a task would "
            "throw away everything cached so far.")

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
        # No restart. The window re-reads the credential when this dialog
        # closes and rebuilds the agent if it changed - see
        # MainWindow._apply_agent_settings.
        self.saved.emit()
        QMessageBox.information(
            self, "Configure AI Agent",
            "Key saved. Py is ready to use.")
        self.accept()

    def _clear(self) -> None:
        self._store.clear_key()
        self.saved.emit()
        QMessageBox.information(self, "Configure AI Agent", "Stored key removed.")
        self.accept()


def build_session(browser, parent=None, settings=None):
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
        config = AgentConfig.from_environment(settings)
        return AgentSession(browser, ClaudeClient(credential, config), config, parent), ""
    except BaseException as exc:  # noqa: BLE001
        # Nothing the agent does may take the browser down with it.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the agent could not start ({exc})"

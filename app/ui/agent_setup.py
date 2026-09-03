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

        self.setWindowTitle("Configure AI Agent")
        self.resize(640, 680)

        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)

        layout.addWidget(self._provider_picker(body))

        self._anthropic_widget = self._anthropic_section(body)
        layout.addWidget(self._anthropic_widget)

        self._other_widget = self._other_provider_section(body)
        layout.addWidget(self._other_widget)

        # Without this, a QVBoxLayout inside a resizable QScrollArea gives
        # its leftover vertical space to whichever child has no stretch
        # factor of its own - which turned out to be the "Provider" label,
        # stretching it to fill the gap left whenever the shorter
        # Groq/OpenRouter section replaces the taller Anthropic one.
        layout.addStretch()

        self._show_provider(self.provider_box.currentData())

    # -- which provider ----------------------------------------------------
    def _provider_picker(self, parent: QWidget) -> QWidget:
        """Provider first, so everything below can be scoped to it.

        Free-provider testing is the whole point of offering Groq and
        OpenRouter: an Anthropic key costs money to use, and someone trying
        PyBrowser out should not have to pay to see whether the agent loop
        works for them.
        """
        from app.agent.config import PROVIDERS, AgentConfig

        current = AgentConfig.from_environment(self._settings)

        box = QWidget(parent)
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(QLabel("<b>Provider</b>", box))

        self.provider_box = QComboBox(box)
        for info in PROVIDERS:
            self.provider_box.addItem(info.label, info.id)
        index = self.provider_box.findData(current.provider)
        self.provider_box.setCurrentIndex(index if index >= 0 else 0)
        self.provider_box.currentIndexChanged.connect(
            lambda: self._show_provider(self.provider_box.currentData()))
        column.addWidget(self.provider_box)
        return box

    def _show_provider(self, provider_id: str) -> None:
        from app.agent.config import describe_provider

        is_anthropic = describe_provider(provider_id).is_anthropic
        self._anthropic_widget.setVisible(is_anthropic)
        self._other_widget.setVisible(not is_anthropic)
        if not is_anthropic:
            self._refresh_other_section(provider_id)

    # -- Anthropic: the full cascade, unchanged from before providers -----
    def _anthropic_section(self, parent: QWidget) -> QWidget:
        from app.agent.credentials import SETUP_HELP, options_summary, resolve

        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        active = resolve(self._store)
        layout.addWidget(QLabel(f"<b>Currently using:</b> {active.describe()}", box))

        # An API key is only one of several ways in, and the least good one -
        # say so, rather than implying a key is required.
        rows = []
        for mode, present, help_text in options_summary():
            mark = "✓" if present else "–"
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
            box)
        options.setWordWrap(True)
        options.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(options)

        layout.addWidget(self._cost_section(box))
        layout.addWidget(self._workspace_section(box))

        explanation = QLabel(
            "<hr><b>Or paste an API key.</b> It is stored in your operating "
            "system's keyring — never in this project, its database, or any "
            "file in the repository.",
            box,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.field = QLineEdit(box)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("sk-ant-…")
        layout.addWidget(self.field)

        save = QPushButton("Save to keyring", box)
        save.clicked.connect(self._save)
        layout.addWidget(save)

        if self._store.get_keyring_key():
            clear = QPushButton("Remove stored key", box)
            clear.clicked.connect(self._clear)
            layout.addWidget(clear)
        return box

    # -- Groq / OpenRouter: a key, a model, and a way to prove it works ----
    def _other_provider_section(self, parent: QWidget) -> QWidget:
        """One section shared by every non-Anthropic provider.

        Deliberately simpler than the Anthropic section: these providers have
        exactly one way in (a key) and no workspace/effort concept, so there
        is nothing else to ask for. Model support for tool calling varies
        and changes over time, so the model list is fetched live from the
        provider rather than trusted from a hardcoded catalogue - see
        ``OpenAICompatibleClient.list_models``.
        """
        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self._other_status = QLabel("", box)
        self._other_status.setWordWrap(True)
        layout.addWidget(self._other_status)

        self._other_help = QLabel("", box)
        self._other_help.setWordWrap(True)
        self._other_help.setTextFormat(Qt.TextFormat.RichText)
        self._other_help.setOpenExternalLinks(True)
        layout.addWidget(self._other_help)

        layout.addWidget(QLabel("<b>API key</b>", box))
        self._other_field = QLineEdit(box)
        self._other_field.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._other_field)

        key_row = QWidget(box)
        key_layout = QVBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        save_key = QPushButton("Save API key", key_row)
        save_key.clicked.connect(self._save_other_key)
        key_layout.addWidget(save_key)
        self._other_clear_button = QPushButton("Remove stored key", key_row)
        self._other_clear_button.clicked.connect(self._clear_other_key)
        key_layout.addWidget(self._other_clear_button)
        layout.addWidget(key_row)

        layout.addWidget(QLabel("<hr><b>Model</b>", box))
        self._other_model_box = QComboBox(box)
        self._other_model_box.setEditable(True)
        layout.addWidget(self._other_model_box)

        model_row = QWidget(box)
        model_layout = QVBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton("Refresh model list", model_row)
        refresh.clicked.connect(self._refresh_other_models)
        model_layout.addWidget(refresh)
        save_model = QPushButton("Save model", model_row)
        save_model.clicked.connect(self._save_other_model)
        model_layout.addWidget(save_model)
        test = QPushButton("Test Connection", model_row)
        test.clicked.connect(self._test_other_connection)
        model_layout.addWidget(test)
        layout.addWidget(model_row)

        self._other_result = QLabel("", box)
        self._other_result.setWordWrap(True)
        layout.addWidget(self._other_result)
        return box

    def _current_other_provider(self) -> str:
        return self.provider_box.currentData()

    def _other_client_class(self, provider_id: str | None = None):
        from app.agent.openai_compatible import GroqClient, OpenRouterClient

        provider_id = provider_id or self._current_other_provider()
        return {"groq": GroqClient, "openrouter": OpenRouterClient}[provider_id]

    def _refresh_other_section(self, provider_id: str) -> None:
        """Repopulate the shared section for whichever provider is selected."""
        from app.agent.config import AgentConfig, describe_provider
        from app.agent.credentials import resolve_for

        info = describe_provider(provider_id)
        credential = resolve_for(provider_id)
        self._other_status.setText(f"<b>Currently using:</b> {credential.describe()}")
        self._other_help.setText(info.key_help)
        self._other_field.clear()
        self._other_field.setPlaceholderText(
            "already set - paste a new key to replace it" if credential.available else "")
        self._other_clear_button.setVisible(credential.mode == "keyring")
        self._other_result.setText("")

        self._other_model_box.clear()
        current_model = AgentConfig.from_environment(self._settings).model
        client_class = self._other_client_class(provider_id)
        for entry in client_class.seed_models():
            self._other_model_box.addItem(entry.get("id", ""), entry.get("id", ""))
        if self.provider_box.currentData() == provider_id:
            if self._other_model_box.findData(current_model) < 0 and current_model:
                self._other_model_box.addItem(current_model, current_model)
            index = self._other_model_box.findData(current_model)
            if index >= 0:
                self._other_model_box.setCurrentIndex(index)
            else:
                self._other_model_box.setCurrentText(current_model)

    def _refresh_other_models(self) -> None:
        provider_id = self._current_other_provider()
        client_class = self._other_client_class(provider_id)
        key = self._other_field.text().strip()
        if not key:
            from app.agent.credentials import resolve_for

            credential = resolve_for(provider_id)
            key = credential.secret or ""
        if not key:
            QMessageBox.warning(self, "Configure AI Agent",
                                "Enter (or save) an API key first, then refresh.")
            return
        models = client_class.list_models(key)
        if not models:
            QMessageBox.warning(
                self, "Configure AI Agent",
                f"Could not load {client_class.label}'s model list. Check the key "
                "and your network connection, or enter a model id directly.")
            return
        current_text = self._other_model_box.currentText()
        self._other_model_box.clear()
        for entry in models:
            model_id = entry.get("id", "")
            if not model_id:
                continue
            supports_tools, note = client_class.capability_of(entry)
            label = model_id if supports_tools else f"{model_id} — unsupported: {note}"
            self._other_model_box.addItem(label, model_id)
            if not supports_tools:
                self._other_model_box.setItemData(
                    self._other_model_box.count() - 1, False, Qt.ItemDataRole.UserRole + 1)
        if current_text:
            index = self._other_model_box.findData(current_text)
            if index >= 0:
                self._other_model_box.setCurrentIndex(index)

    def _selected_other_model(self) -> str:
        index = self._other_model_box.currentIndex()
        data = self._other_model_box.itemData(index) if index >= 0 else None
        return data or self._other_model_box.currentText().strip()

    def _selected_other_model_supported(self) -> bool:
        index = self._other_model_box.currentIndex()
        if index < 0:
            return True
        supported = self._other_model_box.itemData(index, Qt.ItemDataRole.UserRole + 1)
        return supported is not False

    def _save_other_key(self) -> None:
        from app.agent.credentials import PROVIDER_KEY_INFO
        from app.agent.keys import KeyringUnavailable

        provider_id = self._current_other_provider()
        key = self._other_field.text().strip()
        if not key:
            QMessageBox.warning(self, "Configure AI Agent", "Enter a key first.")
            return
        label, env_var, account = PROVIDER_KEY_INFO[provider_id]
        store = self._other_store(account)
        try:
            store.set_key(key)
        except KeyringUnavailable as exc:
            QMessageBox.warning(
                self, "Configure AI Agent",
                "This system has no usable keyring, so the key was not saved.\n\n"
                f"Set the {env_var} environment variable before launching the "
                f"browser instead.\n\nDetail: {exc}")
            return
        finally:
            self._other_field.clear()
        self._set_active_provider(provider_id)
        self._refresh_other_section(provider_id)
        self.saved.emit()
        QMessageBox.information(self, "Configure AI Agent",
                                f"Key saved. Py is set to use {label}.")

    def _clear_other_key(self) -> None:
        from app.agent.credentials import PROVIDER_KEY_INFO

        provider_id = self._current_other_provider()
        _label, _env, account = PROVIDER_KEY_INFO[provider_id]
        self._other_store(account).clear_key()
        self._refresh_other_section(provider_id)
        self.saved.emit()
        QMessageBox.information(self, "Configure AI Agent", "Stored key removed.")

    @staticmethod
    def _other_store(account: str):
        from app.agent.keys import ApiKeyStore

        return ApiKeyStore(account=account)

    def _save_other_model(self) -> None:
        from app.agent.config import KEY_AGENT_MODEL

        model = self._selected_other_model()
        if not model:
            QMessageBox.warning(self, "Configure AI Agent", "Choose or enter a model first.")
            return
        if not self._selected_other_model_supported():
            QMessageBox.warning(
                self, "Configure AI Agent",
                "This model does not report support for tool calling, which "
                "PyBrowser's agent needs for every action. Choose a different "
                "model, or use Test Connection if you believe this is wrong.")
            return
        if self._settings is None:
            QMessageBox.warning(
                self, "Configure AI Agent",
                "Settings are unavailable, so this choice cannot be remembered.")
            return
        provider_id = self._current_other_provider()
        self._set_active_provider(provider_id)
        self._settings.set(KEY_AGENT_MODEL, model)
        self.saved.emit()
        QMessageBox.information(
            self, "Configure AI Agent",
            "Saved. Py picks this up as soon as you close this dialog, "
            "which begins a fresh conversation.")

    def _set_active_provider(self, provider_id: str) -> None:
        from app.agent.config import KEY_AGENT_PROVIDER

        if self._settings is not None:
            self._settings.set(KEY_AGENT_PROVIDER, provider_id)

    def _test_other_connection(self) -> None:
        provider_id = self._current_other_provider()
        client_class = self._other_client_class(provider_id)
        key = self._other_field.text().strip()
        if not key:
            from app.agent.credentials import resolve_for

            key = resolve_for(provider_id).secret or ""
        model = self._selected_other_model()
        self._other_result.setText("Testing…")
        ok, message = client_class.test_connection(key, model)
        prefix = "✓ " if ok else "✗ "
        color = "#2a8f4e" if ok else "#c0392b"
        self._other_result.setTextFormat(Qt.TextFormat.RichText)
        self._other_result.setText(
            f"<span style='color:{color}'>{prefix}{message}</span>")

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

    # -- which workspace a request acts in --------------------------------
    def _workspace_section(self, parent: QWidget) -> QWidget:
        """The Anthropic Workspace ID - only needed for an identity-linked key.

        Not a secret: it names which workspace a request acts in, and shows
        up in the Anthropic Console. Stored as an ordinary preference, same
        as model and effort - never in the keyring, never treated as the API
        key is.
        """
        from app.agent.config import AgentConfig

        current = AgentConfig.from_environment(self._settings)

        box = QWidget(parent)
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)

        heading = QLabel(
            "<hr><b>Anthropic Workspace ID</b><br>"
            "<span style='color:#555'>Only needed if this key is an "
            "“identity-linked” API key - Claude will say so with a "
            "400 error naming <code>anthropic-workspace-id</code> if it "
            "applies to you. Not a secret; find it in the Anthropic Console "
            "under this workspace's settings. Leave blank otherwise.</span>",
            box)
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.TextFormat.RichText)
        column.addWidget(heading)

        self.workspace_field = QLineEdit(box)
        self.workspace_field.setPlaceholderText("leave blank if not required")
        self.workspace_field.setText(current.workspace_id)
        column.addWidget(self.workspace_field)

        save = QPushButton("Save workspace ID", box)
        save.clicked.connect(self._save_workspace_id)
        column.addWidget(save)
        return box

    def _save_workspace_id(self) -> None:
        if self._settings is None:
            QMessageBox.warning(
                self, "Configure AI Agent",
                "Settings are unavailable, so this cannot be remembered. "
                "Set ANTHROPIC_WORKSPACE_ID instead.")
            return
        from app.agent.config import KEY_AGENT_WORKSPACE_ID

        self._settings.set(KEY_AGENT_WORKSPACE_ID, self.workspace_field.text().strip())
        self.saved.emit()
        QMessageBox.information(
            self, "Configure AI Agent",
            "Saved. Py picks this up as soon as you close this dialog, "
            "which begins a fresh conversation.")

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


def build_transport(credential, config):
    """The one place that knows which client class a provider needs.

    Every provider client implements the same ``send()`` contract
    (``ClaudeTransport`` in claude_client.py), so this is the entire
    provider dispatch - nothing downstream (AgentSession, ToolRegistry,
    Missions, safety.py) branches on provider at all.
    """
    from app.agent.claude_client import ClaudeClient
    from app.agent.config import PROVIDER_GROQ, PROVIDER_OPENROUTER
    from app.agent.openai_compatible import GroqClient, OpenRouterClient

    if credential.provider == PROVIDER_GROQ:
        return GroqClient(credential.secret or "", config)
    if credential.provider == PROVIDER_OPENROUTER:
        return OpenRouterClient(credential.secret or "", config)
    return ClaudeClient(credential, config)


def build_session(browser, parent=None, settings=None, missions=None):
    """Create an AgentSession if the agent can run, else return (None, reason).

    Every failure path here is soft. A missing SDK or credential must leave a
    working browser with an agent panel that explains itself, never a crash on
    startup.
    """
    try:
        from app.agent.config import AgentConfig
        from app.agent.credentials import resolve_for
        from app.agent.session import AgentSession
    except ImportError as exc:
        return None, f"the anthropic SDK is not installed ({exc})"

    config = AgentConfig.from_environment(settings)
    try:
        credential = resolve_for(config.provider)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the credential could not be read ({exc})"
    if not credential.available:
        if config.provider == "anthropic":
            return None, ("no credential is configured - sign in with `ant auth login`, "
                          "set ANTHROPIC_API_KEY, or add a key in "
                          "Tools \u2192 Configure AI Agent")
        return None, (f"no {credential.provider} API key is configured - add one in "
                      "Tools \u2192 Configure AI Agent")
    try:
        transport = build_transport(credential, config)
        return AgentSession(browser, transport, config, parent, missions=missions), ""
    except BaseException as exc:  # noqa: BLE001
        # Nothing the agent does may take the browser down with it.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, f"the agent could not start ({exc})"

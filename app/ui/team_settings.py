"""Team settings: limits, model, web search key, and the code sandbox.

Preferences live in the settings table (``team_<field>`` keys - see
app/team/limits.py). **No API key is ever stored there.** The model key is set
in Tools -> Configure AI Agent; the web-search key is saved straight to the OS
keyring (or comes from an environment variable) and is never displayed again.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from app.team import sandbox as sandbox_mod
from app.team import websearch
from app.team.limits import TeamLimits

PROVIDERS = (("groq", "Groq (default)"), ("openai", "OpenAI"), ("openrouter", "OpenRouter"), ("gemini", "Gemini"))
BACKENDS = (("auto", "Automatic (recommended)"), ("native", "Built-in sandbox only"),
            ("container", "Docker / Podman container only"))

#: (field, label, tooltip)
_FIELDS = (
    ("max_concurrency", "Agents working at once", "Keep low on a free-tier key: each agent is one request."),
    ("max_tasks", "Max tasks in a plan", "The Coordinator's plan is rejected above this."),
    ("max_model_calls", "Max model calls per run", "A hard cap, retries included. A run stops when it is reached."),
    ("max_revision_rounds", "Max review revision rounds", "0 makes the Reviewer advisory only."),
    ("max_retries", "Retries after a rate limit", "Per call. Waits honour the provider's Retry-After."),
    ("max_backoff_s", "Longest wait between retries (s)", ""),
    ("max_fetch_pages", "Web pages to read per research task", "0 = snippets only. Only public pages are opened."),
    ("fetch_timeout_s", "Page read timeout (s)", "A slow page is skipped, not waited on."),
    ("fetch_max_chars", "Max characters kept per page", "Longer pages are shortened."),
    ("task_timeout_s", "Task timeout (s)", "A task that runs longer is failed, not waited on."),
    ("check_timeout_s", "Sandbox check timeout (s)", "Per test command."),
)


class TeamSettingsDialog(QDialog):
    def __init__(self, settings, controller=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Team: limits and model")
        self.resize(460, 640)
        self._settings = settings
        self._controller = controller
        defaults = TeamLimits()
        current = TeamLimits.from_settings(settings)

        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(body)

        note = QLabel("The model API key is set in Tools → Configure AI Agent (stored in your OS keyring). "
                      "Nothing on this page stores a key in the settings database.", body)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self._inputs: dict[str, QSpinBox | QDoubleSpinBox] = {}
        for name, label, tip in _FIELDS:
            low, high = TeamLimits.BOUNDS[name]
            widget: QSpinBox | QDoubleSpinBox
            if isinstance(getattr(defaults, name), int):
                widget = QSpinBox(body)
                widget.setRange(int(low), int(high))
                widget.setValue(int(getattr(current, name)))
            else:
                widget = QDoubleSpinBox(body)
                widget.setRange(float(low), float(high))
                widget.setDecimals(0)
                widget.setValue(float(getattr(current, name)))
            widget.setToolTip(tip)
            self._inputs[name] = widget
            form.addRow(label, widget)
        self.provider = QComboBox(body)
        for ident, label in PROVIDERS:
            self.provider.addItem(label, ident)
        self.provider.setCurrentIndex(max(0, self.provider.findData(self._get("team_provider") or "groq")))
        form.addRow("Provider", self.provider)
        self.model = QLineEdit(body)
        self.model.setPlaceholderText("Same model as the AI agent for this provider")
        self.model.setText(self._get("team_model"))
        form.addRow("Model override", self.model)
        layout.addLayout(form)

        layout.addWidget(self._web_group(body))
        layout.addWidget(self._sandbox_group(body))
        layout.addStretch(1)

        reset = QPushButton("Restore defaults", body)
        reset.clicked.connect(self._reset)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------ web search
    def _web_group(self, parent: QWidget) -> QGroupBox:
        box = QGroupBox("Web search", parent)
        column = QVBoxLayout(box)
        intro = QLabel(
            "Lets the Researcher look things up on the web. Only a short search query (secrets removed) is "
            "sent to the search service you choose - never page contents, files or your mission text. "
            "Results are kept as source links, labelled as web results, separate from what you attach.", box)
        intro.setWordWrap(True)
        column.addWidget(intro)
        self.search_provider = QComboBox(box)
        self.search_provider.addItem("Off", "")
        for info in websearch.PROVIDERS.values():
            self.search_provider.addItem(info.label, info.id)
        chosen = websearch.selected_provider(self._settings)
        self.search_provider.setCurrentIndex(max(0, self.search_provider.findData(chosen)))
        self.search_provider.currentIndexChanged.connect(self._refresh_search_state)
        column.addWidget(self.search_provider)
        self.search_help = QLabel("", box)
        self.search_help.setWordWrap(True)
        self.search_help.setOpenExternalLinks(True)
        column.addWidget(self.search_help)
        self.search_key = QLineEdit(box)
        self.search_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.search_key.setPlaceholderText("Paste API key (saved to your OS keyring)")
        column.addWidget(self.search_key)
        row = QHBoxLayout()
        self.search_save = QPushButton("Save key", box)
        self.search_save.clicked.connect(self._save_search_key)
        self.search_remove = QPushButton("Remove key", box)
        self.search_remove.clicked.connect(self._remove_search_key)
        self.search_test = QPushButton("Test", box)
        self.search_test.clicked.connect(self._test_search)
        for button in (self.search_save, self.search_remove, self.search_test):
            row.addWidget(button)
        column.addLayout(row)
        self.search_status = QLabel("", box)
        self.search_status.setWordWrap(True)
        column.addWidget(self.search_status)
        self._refresh_search_state()
        return box

    def _chosen_search(self) -> str:
        return self.search_provider.currentData() or ""

    def _refresh_search_state(self) -> None:
        chosen = self._chosen_search()
        for widget in (self.search_key, self.search_save, self.search_remove, self.search_test):
            widget.setEnabled(bool(chosen))
        if not chosen:
            self.search_help.setText("Web search is off.")
            self.search_status.setText("")
            return
        info = websearch.PROVIDERS[chosen]
        self.search_help.setText(
            f"{info.credential_note} <a href='{info.signup_url}'>{info.signup_url}</a><br>"
            f"Or set the <code>{info.env_var}</code> environment variable before starting the browser.")
        env = {"PYBROWSER_SEARCH_PROVIDER": chosen}
        import os
        env.update({k: v for k, v in os.environ.items() if k != "PYBROWSER_SEARCH_PROVIDER"})
        status = websearch.resolve_search(None, env)
        self.search_status.setText(
            f"Key found ({status.detail})." if status.available else "No key saved yet.")

    def _save_search_key(self) -> None:
        chosen, key = self._chosen_search(), self.search_key.text().strip()
        if not chosen or not key:
            return
        try:
            websearch.save_search_key(chosen, key)
        except Exception as exc:  # noqa: BLE001 - keyring backends fail in many ways
            QMessageBox.warning(self, "Could not save the key",
                                f"The OS keyring is not available ({exc}). Set the "
                                f"{websearch.PROVIDERS[chosen].env_var} environment variable instead.")
            return
        self.search_key.clear()
        self._settings.set("team_search_provider", chosen)
        self._refresh_search_state()

    def _remove_search_key(self) -> None:
        chosen = self._chosen_search()
        if chosen:
            websearch.clear_search_key(chosen)
            self._refresh_search_state()

    def _test_search(self) -> None:
        if self._controller is None:
            return
        self._settings.set("team_search_provider", self._chosen_search())
        self.search_status.setText("Testing…")

        def done(ok: bool, message: str) -> None:
            self.search_status.setText(("✓ " if ok else "✗ ") + message)

        self._controller.test_search(done)

    # --------------------------------------------------------------- sandbox
    def _sandbox_group(self, parent: QWidget) -> QGroupBox:
        box = QGroupBox("Code sandbox (Tester)", parent)
        column = QVBoxLayout(box)
        intro = QLabel(
            "Generated code runs only in a verified isolated environment (no network, your files hidden, "
            "resource limits). There is no setting to run it unconfined. On Windows this needs Docker "
            "Desktop; on Linux/macOS a built-in sandbox is used when present.", box)
        intro.setWordWrap(True)
        column.addWidget(intro)
        form = QFormLayout()
        self.backend = QComboBox(box)
        for ident, label in BACKENDS:
            self.backend.addItem(label, ident)
        self.backend.setCurrentIndex(max(0, self.backend.findData(self._get("team_sandbox_backend") or "auto")))
        form.addRow("Backend", self.backend)
        self.image = QLineEdit(box)
        self.image.setPlaceholderText(sandbox_mod.DEFAULT_IMAGE)
        self.image.setText(self._get("team_container_image"))
        form.addRow("Container image", self.image)
        column.addLayout(form)
        self.sandbox_status = QLabel("", box)
        self.sandbox_status.setWordWrap(True)
        column.addWidget(self.sandbox_status)
        row = QHBoxLayout()
        check = QPushButton("Check sandbox", box)
        check.clicked.connect(self._check_sandbox)
        self.pull = QPushButton("Download image", box)
        self.pull.setToolTip("Runs 'docker pull' for the image above (needs Docker running)")
        self.pull.clicked.connect(self._pull_image)
        row.addWidget(check)
        row.addWidget(self.pull)
        column.addLayout(row)
        self._show_sandbox_status()
        return box

    def _apply_sandbox_fields(self) -> None:
        self._settings.set("team_sandbox_backend", self.backend.currentData() or "auto")
        self._settings.set("team_container_image", self.image.text().strip())

    def _show_sandbox_status(self) -> None:
        if self._controller is None:
            return
        status = self._controller.sandbox_status()
        if status.checking:
            self.sandbox_status.setText("Checking…")
        elif status.available:
            self.sandbox_status.setText(f"✓ Available: {status.mechanism}. Verified: your files hidden, "
                                        "no network.")
        else:
            self.sandbox_status.setText(f"✗ Unavailable: {status.reason}\n\n{status.setup_hint}")

    def _check_sandbox(self) -> None:
        if self._controller is None:
            return
        self._apply_sandbox_fields()
        self.sandbox_status.setText("Checking… (this runs a short self-test)")
        self._controller.recheck_sandbox()
        self._controller.environment_changed.connect(self._show_sandbox_status)

    def _pull_image(self) -> None:
        if self._controller is None:
            return
        self._apply_sandbox_fields()
        self.sandbox_status.setText("Downloading the image… (this can take a few minutes)")
        self.pull.setEnabled(False)

        def done(ok: bool, message: str) -> None:
            self.pull.setEnabled(True)
            self.sandbox_status.setText(("✓ " if ok else "✗ ") + message)
            if ok:
                self._show_sandbox_status()

        self._controller.pull_sandbox_image(done)

    # ----------------------------------------------------------------- shared
    def _get(self, key: str) -> str:
        try:
            return self._settings.get(key, "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _reset(self) -> None:
        defaults = TeamLimits()
        for name, widget in self._inputs.items():
            widget.setValue(getattr(defaults, name))
        self.provider.setCurrentIndex(0)
        self.model.clear()
        self.backend.setCurrentIndex(0)
        self.image.clear()

    def _save(self) -> None:
        for name, widget in self._inputs.items():
            self._settings.set(f"team_{name}", str(int(widget.value())))
        self._settings.set("team_provider", self.provider.currentData() or "groq")
        self._settings.set("team_model", self.model.text().strip())
        self._settings.set("team_search_provider", self._chosen_search())
        before = (self._get("team_sandbox_backend"), self._get("team_container_image"))
        self._apply_sandbox_fields()
        if self._controller is not None and before != (self._get("team_sandbox_backend"),
                                                       self._get("team_container_image")):
            self._controller.recheck_sandbox()
        self.accept()

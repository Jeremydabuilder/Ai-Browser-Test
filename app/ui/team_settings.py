"""Team settings: execution limits, provider/model, and the sandbox opt-in.

Stored as ordinary preferences (``team_<field>`` keys in the settings table -
see app/team/limits.py). The API key is NOT here: it lives in the OS keyring
via Tools -> Configure AI Agent, never in this table.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app.team.limits import TeamLimits

PROVIDERS = (("groq", "Groq (default)"), ("openai", "OpenAI"), ("openrouter", "OpenRouter"), ("gemini", "Gemini"))

#: (field, label, tooltip)
_FIELDS = (
    ("max_concurrency", "Agents working at once", "Keep low on a free-tier key: each agent is one request."),
    ("max_tasks", "Max tasks in a plan", "The Coordinator's plan is rejected above this."),
    ("max_model_calls", "Max model calls per run", "A hard cap, retries included. A run stops when it is reached."),
    ("max_revision_rounds", "Max review revision rounds", "0 makes the Reviewer advisory only."),
    ("max_retries", "Retries after a rate limit", "Per call. Waits honour the provider's Retry-After."),
    ("max_backoff_s", "Longest wait between retries (s)", ""),
    ("task_timeout_s", "Task timeout (s)", "A task that runs longer is failed, not waited on."),
    ("check_timeout_s", "Sandbox check timeout (s)", "Per test command."),
)


class TeamSettingsDialog(QDialog):
    def __init__(self, settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Team settings")
        self._settings = settings
        defaults = TeamLimits()
        current = TeamLimits.from_settings(settings)
        layout = QVBoxLayout(self)
        note = QLabel("Limits keep a mission bounded. The API key is set in Tools → Configure AI Agent; "
                      "it is never stored here.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self._inputs: dict[str, QSpinBox | QDoubleSpinBox] = {}
        for name, label, tip in _FIELDS:
            low, high = TeamLimits.BOUNDS[name]
            widget: QSpinBox | QDoubleSpinBox
            if isinstance(getattr(defaults, name), int):
                widget = QSpinBox(self)
                widget.setRange(int(low), int(high))
                widget.setValue(int(getattr(current, name)))
            else:
                widget = QDoubleSpinBox(self)
                widget.setRange(float(low), float(high))
                widget.setDecimals(0)
                widget.setValue(float(getattr(current, name)))
            widget.setToolTip(tip)
            self._inputs[name] = widget
            form.addRow(label, widget)
        self.provider = QComboBox(self)
        for ident, label in PROVIDERS:
            self.provider.addItem(label, ident)
        stored = (self._get("team_provider") or "groq")
        self.provider.setCurrentIndex(max(0, self.provider.findData(stored)))
        form.addRow("Provider", self.provider)
        self.model = QLineEdit(self)
        self.model.setPlaceholderText("Same model as the AI agent for this provider")
        self.model.setText(self._get("team_model"))
        form.addRow("Model override", self.model)
        layout.addLayout(form)
        self.unisolated = QCheckBox("Allow test runs without network isolation (not recommended)", self)
        self.unisolated.setToolTip("Generated code normally runs only where it has no network. Some systems "
                                   "(macOS without sandbox-exec, other Unixes) cannot provide that.")
        self.unisolated.setChecked(self._flag("team_allow_unisolated_execution"))
        layout.addWidget(self.unisolated)
        reset = QPushButton("Restore defaults", self)
        reset.clicked.connect(self._reset)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _get(self, key: str) -> str:
        try:
            return self._settings.get(key, "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _flag(self, key: str) -> bool:
        try:
            return bool(self._settings.get_bool(key, False))
        except Exception:  # noqa: BLE001
            return False

    def _reset(self) -> None:
        defaults = TeamLimits()
        for name, widget in self._inputs.items():
            widget.setValue(getattr(defaults, name))
        self.provider.setCurrentIndex(0)
        self.model.clear()
        self.unisolated.setChecked(False)

    def _save(self) -> None:
        for name, widget in self._inputs.items():
            self._settings.set(f"team_{name}", str(int(widget.value())))
        self._settings.set("team_provider", self.provider.currentData() or "groq")
        self._settings.set("team_model", self.model.text().strip())
        self._settings.set_bool("team_allow_unisolated_execution", self.unisolated.isChecked())
        self.accept()

"""The Team section of the side panel.

Reads one thing - ``TeamController.snapshot()`` - and renders it: nothing
here decides anything the engine decides. Built for a ~300px column: every
row wraps, tab labels elide, and no widget has a fixed width.
"""

from __future__ import annotations

import time
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPalette, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QFrame, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QTabBar, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from app.team.model import (
    AgentId, Artifact, ArtifactKind, EventKind, Mission, MissionStatus, Source, SourceKind,
    SourceStatus, TaskStatus,
)
from app.team.runner import (
    MAX_SOURCES, TeamController, collect_tab_sources, file_source, paste_source,
)
from app.team.workspace import WorkspaceError
from app.ui import theme
from app.ui.dialogs import confirm_destructive
from app.ui.flow_layout import FlowLayout

EXAMPLES = (
    ("Compare tabs", "Compare the products in these tabs and write a recommendation."),
    ("Research an idea", "Research this idea and produce a plan with sources: "),
    ("Review code", "Review this code, identify bugs, and propose fixes."),
    ("Study guide", "Turn these pages into a clear study guide."),
)

_STATUS_TEXT = {
    TaskStatus.PENDING: "waiting", TaskStatus.RUNNING: "working", TaskStatus.DONE: "done",
    TaskStatus.FAILED: "failed", TaskStatus.BLOCKED: "blocked", TaskStatus.SKIPPED: "skipped",
    TaskStatus.CANCELLED: "cancelled",
}


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _elide(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


#: A QScrollArea's viewport paints the palette's Window colour, which is a
#: different grey from the panel's own background - make both see-through.
_TRANSPARENT_SCROLL = ("QScrollArea { background: transparent; border: none; }"
                       " QScrollArea > QWidget > QWidget { background: transparent; }")


def _style_markdown(document, m) -> None:
    """Qt turns markdown headings into large fixed-size text; scale them to a
    side panel (and keep them bold) after the document is set."""
    sizes = {1: m.text_lg, 2: m.text + 1, 3: m.text, 4: m.text, 5: m.text_sm, 6: m.text_sm}
    block = document.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        if level:
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            fmt = QTextCharFormat()
            # Qt marks markdown headings with a relative size adjustment that
            # wins over any explicit size; zero it, then set a real one.
            fmt.setProperty(QTextFormat.Property.FontSizeAdjustment, 0)
            fmt.setProperty(QTextFormat.Property.FontPixelSize, sizes.get(level, m.text))
            fmt.setFontWeight(700)
            cursor.mergeCharFormat(fmt)
        block = block.next()


class _ElidedLabel(QLabel):
    """One line of plain text that shortens itself to the width it is given,
    instead of demanding the width of its text (which is what pushes a narrow
    panel wider than its column)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._fit()

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        width = max(10, self.width())
        self.setText(QFontMetrics(self.font()).elidedText(
            " ".join(self._full.split()), Qt.TextElideMode.ElideRight, width))


class TeamPanel(QWidget):
    configure_requested = Signal()

    def __init__(self, controller: TeamController, browser=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._browser = browser
        self._sources: list[Source] = []
        self._workspace_path = ""
        self._reading_tabs = False
        self._shown_mission: Mission | None = None
        self._last_event_seq = 0
        self._viewer_ids: list[str] = []
        self._announced: tuple | None = None
        self._test_note = ""
        self._test_ok = True
        self._web_available = False
        self._viewer_mission = -1
        self._provider_ready = True
        m = theme.METRICS
        self._m = m
        self._c = theme.palette_for(QApplication.instance())
        c = self._c

        # Everything lives in one vertical scroll area: when the window is short
        # the panel scrolls instead of letting rows draw over each other.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_TRANSPARENT_SCROLL)
        body = QWidget(self._scroll)
        body.setAutoFillBackground(False)
        self._scroll.setWidget(body)
        outer.addWidget(self._scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, m.space_1, 0)
        layout.setSpacing(m.space_2)

        # Honest, compact environment: model on line one, capabilities on line two.
        self.env = QLabel(body)
        self.env.setWordWrap(True)
        self.env.setTextFormat(Qt.TextFormat.RichText)
        self.env.setOpenExternalLinks(False)
        self.env.setAccessibleName("Team environment")
        self.env.linkActivated.connect(self._on_env_link)
        self._theme_links(self.env)
        layout.addWidget(self.env)
        self.configure_button = QPushButton("Configure AI Agent…", body)
        self.configure_button.setProperty("kind", "chip")
        self.configure_button.clicked.connect(self.configure_requested.emit)
        layout.addWidget(self.configure_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.top_tabs = QTabBar(body)
        self.top_tabs.addTab("Mission")
        self.top_tabs.addTab("History")
        self.top_tabs.setExpanding(True)
        self.top_tabs.setDrawBase(False)
        self.top_tabs.setStyleSheet("QTabBar::tab { min-width: 0px; padding: 4px 8px; }")
        self.top_tabs.currentChanged.connect(self._on_top_tab)
        layout.addWidget(self.top_tabs)

        self.pages = QStackedWidget(body)
        layout.addWidget(self.pages, 1)
        self.pages.addWidget(self._build_compose())
        self.pages.addWidget(self._build_run())
        self.pages.addWidget(self._build_history())

        controller.changed.connect(self.refresh)
        controller.history_changed.connect(self._refresh_history)
        controller.environment_changed.connect(self._refresh_env)
        self._refresh_env()
        self._refresh_history()
        self.refresh()

    def _link_css(self) -> str:
        return f"<style>a {{ color: {self._c.accent}; text-decoration: none; }}</style>"

    def _theme_links(self, label: QLabel) -> None:
        palette = label.palette()
        palette.setColor(QPalette.ColorRole.Link, QColor(self._c.accent))
        palette.setColor(QPalette.ColorRole.LinkVisited, QColor(self._c.accent))
        label.setPalette(palette)

    # ------------------------------------------------------------------ build
    def _build_compose(self) -> QWidget:
        m, c = self._m, self._c
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(m.space_2)

        title = QLabel("Give the team a mission", page)
        title.setStyleSheet(f"color:{c.text}; font-size:{m.text_lg}px; font-weight:700;")
        box.addWidget(title)
        hint = QLabel("A Coordinator plans it and brings in only the specialists it needs. "
                      "Web pages are treated as untrusted data.", page)
        hint.setWordWrap(True)
        hint.setProperty("kind", "muted")
        box.addWidget(hint)

        self.goal = QPlainTextEdit(page)
        self.goal.setPlaceholderText("What should the team do? e.g. “Compare the products in these tabs "
                                     "and write a recommendation.”")
        self.goal.setAccessibleName("Mission")
        self.goal.setMinimumHeight(84)
        self.goal.setMaximumHeight(140)
        self.goal.textChanged.connect(self._sync_start)
        box.addWidget(self.goal)

        examples = FlowLayout(spacing=m.space_1)
        for label, text in EXAMPLES:
            button = QPushButton(label, page)
            button.setProperty("kind", "chip")
            button.setToolTip(text)
            button.clicked.connect(lambda _c=False, t=text: self._use_example(t))
            examples.addWidget(button)
        box.addLayout(examples)

        attach = FlowLayout(spacing=m.space_1)
        self.tabs_button = QPushButton("Add tabs…", page)
        self.tabs_button.setToolTip("Choose open tabs for the team to read")
        self.tabs_button.clicked.connect(self._pick_tabs)
        self.paste_button = QPushButton("Add text…", page)
        self.paste_button.clicked.connect(self._add_pasted)
        self.files_button = QPushButton("Add files…", page)
        self.files_button.setToolTip("Text, Markdown, JSON, CSV, Word and PDF files")
        self.files_button.clicked.connect(self._add_files)
        self.workspace_button = QPushButton("Workspace…", page)
        self.workspace_button.setToolTip("Authorize a project folder the Coder may read. "
                                         "Nothing is written to it unless you press Apply.")
        self.workspace_button.clicked.connect(self._pick_workspace)
        for button in (self.tabs_button, self.paste_button, self.files_button, self.workspace_button):
            button.setProperty("kind", "chip")
            attach.addWidget(button)
        box.addLayout(attach)

        self.workspace_label = QLabel("", page)
        self.workspace_label.setWordWrap(True)
        self.workspace_label.setProperty("kind", "muted")
        self.workspace_label.hide()
        box.addWidget(self.workspace_label)

        self.included = QListWidget(page)
        self.included.setAccessibleName("Included pages and files")
        self.included.setWordWrap(True)
        self.included.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.included.customContextMenuRequested.connect(self._included_menu)
        self.included.setMaximumHeight(150)
        self.included.setProperty("kind", "flat")
        self.included.hide()
        box.addWidget(self.included)
        self.included_note = QLabel("", page)
        self.included_note.setWordWrap(True)
        self.included_note.setProperty("kind", "muted")
        self.included_note.hide()
        box.addWidget(self.included_note)

        self.compose_error = QLabel("", page)
        self.compose_error.setWordWrap(True)
        self.compose_error.setStyleSheet(
            f"color:{c.warning_text}; background:{c.warning_soft}; border-radius:{m.radius_sm}px;"
            f" padding:{m.space_2}px; font-size:{m.text_xs}px;")
        self.compose_error.hide()
        box.addWidget(self.compose_error)

        self.next_step = QLabel("", page)
        self.next_step.setWordWrap(True)
        self.next_step.setProperty("kind", "muted")
        self.next_step.setAccessibleName("Next step")
        box.addWidget(self.next_step)

        self.web_check = QCheckBox("Search the web", page)
        self.web_check.setChecked(True)
        self.web_check.toggled.connect(lambda _on: self._sync_start())
        box.addWidget(self.web_check)

        self.start_button = QPushButton("Start team", page)
        self.start_button.setProperty("kind", "primary")
        self.start_button.clicked.connect(self._start)
        box.addWidget(self.start_button)
        self.settings_button = QPushButton("Limits and model\u2026", page)
        self.settings_button.setProperty("kind", "quiet")
        self.settings_button.clicked.connect(self._open_settings)
        box.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignLeft)
        box.addStretch(1)
        return page

    def _build_run(self) -> QWidget:
        m, c = self._m, self._c
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(m.space_2)

        head = QHBoxLayout()
        head.setSpacing(m.space_1)
        self.run_goal = _ElidedLabel(page)
        # The goal is free text (and may quote a web page): never interpret it as markup.
        self.run_goal.setStyleSheet(f"color:{c.text}; font-weight:600;")
        head.addWidget(self.run_goal, 1)
        self.cancel_button = QPushButton("Cancel", page)
        self.cancel_button.setProperty("kind", "danger")
        self.cancel_button.clicked.connect(self._controller.cancel)
        self.retry_button = QPushButton("Retry", page)
        self.retry_button.setProperty("kind", "chip")
        self.retry_button.setToolTip("Continue from where it stopped; finished work is kept")
        self.retry_button.clicked.connect(self._retry)
        self.new_button = QPushButton("New", page)
        self.new_button.setProperty("kind", "chip")
        self.new_button.setToolTip("Start another mission")
        self.new_button.clicked.connect(self._new_mission)
        for button in (self.cancel_button, self.retry_button, self.new_button):
            head.addWidget(button)
        box.addLayout(head)
        self.run_status = QLabel("", page)
        self.run_status.setWordWrap(True)
        self.run_status.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.run_status)
        self.progress = QProgressBar(page)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setAccessibleName("Mission progress")
        box.addWidget(self.progress)

        self.run_tabs = QTabWidget(page)
        # Four short labels must fit a 300px column: the global tab style has a
        # wide minimum meant for browser tabs, so relax it for these.
        self.run_tabs.setUsesScrollButtons(False)
        self.run_tabs.tabBar().setExpanding(True)
        self.run_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.run_tabs.setStyleSheet("QTabBar::tab { min-width: 0px; padding: 4px 6px; }"
                                    " QTabWidget::pane { background: transparent; border: none; }")
        self.run_tabs.setMinimumHeight(300)
        self.run_tabs.addTab(self._scroll_into("agents"), "Agents")
        self.run_tabs.addTab(self._scroll_into("tasks"), "Tasks")
        activity = QTextBrowser(page)
        activity.setProperty("kind", "flat")
        activity.setOpenLinks(False)
        activity.setAccessibleName("Live activity")
        self.activity = activity
        self.run_tabs.addTab(activity, "Activity")
        self.run_tabs.addTab(self._build_results(), "Results")
        box.addWidget(self.run_tabs, 1)
        return page

    def _scroll_into(self, name: str) -> QScrollArea:
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet(_TRANSPARENT_SCROLL)
        holder = QWidget(area)
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, self._m.space_1, 0, 0)
        inner.setSpacing(self._m.space_1)
        inner.addStretch(1)
        area.setWidget(holder)
        setattr(self, f"_{name}_box", inner)
        setattr(self, f"_{name}_holder", holder)
        return area

    def _build_results(self) -> QWidget:
        m, c = self._m, self._c
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, m.space_1, 0, 0)
        box.setSpacing(m.space_1)
        self.viewer_choice = QComboBox(page)
        self.viewer_choice.setAccessibleName("Choose what to show")
        self.viewer_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.viewer_choice.setMinimumContentsLength(12)
        self.viewer_choice.currentIndexChanged.connect(self._show_selected)
        box.addWidget(self.viewer_choice)
        self.saved_note = QLabel("", page)
        self.saved_note.setWordWrap(True)
        self.saved_note.setStyleSheet(f"color:{c.success}; font-size:{m.text_xs}px;")
        self.saved_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.saved_note.hide()
        box.addWidget(self.saved_note)
        self.viewer = QTextBrowser(page)
        self.viewer.setProperty("kind", "flat")
        self.viewer.setOpenExternalLinks(False)
        self.viewer.setOpenLinks(False)
        self.viewer.setAccessibleName("Result")
        self.viewer.setMinimumHeight(150)
        # Markdown headings default to page-sized type; scale them to a side panel.
        self.viewer.document().setDefaultStyleSheet(
            f"h1 {{ font-size: {m.text_lg}px; }} h2 {{ font-size: {m.text + 1}px; }} "
            f"h3, h4 {{ font-size: {m.text}px; }} body, p, li, td {{ font-size: {m.text_sm}px; }} "
            f"pre, code {{ font-size: {m.text_xs}px; }}")
        self.viewer.anchorClicked.connect(self._open_link)
        box.addWidget(self.viewer, 1)

        # Wraps rather than forcing the panel wider than a narrow column.
        actions = FlowLayout(spacing=m.space_1)
        self.save_button = QPushButton("Save to Downloads", page)
        self.save_button.setProperty("kind", "chip")
        self.save_button.setToolTip("Saves into your Downloads folder and lists it in the Downloads window (Ctrl+J)")
        self.save_button.clicked.connect(self._save_current)
        self.copy_button = QPushButton("Copy", page)
        self.copy_button.setProperty("kind", "chip")
        self.copy_button.clicked.connect(self._copy_current)
        self.more_button = QPushButton("More", page)
        self.more_button.setProperty("kind", "chip")
        menu = QMenu(self.more_button)
        self.save_as_action = menu.addAction("Save as\u2026")
        self.save_as_action.triggered.connect(self._save_current_as)
        self.save_files_action = menu.addAction("Save all files to Downloads")
        self.save_files_action.triggered.connect(self._save_all_files)
        self.more_button.setMenu(menu)
        self._more_menu = menu
        for button in (self.save_button, self.copy_button, self.more_button):
            actions.addWidget(button)
        box.addLayout(actions)
        self.apply_button = QPushButton("Apply to workspace\u2026", page)
        self.apply_button.setProperty("kind", "primary")
        self.apply_button.setToolTip("Shows each change first; nothing is written until you confirm")
        self.apply_button.clicked.connect(self._apply_to_workspace)
        box.addWidget(self.apply_button)
        return page

    def _build_history(self) -> QWidget:
        m = self._m
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(m.space_2)
        self.history_list = QListWidget(page)
        self.history_list.setMinimumHeight(200)
        self.history_list.setStyleSheet(
            f"QListWidget::item {{ padding: 6px 4px; border-bottom: 1px solid {self._c.line}; }}")
        self.history_list.setWordWrap(True)
        self.history_list.setAccessibleName("Saved missions")
        self.history_list.itemDoubleClicked.connect(lambda _i: self._open_history())
        box.addWidget(self.history_list, 1)
        self.history_empty = QLabel("No saved missions yet.", page)
        self.history_empty.setProperty("kind", "muted")
        box.addWidget(self.history_empty)
        row = FlowLayout(spacing=m.space_1)
        self.open_button = QPushButton("Open", page)
        self.open_button.setProperty("kind", "chip")
        self.open_button.clicked.connect(self._open_history)
        self.delete_button = QPushButton("Delete…", page)
        self.delete_button.setProperty("kind", "danger")
        self.delete_button.clicked.connect(self._delete_history)
        row.addWidget(self.open_button)
        row.addWidget(self.delete_button)
        box.addLayout(row)
        return page

    # ------------------------------------------------------------ environment
    def _refresh_env(self) -> None:
        c = self._c
        status = self._controller.provider_status()
        sandbox = self._controller.sandbox_status()
        web = self._controller.web_status()
        knowledge = self._controller._knowledge
        muted = c.muted
        if status.available:
            first = (f"<span style='color:{c.success}'>\u25cf</span> <b>{escape(status.label)}</b> "
                     f"\u00b7 {escape(status.model)} \u00b7 <a href='test'>Test</a> \u00b7 "
                     "<a href='settings'>Settings</a>")
            self.configure_button.hide()
        else:
            first = (f"<span style='color:{c.danger}'>\u25cf</span> <b>{escape(status.label)}</b> not set up "
                     "\u00b7 <a href='settings'>Settings</a>")
            self.configure_button.setText(f"Set up {status.label}\u2026")
            self.configure_button.show()
        lines = [first]
        if not status.available:
            lines.append(f"<span style='color:{muted}'>Add your {escape(status.label)} key to start.</span>")
        if self._test_note:
            tone = c.success if self._test_ok else c.danger
            lines.append(f"<span style='color:{tone}'>{escape(self._test_note)}</span>")
        if sandbox.checking:
            box = "Sandbox: checking\u2026"
        elif sandbox.available:
            box = f"Sandbox: {escape(sandbox.backend)}"
        else:
            box = ("<span style='color:%s'>Sandbox: unavailable</span> (<a href='sandbox-setup'>how to enable</a>)"
                   % c.warning)
        known = "on" if knowledge is not None and getattr(knowledge, "enabled", False) else "off"
        webtext = f"Web: {escape(web.label)}" if web.available else "Web: off"
        lines.append(f"<span style='color:{muted}'>{webtext} \u00b7 Knowledge: {known} \u00b7 </span>"
                     f"<span style='color:{muted}'>{box}</span>")
        self.env.setText(self._link_css() + "<br>".join(lines))
        self.env.setToolTip(f"{status.detail}\n\nExecution: {sandbox.summary()}")
        self._provider_ready = status.available
        if status.available:
            self.compose_error.hide()        # the warning was about a key that now exists
        became_available = web.available and not self._web_available
        self._web_available = web.available
        self.web_check.setEnabled(web.available)
        if became_available:
            self.web_check.setChecked(True)   # configuring a provider is the opt-in; untick per mission
        if web.available:
            self.web_check.setText(f"Search the web ({web.label})")
            self.web_check.setToolTip(f"Short search queries (secrets removed) are sent to {web.label}. "
                                      "Results are labelled as web results, separate from your sources.")
        else:
            self.web_check.setChecked(False)
            self.web_check.setText("Search the web (not set up)")
            self.web_check.setToolTip(web.detail)
        self._sync_start()

    def _on_env_link(self, link: str) -> None:
        if link == "test":
            self._run_connection_test()
        elif link == "settings":
            self._open_settings()
        elif link == "sandbox-setup":
            sandbox = self._controller.sandbox_status()
            QMessageBox.information(
                self, "Code sandbox", (sandbox.reason + "\n\n" if sandbox.reason else "")
                + (sandbox.setup_hint or "No setup is needed.") +
                "\n\nGenerated code is only ever run in a verified isolated environment; "
                "there is no unrestricted fallback.")

    def refresh_environment(self) -> None:
        """Called after the key dialog closes: re-read the key and, if there
        is one now, prove it works with a tiny real request."""
        self._test_note = ""
        self._refresh_env()
        if self._controller.provider_status().available:
            self._run_connection_test()

    def _run_connection_test(self) -> None:
        self._test_note, self._test_ok = "Testing the connection\u2026", True
        self._refresh_env()

        def done(ok: bool, message: str) -> None:
            self._test_note, self._test_ok = message, ok
            self._refresh_env()

        self._controller.test_provider(done)

    # ------------------------------------------------------------- compose
    def _open_settings(self) -> None:
        settings = self._controller.settings
        if settings is None:
            return
        from app.ui.team_settings import TeamSettingsDialog

        dialog = TeamSettingsDialog(settings, self._controller, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_env()

    def _use_example(self, text: str) -> None:
        self.goal.setPlainText(text)
        self.goal.setFocus()
        cursor = self.goal.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.goal.setTextCursor(cursor)

    def _sync_start(self) -> None:
        running = self._controller.is_running
        ok = bool(self.goal.toPlainText().strip()) and not running and not self._reading_tabs
        self.start_button.setEnabled(ok)
        if running:
            self.start_button.setToolTip("A mission is already running")
        elif not getattr(self, "_provider_ready", True):
            self.start_button.setToolTip("No model key is configured yet - see the message above")
        else:
            self.start_button.setToolTip("")
        self.next_step.setText(self._next_step_text(running))

    def _next_step_text(self, running: bool) -> str:
        if running:
            return "A mission is running - open the Mission view to follow it."
        if not getattr(self, "_provider_ready", True):
            return "Next: add your model key (Set up\u2026 above). It stays in your system keyring."
        if not self.goal.toPlainText().strip():
            return ("Next: describe the mission, or pick an example. Optional: add open tabs, text or files "
                    "for the team to read.")
        extras = []
        if not self._sources:
            extras.append("no pages attached" + (" - the team will search the web" if self._web_available
                                                 and self.web_check.isChecked() else ""))
        limits = self._controller.limits()
        return ("Ready. " + ("; ".join(extras) + ". " if extras else "")
                + f"A run uses at most {limits.max_model_calls} model calls and you can cancel at any time.")

    def _pick_tabs(self) -> None:
        if self._browser is None:
            return
        tabs = [t for t in self._browser.list_tabs()]
        taken = {s.url for s in self._sources if s.kind == SourceKind.TAB}
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose tabs for the team")
        box = QVBoxLayout(dialog)
        note = QLabel("The team reads the text of the tabs you tick. Pages that cannot be read "
                      "are listed as not included.", dialog)
        note.setWordWrap(True)
        box.addWidget(note)
        listing = QListWidget(dialog)
        for tab in tabs:
            item = QListWidgetItem(_elide(tab.get("title") or tab.get("url") or "Tab", 70))
            item.setToolTip(str(tab.get("url", "")))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if tab.get("url") in taken else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, tab["tab_id"])
            listing.addItem(item)
        box.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        self._tab_dialog = dialog
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = [listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())
                  if listing.item(i).checkState() == Qt.CheckState.Checked]
        self._add_tab_ids(chosen)

    def _add_tab_ids(self, tab_ids: list[int]) -> None:
        room = MAX_SOURCES - len(self._sources)
        tab_ids = tab_ids[: max(0, room)]
        if not tab_ids:
            return
        self._reading_tabs = True
        self._sync_start()

        def done(new_sources: list[Source]) -> None:
            self._reading_tabs = False
            existing = {s.url for s in self._sources if s.kind == SourceKind.TAB}
            for source in new_sources:
                if source.url and source.url in existing:
                    continue
                self._sources.append(source)
            self._refresh_included()
            self._sync_start()

        collect_tab_sources(self._browser, tab_ids, self._sources, done)

    def _add_pasted(self) -> None:
        if len(self._sources) >= MAX_SOURCES:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Add text", "Paste text or code for the team to use:")
        if ok and text.strip():
            self._sources.append(paste_source(self._sources, text, "Pasted text"))
            self._refresh_included()

    def _add_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Add files", "", "Documents (*.txt *.md *.markdown *.json *.csv *.docx *.pdf);;All files (*)")
        for path in paths:
            if len(self._sources) >= MAX_SOURCES:
                break
            self._sources.append(file_source(self._sources, path))
        if paths:
            self._refresh_included()

    def _pick_workspace(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Authorize a project folder for the Coder")
        if path:
            self._workspace_path = path
            self.workspace_label.setText(
                f"Workspace: {path}\nThe Coder can read it and propose changes. Nothing is written "
                "until you press Apply and confirm.")
            self.workspace_label.show()

    def _refresh_included(self) -> None:
        c = self._c
        self.included.clear()
        for source in self._sources:
            if source.usable:
                mark = "✓"
                detail = "truncated to fit" if source.status == SourceStatus.TRUNCATED else f"{len(source.text):,} chars"
            else:
                mark = "✗"
                detail = f"not included — {source.error or 'no readable text'}"
            item = QListWidgetItem(f"{mark} [{source.id}] {_elide(source.title, 60)}\n    {detail}")
            if not source.usable:
                item.setForeground(Qt.GlobalColor.darkYellow)
            item.setToolTip(source.url or source.title)
            item.setData(Qt.ItemDataRole.UserRole, source.id)
            self.included.addItem(item)
        self.included.setVisible(bool(self._sources))
        bad = [s for s in self._sources if not s.usable]
        self.included_note.setVisible(bool(self._sources))
        self.included_note.setText(
            (f"{len(bad)} page(s) could not be read and will NOT be sent to the team. " if bad else "")
            + "Right-click to remove. Up to %d sources." % MAX_SOURCES)

    def _included_menu(self, pos) -> None:
        item = self.included.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        remove = menu.addAction("Remove")
        if menu.exec(self.included.mapToGlobal(pos)) is remove:
            source_id = item.data(Qt.ItemDataRole.UserRole)
            self._sources = [s for s in self._sources if s.id != source_id]
            self._refresh_included()

    def _start(self) -> None:
        goal = self.goal.toPlainText().strip()
        if not goal:
            return
        self.compose_error.hide()
        status = self._controller.provider_status()
        if not status.available:
            self._refresh_env()
            self.compose_error.setText(status.detail)
            self.compose_error.show()
            return
        mission = self._controller.start(goal, list(self._sources), self._workspace_path,
                                         web_search=self._web_available and self.web_check.isChecked())
        if mission is None:
            return
        self._sources = []
        self._workspace_path = ""
        self.workspace_label.hide()
        self.goal.clear()
        self._refresh_included()
        self._last_event_seq = 0
        self._shown_mission = None

    # ------------------------------------------------------------- run view
    def _retry(self) -> None:
        mission = self._controller.snapshot()
        more = (mission is not None and mission.status == MissionStatus.COMPLETED_WITH_ISSUES
                and bool(mission.unresolved_issues)
                and not any(t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED) for t in mission.tasks))
        self._controller.retry(extra_round=more)

    def _on_task_link(self, link: str) -> None:
        action, _, task_id = link.partition(":")
        if action in ("retry", "skip") and not self._controller.is_running:
            self._controller.retry(task_id, skip=action == "skip")

    def _retry_label(self, mission: Mission) -> tuple[str, str]:
        failed = [t for t in mission.tasks if t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)]
        if mission.status == MissionStatus.COMPLETED_WITH_ISSUES:
            if failed:
                return "Retry failed", "Run only the tasks that failed; finished work is kept"
            return "Revise again", "Give the team one more review round to fix the open issues"
        if mission.status == MissionStatus.FAILED and not mission.tasks:
            return "Try again", "Plan the mission again"
        if mission.status in (MissionStatus.INTERRUPTED, MissionStatus.CANCELLED):
            return "Resume", "Continue from where it stopped; finished work is kept"
        return "Retry failed", "Run only the tasks that failed; finished work is kept"

    def _new_mission(self) -> None:
        self._controller.new_mission()

    def _on_top_tab(self, index: int) -> None:
        self.refresh()

    def refresh(self) -> None:
        mission = self._controller.snapshot()
        index = self.top_tabs.currentIndex()
        if index == 1:
            self.pages.setCurrentIndex(2)
            self._refresh_history()
        elif mission is None:
            self.pages.setCurrentIndex(0)
        else:
            self.pages.setCurrentIndex(1)
        self._sync_start()
        if mission is None:
            return
        self._render_run(mission)

    def _render_run(self, mission: Mission) -> None:
        c = self._c
        active = mission.status in MissionStatus.ACTIVE
        self.run_goal.set_full_text(mission.goal)
        colour = {MissionStatus.COMPLETED: c.success, MissionStatus.FAILED: c.danger,
                  MissionStatus.COMPLETED_WITH_ISSUES: c.warning}.get(mission.status, c.accent)
        label = MissionStatus.LABELS.get(mission.status, mission.status)
        done = sum(1 for t in mission.tasks if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED))
        progress = f" · {done}/{len(mission.tasks)} tasks" if mission.tasks else ""
        usage = f" · {mission.model_calls} model calls" if mission.model_calls else ""
        model = f" · {escape(mission.model_label)}" if mission.model_label else ""
        text = f"<span style='color:{colour}'>●</span> <b>{label}</b>{progress}{usage}{model}"
        if mission.error:
            text += f"<br><span style='color:{c.danger}'>{escape(mission.error)}</span>"
        elif mission.coordinator_note:
            text += f"<br><span style='color:{c.muted}'>{escape(mission.coordinator_note)}…</span>"
        if active and mission.throttled:
            text += (f"<br><span style='color:{c.warning}'>The provider is rate-limiting, so the team is "
                     "working one task at a time.</span>")
        waiting = [t for t in mission.tasks if t.status == TaskStatus.PENDING and t.not_before > time.time()]
        if active and waiting:
            seconds = int(max(1, max(t.not_before for t in waiting) - time.time()))
            text += (f"<br><span style='color:{c.warning}'>Waiting about {seconds}s for the rate limit to "
                     f"clear before {waiting[0].id}.</span>")
        if not active and mission.status in (MissionStatus.INTERRUPTED, MissionStatus.CANCELLED,
                                             MissionStatus.FAILED) and mission.tasks:
            kept = sum(1 for t in mission.tasks if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED))
            word = "was interrupted" if mission.status == MissionStatus.INTERRUPTED else "stopped"
            text += (f"<br><span style='color:{c.muted}'>This mission {word}. {kept} finished task(s) are kept - "
                     "pressing the button continues without repeating them.</span>")
        if not active and mission.status == MissionStatus.COMPLETED_WITH_ISSUES and mission.unresolved_issues:
            text += (f"<br><span style='color:{c.warning}'>{len(mission.unresolved_issues)} review issue(s) "
                     "are still open - see the Final result.</span>")
        self.run_status.setText(self._link_css() + text)
        self.progress.setRange(0, max(1, len(mission.tasks)))
        self.progress.setValue(done if mission.tasks else 0)
        self.progress.setVisible(bool(mission.tasks))
        self.cancel_button.setVisible(active)
        actionable = mission.status != MissionStatus.COMPLETED_WITH_ISSUES or bool(
            mission.unresolved_issues or any(t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
                                             for t in mission.tasks))
        self.retry_button.setVisible(mission.status in (
            MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.INTERRUPTED,
            MissionStatus.COMPLETED_WITH_ISSUES) and actionable and not self._controller.is_running)
        label, tip = self._retry_label(mission)
        self.retry_button.setText(label)
        self.retry_button.setToolTip(tip)
        self.new_button.setVisible(not active)
        self._render_agents(mission)
        self._render_tasks(mission)
        self._render_activity(mission)
        self._render_results(mission)
        self._shown_mission = mission

    # agents
    def _clear_box(self, box: QVBoxLayout) -> None:
        while box.count() > 1:
            item = box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _card(self, html: str, tint: str | None = None) -> QLabel:
        m, c = self._m, self._c
        label = QLabel(html)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        label.setStyleSheet(
            f"background:{tint or c.surface}; border:1px solid {c.line}; border-radius:{m.radius_md}px;"
            f" padding:{m.space_2}px; color:{c.text}; font-size:{m.text_sm}px;")
        return label

    def _agent_state(self, mission: Mission, agent: str) -> tuple[str, str, str]:
        """(colour, status word, assignment line) from what is actually happening."""
        c = self._c
        if agent == AgentId.COORDINATOR:
            if mission.status == MissionStatus.PLANNING or (
                    mission.coordinator_note and mission.status in MissionStatus.ACTIVE):
                return c.accent, "working", mission.coordinator_note or "Planning the mission"
            if mission.final_artifact_id:
                return c.success, "done", "Final result assembled"
            if mission.status == MissionStatus.FAILED and not mission.tasks:
                return c.danger, "failed", mission.error
            if mission.tasks and mission.status in MissionStatus.ACTIVE:
                return c.muted, "overseeing", f"{len(mission.tasks)} task(s) planned"
            return c.muted, "idle", ""
        mine = [t for t in mission.tasks if t.agent == agent]
        if not mine:
            return c.disabled, "not needed", "Not part of this mission" if mission.tasks else ""
        running = [t for t in mine if t.status == TaskStatus.RUNNING]
        if running:
            return c.accent, "working", f"{running[0].id}: {running[0].title}"
        failed = [t for t in mine if t.status == TaskStatus.FAILED]
        if failed:
            return c.danger, "failed", f"{failed[0].id}: {failed[0].error or 'failed'}"
        waiting = [t for t in mine if t.status == TaskStatus.PENDING]
        if waiting:
            return c.warning, "waiting", f"Next: {waiting[0].id} {waiting[0].title}"
        blocked = [t for t in mine if t.status == TaskStatus.BLOCKED]
        if blocked:
            return c.warning, "blocked", blocked[0].error
        done = [t for t in mine if t.status == TaskStatus.DONE]
        skipped = [t for t in mine if t.status == TaskStatus.SKIPPED]
        if skipped and not done:
            return c.warning, "skipped", skipped[0].error
        if mine and all(t.status == TaskStatus.CANCELLED for t in mine):
            return c.muted, "cancelled", ""
        return c.success, "done", f"{len(done)} task(s) finished"

    def _render_agents(self, mission: Mission) -> None:
        box = self._agents_box
        self._clear_box(box)
        for agent in AgentId.ALL:
            colour, word, assignment = self._agent_state(mission, agent)
            html = (f"<span style='color:{colour}'>●</span> <b>{AgentId.LABELS[agent]}</b> "
                    f"<span style='color:{self._c.muted}'>· {word}</span><br>"
                    f"<span style='color:{self._c.muted}'>{escape(AgentId.BLURBS[agent])}</span>")
            if assignment:
                html += f"<br>{escape(_elide(assignment, 160))}"
            box.insertWidget(box.count() - 1, self._card(html))

    def _render_tasks(self, mission: Mission) -> None:
        box = self._tasks_box
        self._clear_box(box)
        if not mission.tasks:
            box.insertWidget(0, self._card(f"<span style='color:{self._c.muted}'>"
                                           "The Coordinator has not produced a plan yet.</span>"))
            return
        palette = {TaskStatus.DONE: self._c.success, TaskStatus.RUNNING: self._c.accent,
                   TaskStatus.FAILED: self._c.danger, TaskStatus.BLOCKED: self._c.warning,
                   TaskStatus.PENDING: self._c.muted, TaskStatus.SKIPPED: self._c.warning,
                   TaskStatus.CANCELLED: self._c.muted}
        for task in mission.tasks:
            deps = f"after {', '.join(task.depends_on)}" if task.depends_on else "no dependencies"
            rev = " · revision" if task.revision_of else ""
            html = (f"<span style='color:{palette[task.status]}'>●</span> <b>{task.id}</b> "
                    f"{escape(_elide(task.title, 70))}<br>"
                    f"<span style='color:{self._c.muted}'>{AgentId.LABELS[task.agent]} · "
                    f"{_STATUS_TEXT[task.status]}{rev} · {deps}</span>")
            if task.outputs:
                html += f"<br><span style='color:{self._c.muted}'>produced {', '.join(task.outputs)}</span>"
            if task.inputs:
                html += f"<br><span style='color:{self._c.muted}'>received {', '.join(task.inputs)}</span>"
            if task.error:
                tone = self._c.warning if task.status in (TaskStatus.SKIPPED, TaskStatus.BLOCKED) else self._c.danger
                html += f"<br><span style='color:{tone}'>{escape(_elide(task.error, 200))}</span>"
            elif task.summary and task.status == TaskStatus.DONE:
                html += f"<br>{escape(_elide(task.summary, 140))}"
            if task.handoff and task.status == TaskStatus.DONE:
                html += (f"<br><span style='color:{self._c.muted}'>handoff: "
                         f"{escape(_elide(task.handoff, 200))}</span>")
            if task.status == TaskStatus.PENDING and task.not_before > time.time():
                html += (f"<br><span style='color:{self._c.warning}'>waiting for the rate limit "
                         f"(about {int(task.not_before - time.time()) + 1}s)</span>")
            if task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED) and not self._controller.is_running:
                html += (f"<br><a href='retry:{task.id}'>Retry this task</a>"
                         + (f" \u00b7 <a href='skip:{task.id}'>Skip it</a>" if task.status == TaskStatus.FAILED else ""))
            label = self._card(self._link_css() + html)
            label.setOpenExternalLinks(False)
            label.linkActivated.connect(self._on_task_link)
            self._theme_links(label)
            if task.acceptance:
                label.setToolTip("Acceptance criteria:\n- " + "\n- ".join(task.acceptance))
            box.insertWidget(box.count() - 1, label)

    def _render_activity(self, mission: Mission) -> None:
        c = self._c
        same = self._shown_mission is not None and self._shown_mission.id == mission.id
        if not same:
            self.activity.clear()
            self._last_event_seq = 0
        bar = self.activity.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        colours = {EventKind.ERROR: c.danger, EventKind.WARNING: c.warning, EventKind.TOOL: c.accent,
                   EventKind.REVIEW: c.accent2, EventKind.HANDOFF: c.success}
        for event in mission.events:
            if event.seq <= self._last_event_seq:
                continue
            colour = colours.get(event.kind, c.muted)
            who = AgentId.LABELS.get(event.agent, event.agent)
            self.activity.append(
                f"<span style='color:{c.disabled}'>{_fmt_time(event.ts)}</span> "
                f"<b style='color:{colour}'>{who}</b> {escape(event.text)}")
            self._last_event_seq = event.seq
        if at_bottom:
            bar.setValue(bar.maximum())

    # results
    def _render_results(self, mission: Mission) -> None:
        entries: list[tuple[str, str]] = []
        if mission.final_artifact_id:
            entries.append(("Final result", mission.final_artifact_id))
        entries.append(("Sources and pages", "__sources__"))
        for artifact in mission.artifacts:
            if artifact.id == mission.final_artifact_id:
                continue
            who = AgentId.LABELS.get(artifact.agent, artifact.agent)
            version = f" v{artifact.version}" if artifact.version > 1 else ""
            entries.append((f"{artifact.id} · {_elide(artifact.title, 28)}{version} · {who}", artifact.id))
        ids = [e[1] for e in entries]
        previous = self.viewer_choice.currentData()
        switched = mission.id != self._viewer_mission
        final_arrived = bool(mission.final_artifact_id) and mission.final_artifact_id not in self._viewer_ids
        if ids != self._viewer_ids:
            self.viewer_choice.blockSignals(True)
            self.viewer_choice.clear()
            for text, key in entries:
                self.viewer_choice.addItem(text, key)
            self._viewer_ids = ids
            # Keep the user's place while a run adds artifacts - except that a
            # different mission, or the final result appearing, takes the viewer.
            if not switched and not final_arrived and previous in ids:
                self.viewer_choice.setCurrentIndex(ids.index(previous))
            else:
                self.viewer_choice.setCurrentIndex(0)
            self.viewer_choice.blockSignals(False)
        if switched:
            self.saved_note.hide()          # a note about a save in a different mission would mislead
        self._viewer_mission = mission.id
        self._show_selected()
        files = [a for a in mission.artifacts if a.kind == ArtifactKind.FILE]
        self.save_files_action.setVisible(bool(files))
        pending = [a for a in files if not a.meta.get("applied")]
        self.apply_button.setVisible(bool(mission.workspace_path and pending and self._latest_files(mission)))
        if mission.final_artifact_id and self._announced != (mission.id, mission.final_artifact_id):
            self._announced = (mission.id, mission.final_artifact_id)
            self.run_tabs.setCurrentIndex(3)

    @staticmethod
    def _latest_files(mission: Mission) -> list[Artifact]:
        latest: dict[str, Artifact] = {}
        for artifact in mission.artifacts:
            if artifact.kind == ArtifactKind.FILE:
                latest[artifact.meta.get("path") or artifact.title] = artifact
        return [a for a in latest.values() if not a.meta.get("applied")]

    def _show_selected(self) -> None:
        mission = self._controller.snapshot()
        key = self.viewer_choice.currentData()
        if mission is None or key is None:
            self.viewer.clear()
            return
        bar = self.viewer.verticalScrollBar()
        position = bar.value()
        if key == "__sources__":
            self.viewer.setHtml(self._sources_html(mission))
        else:
            artifact = mission.artifact(key)
            if artifact is None:
                return
            if artifact.kind == ArtifactKind.FILE:
                diff = artifact.meta.get("diff")
                body = f"```\n{artifact.content}\n```"
                if diff:
                    body = f"**Proposed change to `{artifact.meta.get('path')}`**\n\n```diff\n{diff}\n```"
                self.viewer.setMarkdown(f"### {artifact.title}\n\n{body}")
                _style_markdown(self.viewer.document(), self._m)
            else:
                self.viewer.setMarkdown(artifact.content)
                _style_markdown(self.viewer.document(), self._m)
        bar.setValue(position)
        saveable = key != "__sources__"
        self.save_button.setEnabled(saveable)
        self.save_as_action.setEnabled(saveable)
        self.copy_button.setEnabled(saveable)

    def _open_link(self, url) -> None:
        """A source link the user clicked: open it in a normal browser tab."""
        address = url.toString()
        if self._browser is not None and address.lower().startswith(("http://", "https://")):
            self._browser.open_tab(address)

    def _sources_html(self, mission: Mission) -> str:
        c = self._c
        if not mission.sources:
            return f"<p style='color:{c.muted}'>No pages, text or files were attached to this mission.</p>"
        groups = (
            ("Attached by you", [s for s in mission.sources if s.kind in SourceKind.ATTACHED]),
            ("Web pages (retrieved text)", [s for s in mission.sources if s.kind == SourceKind.WEB and s.depth == "page"]),
            ("Web search snippets (page not opened)",
             [s for s in mission.sources if s.kind == SourceKind.WEB and s.depth != "page"]),
            ("From your local knowledge", [s for s in mission.sources if s.kind == SourceKind.KNOWLEDGE]),
        )
        out = []
        for title, items in groups:
            if not items:
                continue
            out.append(f"<h4 style='margin-bottom:2px'>{escape(title)}</h4>")
            for source in items:
                if source.usable:
                    state = f"<span style='color:{c.success}'>included</span>"
                    if source.status == SourceStatus.TRUNCATED:
                        state += " (truncated)"
                else:
                    state = f"<span style='color:{c.danger}'>not included</span> \u2014 {escape(source.error)}"
                link = f"<br><a href='{escape(source.url)}'>{escape(source.url)}</a>" if source.url else ""
                when = (f" \u00b7 page text retrieved {escape(source.retrieved)}"
                        + (" \u00b7 shortened" if source.truncated else "") if source.retrieved else "")
                note = f"<br><span style='color:{c.muted}'>{escape(source.note)}</span>" if source.note else ""
                out.append(f"<p style='margin-top:2px'><b>[{source.id}] {escape(source.title)}</b><br>"
                           f"{state}{when}{link}{note}</p>")
        return "".join(out)

    def _current_artifact(self) -> Artifact | None:
        mission = self._controller.snapshot()
        key = self.viewer_choice.currentData()
        if mission is None or key in (None, "__sources__"):
            return None
        return mission.artifact(key)

    def _copy_current(self) -> None:
        artifact = self._current_artifact()
        if artifact is not None:
            QApplication.clipboard().setText(artifact.content)

    def _suggested_name(self, artifact: Artifact) -> str:
        if artifact.kind == ArtifactKind.FILE:
            return Path(artifact.meta.get("path") or artifact.title).name
        stem = {ArtifactKind.FINAL: "team-result", ArtifactKind.REPORT: "team-draft",
                ArtifactKind.NOTES: "team-research-notes", ArtifactKind.TEST_REPORT: "team-test-report",
                ArtifactKind.REVIEW: "team-review", ArtifactKind.PLAN: "team-plan"}.get(artifact.kind, "team-output")
        return f"{stem}-{artifact.id}.md"

    def _mission_id(self) -> int:
        mission = self._controller.snapshot()
        return mission.id if mission else 0

    def _note_saved(self, path: str) -> None:
        self.saved_note.setText(f"Saved: {path}")
        self.saved_note.show()

    def _save_current(self) -> None:
        """Default save: into Downloads, through the Downloads manager."""
        artifact = self._current_artifact()
        if artifact is None:
            return
        try:
            self._note_saved(self._controller.save_to_downloads(
                self._suggested_name(artifact), artifact.content, self._mission_id()))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    def _save_current_as(self) -> None:
        artifact = self._current_artifact()
        if artifact is None:
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Save as", self._suggested_name(artifact))
        if not path:
            return
        target = Path(path)
        try:
            self._note_saved(self._controller.save_to_downloads(
                target.name, artifact.content, self._mission_id(), directory=str(target.parent), overwrite=True))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    def _save_all_files(self) -> None:
        mission = self._controller.snapshot()
        if mission is None:
            return
        latest: dict[str, Artifact] = {}
        for artifact in mission.artifacts:
            if artifact.kind == ArtifactKind.FILE:
                latest[artifact.meta.get("path") or artifact.title] = artifact
        if not latest:
            return
        slug = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in mission.goal)[:32].strip() or "mission"
        folder = f"AI Team - {slug} (#{mission.id})"
        try:
            last = ""
            for rel, artifact in latest.items():
                last = self._controller.save_to_downloads(rel, artifact.content, mission.id, subfolder=folder)
            self._note_saved(f"{len(latest)} file(s) in {Path(last).parent}")
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    def _apply_to_workspace(self) -> None:
        mission = self._controller.snapshot()
        if mission is None:
            return
        pending = self._latest_files(mission)
        if not pending:
            return
        summary = "\n\n".join(
            f"{'NEW ' if a.meta.get('is_new') else 'EDIT'} {a.meta.get('path')}\n"
            f"{(a.meta.get('diff') or a.content)[:1500]}" for a in pending)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Apply changes to your workspace?")
        box.setText(f"Write {len(pending)} file(s) into\n{mission.workspace_path}?")
        box.setInformativeText("The Coder only proposed these. Review the changes below; they overwrite the files in place.")
        box.setDetailedText(summary)
        apply = box.addButton("Apply changes", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not apply:
            return
        try:
            self._controller.apply_files([a.id for a in pending])
        except (WorkspaceError, OSError) as exc:
            QMessageBox.warning(self, "Could not apply", str(exc))

    # --------------------------------------------------------------- history
    def _refresh_history(self) -> None:
        self.history_list.clear()
        rows = self._controller.history()
        for row in rows:
            when = time.strftime("%b %d %H:%M", time.localtime(row.updated_at))
            item = QListWidgetItem(f"{_elide(row.goal, 90)}\n{MissionStatus.LABELS.get(row.status, row.status)} · {when}")
            item.setData(Qt.ItemDataRole.UserRole, row.id)
            self.history_list.addItem(item)
        self.history_empty.setVisible(not rows)

    def _selected_history_id(self) -> int | None:
        item = self.history_list.currentItem()
        return None if item is None else int(item.data(Qt.ItemDataRole.UserRole))

    def _open_history(self) -> None:
        mission_id = self._selected_history_id()
        if mission_id is None:
            return
        if self._controller.open_mission(mission_id) is not None:
            self.top_tabs.setCurrentIndex(0)
            self._shown_mission = None
            self.refresh()

    def _delete_history(self) -> None:
        mission_id = self._selected_history_id()
        if mission_id is None:
            return
        if confirm_destructive(self, "Delete saved mission", "Delete this mission and everything it produced?",
                               "Delete", informative="Its results, activity and any attached page text are removed."):
            self._controller.delete_mission(mission_id)

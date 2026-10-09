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
from PySide6.QtGui import (
    QColor, QFontMetrics, QKeySequence, QPalette, QShortcut, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QFrame, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
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
from app.team.followup import MODE_LABELS as FOLLOWUP_MODE_LABELS, MODES as FOLLOWUP_MODES
from app.ui import team_results
from app.team import templates as team_templates
from app.ui.flow_layout import FlowLayout
from app.ui.team_results import VIEWS

_STATUS_TEXT = {
    TaskStatus.PENDING: "waiting", TaskStatus.RUNNING: "working", TaskStatus.DONE: "done",
    TaskStatus.FAILED: "failed", TaskStatus.BLOCKED: "blocked", TaskStatus.SKIPPED: "skipped",
    TaskStatus.CANCELLED: "cancelled",
}


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _one_line(note: str) -> str:
    """A handoff written as bullets, shown as one readable line."""
    parts = [line.strip().lstrip("-*\u2022 ").strip() for line in note.splitlines()]
    return "; ".join(p.rstrip(".") for p in parts if p) or note


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
        self._view = "answer"
        self._template_id = ""
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
        controller.followup_finished.connect(self._on_followup_finished)
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
                                     "and write a recommendation.” (Ctrl+Enter to start)")
        self.goal.setAccessibleName("Mission")
        self.goal.setMinimumHeight(84)
        self.goal.setMaximumHeight(140)
        self.goal.textChanged.connect(self._sync_start)
        QShortcut(QKeySequence("Ctrl+Return"), self.goal, activated=self._start_if_ready)
        QShortcut(QKeySequence("Ctrl+Enter"), self.goal, activated=self._start_if_ready)
        box.addWidget(self.goal)

        # Templates: an editable starting sentence, what it needs, and which agents it will use.
        templates = FlowLayout(spacing=m.space_1)
        self.template_buttons: dict[str, QPushButton] = {}
        for template in team_templates.TEMPLATES:
            button = QPushButton(template.label, page)
            button.setProperty("kind", "chip")
            button.setCheckable(True)
            button.setToolTip(template.summary)
            button.setAccessibleName(f"Template: {template.label}")
            button.clicked.connect(lambda _c=False, t=template.id: self._pick_template(t))
            self.template_buttons[template.id] = button
            templates.addWidget(button)
        box.addLayout(templates)
        self.template_note = QLabel("", page)
        self.template_note.setWordWrap(True)
        self.template_note.setTextFormat(Qt.TextFormat.RichText)
        self.template_note.setAccessibleName("What this template needs")
        self.template_note.setStyleSheet(f"font-size:{m.text_xs}px;")
        self.template_note.linkActivated.connect(self._on_template_link)
        self._theme_links(self.template_note)
        self.template_note.hide()
        box.addWidget(self.template_note)

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
        # Steer: extra instructions while the team works. They apply at the next task boundary.
        self.steer_box = QWidget(page)
        steer = QVBoxLayout(self.steer_box)
        steer.setContentsMargins(0, 0, 0, 0)
        steer.setSpacing(m.space_1)
        row = QHBoxLayout()
        self.steer_input = QLineEdit(self.steer_box)
        self.steer_input.setPlaceholderText("Add an instruction for the team\u2026")
        self.steer_input.setAccessibleName("Instruction for the running team")
        self.steer_input.returnPressed.connect(self._send_steer)
        self.steer_input.textChanged.connect(self._sync_steer)
        row.addWidget(self.steer_input, 1)
        self.steer_send = QPushButton("Send", self.steer_box)
        self.steer_send.setProperty("kind", "chip")
        self.steer_send.setToolTip("Takes effect when the next task starts - a task already running is never changed")
        self.steer_send.clicked.connect(self._send_steer)
        row.addWidget(self.steer_send)
        steer.addLayout(row)
        self.steer_redo = QCheckBox("Also redo finished work this changes", self.steer_box)
        self.steer_redo.setToolTip("Marks finished tasks that depend on the change as out of date and re-runs them "
                                   "(uses more model calls).")
        steer.addWidget(self.steer_redo)
        self.steer_note = QLabel("", self.steer_box)
        self.steer_note.setWordWrap(True)
        self.steer_note.setProperty("kind", "muted")
        self.steer_note.setAccessibleName("Your instructions")
        steer.addWidget(self.steer_note)
        self.steer_box.hide()
        box.addWidget(self.steer_box)

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

        # View switcher: wraps instead of overflowing a narrow column; every button is a Tab stop,
        # has an Alt+letter mnemonic, and the checked one is announced as the current view.
        switch = FlowLayout(spacing=m.space_1)
        self._view_group = QButtonGroup(page)
        self._view_group.setExclusive(True)
        self.view_buttons: dict[str, QPushButton] = {}
        for key, label in VIEWS:
            button = QPushButton(label, page)
            button.setCheckable(True)
            button.setProperty("kind", "chip")
            button.setAccessibleName(label.replace("&", "") + " view")
            button.clicked.connect(lambda _c=False, k=key: self._set_view(k))
            self._view_group.addButton(button)
            self.view_buttons[key] = button
            switch.addWidget(button)
        self.view_buttons["answer"].setChecked(True)
        box.addLayout(switch)

        self.answer_banner = QLabel("", page)
        self.answer_banner.setWordWrap(True)
        self.answer_banner.setTextFormat(Qt.TextFormat.RichText)
        self.answer_banner.setAccessibleName("About this answer")
        self.answer_banner.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
        self.answer_banner.linkActivated.connect(lambda link: self._set_view(link))
        self._theme_links(self.answer_banner)
        box.addWidget(self.answer_banner)

        picker = QHBoxLayout()
        self.viewer_choice = QComboBox(page)
        self.viewer_choice.setAccessibleName("Choose which item to show")
        self.viewer_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.viewer_choice.setMinimumContentsLength(12)
        self.viewer_choice.currentIndexChanged.connect(self._show_selected)
        picker.addWidget(self.viewer_choice, 1)
        self.diff_toggle = QPushButton("Show changes", page)
        self.diff_toggle.setCheckable(True)
        self.diff_toggle.setChecked(True)
        self.diff_toggle.setProperty("kind", "chip")
        self.diff_toggle.setToolTip("Compare this version with the one it replaced")
        self.diff_toggle.toggled.connect(lambda _on: self._show_selected())
        picker.addWidget(self.diff_toggle)
        box.addLayout(picker)

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

        # Ask: follow-up questions about this mission (separate from the run controls).
        self.ask_box = QWidget(page)
        ask = QVBoxLayout(self.ask_box)
        ask.setContentsMargins(0, 0, 0, 0)
        ask.setSpacing(m.space_1)
        self.ask_mode = QComboBox(self.ask_box)
        self.ask_mode.setAccessibleName("What to do with the question")
        for key in FOLLOWUP_MODES:
            self.ask_mode.addItem(FOLLOWUP_MODE_LABELS[key], key)
        self.ask_mode.setItemData(0, "Answers only from this mission's results and sources. Nothing new is researched.",
                                  Qt.ItemDataRole.ToolTipRole)
        self.ask_mode.setItemData(1, "Rewrites the final answer (shorter, simpler...) from the same evidence. "
                                     "The old version stays in History.", Qt.ItemDataRole.ToolTipRole)
        self.ask_mode.setItemData(2, "Searches the web and reads pages for NEW information. Sends short search "
                                     "queries to your configured search provider.", Qt.ItemDataRole.ToolTipRole)
        self.ask_mode.currentIndexChanged.connect(self._sync_ask)
        ask.addWidget(self.ask_mode)
        self.ask_input = QPlainTextEdit(self.ask_box)
        self.ask_input.setAccessibleName("Follow-up question")
        self.ask_input.setPlaceholderText("Ask about this result, e.g. \u201cExplain the price difference\u201d "
                                          "(Ctrl+Enter to send)")
        self.ask_input.setMaximumHeight(72)
        self.ask_input.textChanged.connect(self._sync_ask)
        QShortcut(QKeySequence("Ctrl+Return"), self.ask_input, activated=self._send_ask)
        QShortcut(QKeySequence("Ctrl+Enter"), self.ask_input, activated=self._send_ask)
        ask.addWidget(self.ask_input)
        row = QHBoxLayout()
        self.ask_send = QPushButton("Ask", self.ask_box)
        self.ask_send.setProperty("kind", "primary")
        self.ask_send.clicked.connect(self._send_ask)
        row.addWidget(self.ask_send)
        self.ask_status = QLabel("", self.ask_box)
        self.ask_status.setWordWrap(True)
        self.ask_status.setProperty("kind", "muted")
        self.ask_status.setAccessibleName("Follow-up status")
        row.addWidget(self.ask_status, 1)
        ask.addLayout(row)
        self.ask_box.hide()
        box.addWidget(self.ask_box)

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
        self.export_html_action = menu.addAction("Export as web page (.html)\u2026")
        self.export_html_action.triggered.connect(self._export_html)
        self.save_files_action = menu.addAction("Save all files to Downloads")
        self.save_files_action.triggered.connect(self._save_all_files)
        self.more_button.setMenu(menu)
        self._more_menu = menu
        for button in (self.save_button, self.copy_button, self.more_button):
            actions.addWidget(button)
        self.actions_box = QWidget(page)
        self.actions_box.setLayout(actions)
        box.addWidget(self.actions_box)
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
        self._refresh_template_note()
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

    # -- templates -------------------------------------------------------------------------
    def _template(self) -> "team_templates.Template | None":
        return team_templates.BY_ID.get(self._template_id)

    def _pick_template(self, template_id: str) -> None:
        """Choose (or, clicking the chosen one again, drop) a template. The starting text replaces the
        mission box only when that box is empty or still holds a template's own text - your wording is never
        overwritten - but the template's agent routing applies either way."""
        settings = self._controller.settings
        previous = self._template()
        if template_id == self._template_id:
            self._template_id = ""
        else:
            template = team_templates.BY_ID[template_id]
            current = self.goal.toPlainText().strip()
            own_text = {team_templates.goal_for(settings, t).strip() for t in team_templates.TEMPLATES}
            self._template_id = template_id
            if not current or current in own_text:
                self.goal.setPlainText(team_templates.goal_for(settings, template))
                cursor = self.goal.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                self.goal.setTextCursor(cursor)
            self.goal.setFocus()
        for key, button in self.template_buttons.items():
            button.setChecked(key == self._template_id)
        self._refresh_template_note()
        self._sync_start()

    def _refresh_template_note(self) -> None:
        template = self._template()
        if template is None:
            self.template_note.hide()
            return
        c = self._c
        attached = [s for s in self._sources if s.usable]
        checks = team_templates.check(
            template, tabs=sum(1 for s in attached if s.kind == SourceKind.TAB), material=len(attached),
            web=self._web_available, workspace=bool(self._workspace_path),
            sandbox=self._controller.sandbox_status().available)
        marks = {"ok": (c.success, "\u2713"), "missing": (c.danger, "\u2717"), "optional-missing": (c.muted, "\u25cb")}
        lines = [f"<b>{escape(template.label)}</b> \u2014 {escape(template.summary)}",
                 f"<span style='color:{c.muted}'>Team: {escape(team_templates.route_text(template))} "
                 "(no other agents will be used)</span>"]
        for state, text, fix in checks:
            colour, mark = marks[state]
            tail = f" \u2014 {escape(fix)}" if fix else ""
            lines.append(f"<span style='color:{colour}'>{mark}</span> {escape(text)}"
                         f"<span style='color:{c.muted}'>{tail}</span>")
        edited = " (edited)" if team_templates.is_edited(self._controller.settings, template) else ""
        lines.append(f"<a href='edit'>Edit this template{edited}</a>")
        self.template_note.setText(self._link_css() + "<br>".join(lines))
        self.template_note.show()

    def _on_template_link(self, link: str) -> None:
        if link == "edit":
            self._edit_template()

    def _edit_template(self) -> None:
        template = self._template()
        settings = self._controller.settings
        if template is None or settings is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit template: {template.label}")
        layout = QVBoxLayout(dialog)
        note = QLabel("This starting text is filled into the mission box when you pick the template. "
                      "Which agents it uses and what it needs do not change.", dialog)
        note.setWordWrap(True)
        layout.addWidget(note)
        editor = QPlainTextEdit(dialog)
        editor.setAccessibleName("Template text")
        editor.setPlainText(team_templates.goal_for(settings, template))
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda: editor.setPlainText(template.goal))
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            team_templates.save_goal(settings, template, editor.toPlainText())
            self.goal.setPlainText(team_templates.goal_for(settings, template))
            self._refresh_template_note()

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
            return ("Next: describe the mission, or pick a template. Optional: add open tabs, text or files "
                    "for the team to read.")
        extras = []
        if not self._sources:
            extras.append("no pages attached" + (" - the team will search the web" if self._web_available
                                                 and self.web_check.isChecked() else ""))
        limits = self._controller.limits()
        return ("Ready. " + ("; ".join(extras) + ". " if extras else "")
                + f"A run uses at most {limits.max_model_calls} model calls; one mission is capped at "
                f"{limits.lifetime_calls} ({limits.LIFETIME_RUNS} runs) across retries. Cancelling stops new work "
                "but cannot undo calls already made.")

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
            self._refresh_template_note()

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
        self._refresh_template_note()
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

    def _start_if_ready(self) -> None:
        if self.start_button.isEnabled():
            self._start()

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
        template = self._template()
        mission = self._controller.start(goal, list(self._sources), self._workspace_path,
                                         web_search=self._web_available and self.web_check.isChecked(),
                                         template_id=template.id if template else "",
                                         allowed_agents=template.agents if template else ())
        if mission is None:
            return
        self._sources = []
        self._workspace_path = ""
        self._template_id = ""
        for button in self.template_buttons.values():
            button.setChecked(False)
        self.template_note.hide()
        self.workspace_label.hide()
        self.goal.clear()
        self._refresh_included()
        self._last_event_seq = 0
        self._shown_mission = None

    # ------------------------------------------------------------- run view
    def _sync_steer(self) -> None:
        self.steer_send.setEnabled(bool(self.steer_input.text().strip()))

    def _send_steer(self) -> None:
        text = self.steer_input.text().strip()
        if not text:
            return
        message = self._controller.steer(text, self.steer_redo.isChecked())
        if message:
            self.steer_note.setText(message)
            return
        self.steer_input.clear()
        self.steer_redo.setChecked(False)

    def _render_steer(self, mission: Mission, active: bool) -> None:
        self.steer_box.setVisible(active and mission.status != MissionStatus.PLANNING or
                                  (active and bool(mission.steering)))
        self._sync_steer()
        lines = []
        for s in mission.steering:
            state = (f"applies from {s['effective_from']}" if s["effective_from"]
                     else "waits for the next task to start")
            redo = " \u00b7 redoing dependent work" if s["redo"] else ""
            lines.append(f"#{s['id']} {_elide(s['text'], 60)} \u2014 {state}{redo}")
        self.steer_note.setText("\n".join(lines))
        self.steer_note.setVisible(bool(lines))

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
        limits = self._controller.limits()
        run_left, mission_left = limits.allowance(mission.model_calls, mission.calls_at_run_start)
        usage = ""
        if mission.model_calls:
            usage = f" · {mission.model_calls} model calls"
            usage += (f" · {mission_left} left" if not active else f" · {run_left} left this run")
            if mission_left <= limits.max_model_calls // 5:
                usage = usage.replace(f"{mission_left} left", f"<span style='color:{c.warning}'>{mission_left} left</span>")
        self.run_status.setToolTip(
            f"Model-call allowance: each run may use up to {limits.max_model_calls} calls (retries included). "
            f"Retry / Resume / Revise again start a fresh run, but one mission is capped at "
            f"{limits.lifetime_calls} calls in total ({limits.LIFETIME_RUNS} runs' worth). "
            f"Used {mission.model_calls}; {mission_left} left for this mission.\n"
            "Cancelling stops new work at once but cannot undo calls already made or tokens already "
            "consumed; a reply that arrives after Cancel is discarded.")
        model = f" · {escape(mission.model_label)}" if mission.model_label else ""
        text = f"<span style='color:{colour}'>●</span> <b>{label}</b>{progress}{usage}{model}"
        if mission.error:
            text += f"<br><span style='color:{c.danger}'>{escape(mission.error)}</span>"
        elif mission.coordinator_note:
            text += f"<br><span style='color:{c.muted}'>{escape(mission.coordinator_note)}…</span>"
        running = [t for t in mission.tasks if t.status == TaskStatus.RUNNING]
        if active and running:
            now = "; ".join(f"{AgentId.LABELS[t.agent]} on {t.id} since {_fmt_time(t.started_at)}" for t in running)
            text += f"<br><span style='color:{c.muted}'>Now: {escape(now)}</span>"
        if active and mission.throttled:
            text += (f"<br><span style='color:{c.warning}'>The provider is rate-limiting, so the team is "
                     "working one task at a time.</span>")
        waiting = [t for t in mission.tasks if t.status == TaskStatus.PENDING and t.not_before > time.time()]
        if active and waiting:
            seconds = int(max(1, max(t.not_before for t in waiting) - time.time()))
            text += (f"<br><span style='color:{c.warning}'>Waiting about {seconds}s for the rate limit to "
                     f"clear before {waiting[0].id}.</span>")
        if mission.status == MissionStatus.CANCELLED and mission.model_calls:
            text += (f"<br><span style='color:{c.muted}'>Cancelled. The {mission.model_calls} model calls already "
                     "made (and their tokens) were used and cannot be undone.</span>")
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
        self._render_steer(mission, active)
        self.cancel_button.setVisible(active)
        actionable = mission.status != MissionStatus.COMPLETED_WITH_ISSUES or bool(
            mission.unresolved_issues or any(t.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
                                             for t in mission.tasks))
        self.retry_button.setVisible(mission.status in (
            MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.INTERRUPTED,
            MissionStatus.COMPLETED_WITH_ISSUES) and actionable and not self._controller.is_running)
        label, tip = self._retry_label(mission)
        self.retry_button.setText(label)
        self.retry_button.setToolTip(tip + f"\nAllowance left for this mission: {mission_left} model calls.")
        self.retry_button.setEnabled(mission_left > 0)
        if mission_left <= 0:
            self.retry_button.setToolTip("This mission has used its whole model-call allowance "
                                         f"({limits.lifetime_calls}). Start a new mission, or raise the limit "
                                         "in Settings.")
        self.cancel_button.setToolTip("Stops new work immediately. Calls already made and tokens already "
                                      "consumed cannot be undone, and a reply that arrives after Cancel is "
                                      "discarded.")
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
        latest = max(done, key=lambda t: t.finished_at) if done else None
        detail = f"{len(done)} task(s) finished"
        if latest is not None and latest.summary:
            detail += f" \u2014 {latest.id}: {latest.summary}"
        return c.success, "done", detail

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
            if task.instructions and task.status != TaskStatus.DONE:
                html += (f"<br><span style='color:{self._c.muted}'>"
                         f"{escape(_elide(task.instructions.split('REVISION REQUEST')[0], 150))}</span>")
            if task.outputs:
                html += f"<br><span style='color:{self._c.muted}'>produced {', '.join(task.outputs)}</span>"
            if task.inputs:
                html += f"<br><span style='color:{self._c.muted}'>received {', '.join(task.inputs)}</span>"
            if task.error:
                tone = self._c.warning if task.status in (TaskStatus.SKIPPED, TaskStatus.BLOCKED) else self._c.danger
                html += f"<br><span style='color:{tone}'>{escape(_elide(task.error, 200))}</span>"
            elif task.summary and task.status == TaskStatus.DONE:
                html += f"<br>{escape(_elide(task.summary, 140))}"
            if task.steering and task.status in (TaskStatus.RUNNING, TaskStatus.DONE):
                html += (f"<br><span style='color:{self._c.muted}'>follows your instruction"
                         f"{'s' if len(task.steering) > 1 else ''} "
                         + ", ".join(f"#{i}" for i in task.steering) + "</span>")
            if task.handoff and task.status == TaskStatus.DONE:
                html += (f"<br><span style='color:{self._c.muted}'>handoff: "
                         f"{escape(_elide(_one_line(task.handoff), 200))}</span>")
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
        if not mission.events:
            self.activity.setPlaceholderText("Nothing has happened yet. Each step the team takes is listed here "
                                             "as it happens.")
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
    # ---------------------------------------------------------------- results workspace
    def _set_view(self, view: str) -> None:
        if view not in self.view_buttons:
            return
        self._view = view
        self.view_buttons[view].setChecked(True)
        mission = self._controller.snapshot()
        if mission is not None:
            self._render_results(mission, keep_choice=False)

    def _render_results(self, mission: Mission, *, keep_choice: bool = True) -> None:
        view = self._view
        counts = team_results.counts(mission)
        for key, label in VIEWS:
            n = counts.get(key, 0) if key != "answer" else 0
            self.view_buttons[key].setText(label + (f" ({n})" if n and key in ("sources", "files", "review", "tests", "ask") else ""))
        switched = mission.id != self._viewer_mission
        final_arrived = bool(mission.final_artifact_id) and mission.final_artifact_id not in self._viewer_ids
        if switched:
            self.saved_note.hide()          # a note about a save in a different mission would mislead
            self._view = view = "answer"
            self.view_buttons["answer"].setChecked(True)
        items = team_results.view_items(mission, view)
        ids = [view] + [k for _, k in items]
        previous = self.viewer_choice.currentData()
        if ids != self._viewer_ids:
            self.viewer_choice.blockSignals(True)
            self.viewer_choice.clear()
            for text, key in items:
                self.viewer_choice.addItem(text, key)
            self._viewer_ids = ids
            keys = [k for _, k in items]
            self.viewer_choice.setCurrentIndex(
                keys.index(previous) if keep_choice and not switched and previous in keys and view != "answer" else 0)
            self.viewer_choice.blockSignals(False)
        self._viewer_mission = mission.id
        self.viewer_choice.setVisible(len(items) > 1)
        self.ask_box.setVisible(view == "ask")
        self.actions_box.setVisible(view != "ask")
        self._show_selected()
        files = [a for a in mission.artifacts if a.kind == ArtifactKind.FILE]
        self.save_files_action.setVisible(bool(files))
        pending = [a for a in files if not a.meta.get("applied")]
        self.apply_button.setVisible(bool(mission.workspace_path and pending and self._latest_files(mission))
                                     and view in ("files", "answer"))
        self._sync_ask()
        if mission.final_artifact_id and self._announced != (mission.id, mission.final_artifact_id):
            first = self._announced is None or self._announced[0] != mission.id
            self._announced = (mission.id, mission.final_artifact_id)
            if first or view != "ask":
                self.run_tabs.setCurrentIndex(3)
                if view != "answer" and view != "ask":
                    self._set_view("answer")

    @staticmethod
    def _latest_files(mission: Mission) -> list[Artifact]:
        latest: dict[str, Artifact] = {}
        for artifact in mission.artifacts:
            if artifact.kind == ArtifactKind.FILE:
                latest[artifact.meta.get("path") or artifact.title] = artifact
        return [a for a in latest.values() if not a.meta.get("applied")]

    def _show_selected(self) -> None:
        mission = self._controller.snapshot()
        view = self._view
        key = self.viewer_choice.currentData()
        self.diff_toggle.hide()
        if mission is None:
            self.viewer.clear()
            return
        bar = self.viewer.verticalScrollBar()
        position = bar.value()
        banner = ""
        if view == "answer":
            banner = escape(team_results.answer_banner(mission))
            if mission.final_artifact_id:
                banner += (" \u00b7 " if banner else "") + "<a href='ask'>Ask a follow-up</a>"
        self.answer_banner.setText(self._link_css() + banner if banner else "")
        self.answer_banner.setVisible(bool(banner))
        saveable = False
        if view == "ask":
            text = team_results.followups_markdown(mission)
            self.viewer.setMarkdown(text or "*No follow-up questions yet.* Pick what to do below, type a question and "
                                    "press **Ask**. Answers state whether they come from this mission's existing "
                                    "evidence or from new research, and every model call counts toward the mission's "
                                    "allowance.")
            _style_markdown(self.viewer.document(), self._m)
        elif key is None:
            self.viewer.setMarkdown(f"*{team_results.empty_text(mission, view)}*")
            _style_markdown(self.viewer.document(), self._m)
        elif key == team_results.SOURCES_KEY:
            self.viewer.setHtml(self._sources_html(mission))
        else:
            artifact = mission.artifact(key)
            if artifact is None:
                return
            saveable = True
            earlier = team_results.predecessor(mission, artifact)
            show_diff = earlier is not None and self.diff_toggle.isChecked()
            if earlier is not None:
                self.diff_toggle.show()
            if show_diff:
                self.viewer.setHtml(team_results.diff_html(earlier, artifact, self._c))
            elif artifact.kind == ArtifactKind.FILE:
                diff = artifact.meta.get("diff")
                body = f"```\n{artifact.content}\n```"
                if diff:
                    body = f"**Proposed change to `{artifact.meta.get('path')}`**\n\n```diff\n{diff}\n```"
                self.viewer.setMarkdown(f"### {artifact.title}\n\n{body}")
            else:
                self.viewer.setMarkdown(artifact.content)
            _style_markdown(self.viewer.document(), self._m)
        bar.setValue(position)
        self.save_button.setEnabled(saveable)
        self.save_as_action.setEnabled(saveable)
        self.export_html_action.setEnabled(saveable)
        self.copy_button.setEnabled(saveable)

    # -- follow-up questions ------------------------------------------------------------
    def _ask_ready(self, mission: Mission | None) -> tuple[bool, str]:
        """(can send, why not / what happens) for the current mode."""
        mode = self.ask_mode.currentData()
        if mission is None or not mission.final_artifact_id and mode != "research":
            return False, "Ask becomes available when the team has a final answer."
        if self._controller.is_running:
            return False, "Working on the previous request\u2026" if self._controller.followup_busy else \
                "The team is still running."
        limits = self._controller.limits()
        _run_left, left = limits.allowance(mission.model_calls, 0)
        if left <= 0:
            return False, (f"This mission has used its whole model-call allowance ({limits.lifetime_calls}). "
                           "Start a new mission to ask more.")
        if mode == "research" and not self._controller.web_status().available:
            return False, "New research needs web search - set it up in Settings. Ask and Rewrite still work."
        cost = "1\u20132 model calls" if mode == "research" else "1 model call"
        return True, f"Uses {cost}; {left} left for this mission."

    def _sync_ask(self) -> None:
        if not hasattr(self, "ask_send"):
            return
        mission = self._controller.snapshot()
        ready, note = self._ask_ready(mission)
        has_text = bool(self.ask_input.toPlainText().strip())
        self.ask_send.setEnabled(ready and has_text)
        busy = self._controller.followup_busy
        self.ask_send.setText("Working\u2026" if busy else self.ask_mode.currentText().split(" (")[0])
        self.ask_input.setEnabled(not busy)
        self.ask_status.setText(note)

    def _send_ask(self) -> None:
        text = self.ask_input.toPlainText().strip()
        mission = self._controller.snapshot()
        if not text or not self.ask_send.isEnabled() or mission is None:
            return
        mode = self.ask_mode.currentData()
        if self._controller.ask_followup(text, mode):
            self.ask_input.clear()
            self._sync_ask()

    def _on_followup_finished(self, message: str) -> None:
        if message:
            self.ask_status.setText(message)
            self.ask_status.setStyleSheet(f"color:{self._c.danger};")
        else:
            self.ask_status.setStyleSheet("")
            mission = self._controller.snapshot()
            if mission is not None and mission.followups and self._view == "ask":
                bar = self.viewer.verticalScrollBar()
                bar.setValue(bar.maximum())
        self.ask_input.setFocus()
        self._sync_ask()

    def _export_html(self) -> None:
        artifact = self._current_artifact()
        mission = self._controller.snapshot()
        if artifact is None or mission is None:
            return
        document = QTextDocument()
        document.setMarkdown(artifact.content)
        body = document.toHtml()
        inner = body[body.find("<body"):]
        inner = inner[inner.find(">") + 1:inner.rfind("</body>")]
        page = team_results.html_document(artifact.title, inner)
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export as web page", team_results.export_name(mission, artifact, "html"), "Web page (*.html)")
        if not path:
            return
        target = Path(path)
        try:
            self._note_saved(self._controller.save_to_downloads(
                target.name, page, mission.id, directory=str(target.parent), overwrite=True))
        except OSError as exc:
            QMessageBox.warning(self, "Could not export", str(exc))

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
        if mission is None or key in (None, team_results.SOURCES_KEY) or self._view == "ask":
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

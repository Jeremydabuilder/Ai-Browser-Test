"""Workspace UI: the compact switcher pill, and the create/manage/delete
dialogs it opens. Kept deliberately small - a pill with an icon and a name,
never a project-management dashboard - per the Phase 17 brief's own UI
instruction.

Wording rule this whole module follows: "Workspace" and "Isolated Profile"
are never conflated. Only a workspace with isolated_profile=True is ever
described as having separate cookies/storage; every dialog here says so
in those words, and never implies it for a plain workspace.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.agent.config import PROVIDERS
from app.ui import theme
from app.workspaces.model import DEFAULT_WORKSPACE_ID, Workspace, new_workspace


class WorkspaceSwitcher(QToolButton):
    """The compact pill in the toolbar: current workspace's icon + name,
    opening a menu of every workspace plus New/Manage actions."""

    def __init__(self, parent: QWidget | None = None, *, on_select=None,
                on_new=None, on_manage=None) -> None:
        super().__init__(parent)
        self._on_select = on_select
        self._on_new = on_new
        self._on_manage = on_manage
        self._workspaces: list[Workspace] = []
        self._current_id = DEFAULT_WORKSPACE_ID
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip("Switch workspace")
        self.setAccessibleName("Workspace switcher")

    def refresh(self, workspaces: list[Workspace], current_id: str) -> None:
        self._workspaces = list(workspaces)
        self._current_id = current_id
        current = next((w for w in workspaces if w.id == current_id), None)
        label = f"{current.icon} {current.name}" if current else "Workspace"
        self.setText(label)
        menu = QMenu(self)
        for workspace in self._workspaces:
            marker = "● " if workspace.id == current_id else "   "
            action = menu.addAction(f"{marker}{workspace.icon} {workspace.name}")
            action.triggered.connect(
                lambda _=False, wid=workspace.id: self._select(wid))
        menu.addSeparator()
        new_action = menu.addAction("New Workspace…")
        new_action.triggered.connect(lambda: self._on_new() if self._on_new else None)
        manage_action = menu.addAction("Manage Workspaces…")
        manage_action.triggered.connect(lambda: self._on_manage() if self._on_manage else None)
        self.setMenu(menu)

    def _select(self, workspace_id: str) -> None:
        if self._on_select is not None and workspace_id != self._current_id:
            self._on_select(workspace_id)


class NewWorkspaceDialog(QDialog):
    """Name, icon, and the one consequential choice: Isolated Profile."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Workspace")
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Icon", self))
        self.icon_edit = QLineEdit("🗂", self)
        self.icon_edit.setMaximumWidth(48)
        name_row.addWidget(self.icon_edit)
        name_row.addWidget(QLabel("Name", self))
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("e.g. Coding, School, Personal")
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)

        self.isolated_checkbox = QCheckBox(
            "Use an isolated profile (separate cookies and sign-ins)", self)
        layout.addWidget(self.isolated_checkbox)
        note = QLabel(
            "Without this, the workspace shares this browser's one sign-in "
            "state - separate tabs and history, but the same logins. With "
            "it, this workspace gets its own cookies and storage, "
            "completely separate from every other workspace - but its "
            "tabs cannot show PyBrowser's own New Tab page or Mission "
            "Library (a Qt WebEngine limitation), and will show a blank "
            "page there instead.", self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.palette_for(QApplication.instance()).muted}; "
                          f"font-size:{m.text_xs}px;")
        layout.addWidget(note)

        self._error_label = QLabel("", self)
        self._error_label.setStyleSheet(
            f"color:{theme.palette_for(QApplication.instance()).warning_text};")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        create = QPushButton("Create", self)
        create.setProperty("kind", "primary")
        create.clicked.connect(self._on_create)
        buttons.addWidget(create)
        layout.addLayout(buttons)

        self._result: Workspace | None = None

    def _on_create(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._error_label.setText("Name is required.")
            self._error_label.show()
            return
        icon = self.icon_edit.text().strip() or "🗂"
        self._result = new_workspace(name, icon=icon,
                                     isolated_profile=self.isolated_checkbox.isChecked())
        self.accept()

    def result_workspace(self) -> Workspace | None:
        return self._result


class DeleteWorkspaceDialog(QDialog):
    """The Phase 17 brief's own explicit rule: deleting a workspace must
    clearly ask what happens to its Missions/scheduled tasks/context -
    never a silent cascade delete."""

    def __init__(self, workspace: Workspace, others: list[Workspace],
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f'Delete "{workspace.name}"')
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        layout.addWidget(QLabel(
            f'Deleting "{workspace.name}" will close its tabs. Its Missions, '
            "scheduled tasks, and indexed browsing history need somewhere to go:",
            self, wordWrap=True))

        self.global_radio = QRadioButton("Keep them, as global (not tied to any workspace)", self)
        self.global_radio.setChecked(True)
        layout.addWidget(self.global_radio)

        self.move_radio = QRadioButton("Move them to:", self)
        layout.addWidget(self.move_radio)
        self.target_combo = QComboBox(self)
        for other in others:
            self.target_combo.addItem(f"{other.icon} {other.name}", other.id)
        self.target_combo.setEnabled(False)
        self.move_radio.toggled.connect(self.target_combo.setEnabled)
        layout.addWidget(self.target_combo)
        if not others:
            self.move_radio.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        delete = QPushButton("Delete Workspace", self)
        delete.setProperty("kind", "danger")
        delete.clicked.connect(self.accept)
        buttons.addWidget(delete)
        layout.addLayout(buttons)

    def reassign_to(self) -> str | None:
        """The workspace id to move Missions/tasks/history to, or None to
        make them global (unscoped)."""
        if self.move_radio.isChecked():
            return self.target_combo.currentData()
        return None


class WorkspaceSettingsDialog(QDialog):
    """Edit one workspace's soft preferences: homepage, preferred provider/
    model, and which connected MCP servers it can see. Everything here is
    exactly that - a preference/filter, never a hard lock or a duplicated
    credential; see Workspace and McpConnectionManager.set_workspace_
    visibility.
    """

    def __init__(self, workspace: Workspace, mcp_servers: list[tuple[str, str]],
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f'Workspace Settings — {workspace.name}')
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("Preferred provider", self))
        self.provider_combo = QComboBox(self)
        self.provider_combo.addItem("(no preference)", "")
        for info in PROVIDERS:
            self.provider_combo.addItem(info.label, info.id)
        if workspace.preferred_provider:
            index = self.provider_combo.findData(workspace.preferred_provider)
            if index != -1:
                self.provider_combo.setCurrentIndex(index)
        provider_row.addWidget(self.provider_combo, 1)
        layout.addLayout(provider_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Preferred model", self))
        self.model_edit = QLineEdit(workspace.preferred_model, self)
        self.model_edit.setPlaceholderText("Optional - e.g. claude-opus-5")
        model_row.addWidget(self.model_edit, 1)
        layout.addLayout(model_row)

        home_row = QHBoxLayout()
        home_row.addWidget(QLabel("Homepage", self))
        self.homepage_edit = QLineEdit(workspace.homepage, self)
        self.homepage_edit.setPlaceholderText("Optional - blank uses the app default")
        home_row.addWidget(self.homepage_edit, 1)
        layout.addLayout(home_row)

        layout.addWidget(QLabel("Visible MCP connections", self))
        self.mcp_checkbox_all = QCheckBox("All connected servers", self)
        self.mcp_checkbox_all.setChecked(workspace.mcp_visible_server_ids is None)
        layout.addWidget(self.mcp_checkbox_all)
        self.mcp_list = QListWidget(self)
        self.mcp_list.setMaximumHeight(120)
        visible_ids = set(workspace.mcp_visible_server_ids or ())
        for server_id, name in mcp_servers:
            item = QListWidgetItem(name, self.mcp_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(1001, server_id)
            checked = workspace.mcp_visible_server_ids is None or server_id in visible_ids
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.mcp_list.setEnabled(not self.mcp_checkbox_all.isChecked())
        self.mcp_checkbox_all.toggled.connect(
            lambda checked: self.mcp_list.setEnabled(not checked))
        layout.addWidget(self.mcp_list)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save", self)
        save.setProperty("kind", "primary")
        save.clicked.connect(self.accept)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def mcp_visible_server_ids(self) -> tuple[str, ...] | None:
        if self.mcp_checkbox_all.isChecked():
            return None
        return tuple(
            self.mcp_list.item(i).data(1001) for i in range(self.mcp_list.count())
            if self.mcp_list.item(i).checkState() == Qt.CheckState.Checked)

    def preferred_provider(self) -> str:
        return self.provider_combo.currentData() or ""

    def preferred_model(self) -> str:
        return self.model_edit.text().strip()

    def homepage(self) -> str:
        return self.homepage_edit.text().strip()


class ManageWorkspacesDialog(QDialog):
    def __init__(self, workspaces: list[Workspace], parent: QWidget | None = None, *,
                on_rename, on_duplicate, on_delete, on_settings=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Workspaces")
        self.resize(420, 420)
        self._on_rename = on_rename
        self._on_duplicate = on_duplicate
        self._on_delete = on_delete
        self._on_settings = on_settings
        m = theme.METRICS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        self.list_widget = QListWidget(self)
        for workspace in workspaces:
            item = QListWidgetItem(f"{workspace.icon} {workspace.name}", self.list_widget)
            item.setData(1000, workspace.id)
        layout.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        rename_button = QPushButton("Rename", self)
        rename_button.clicked.connect(self._rename_selected)
        buttons.addWidget(rename_button)
        if on_settings is not None:
            settings_button = QPushButton("Settings…", self)
            settings_button.clicked.connect(self._settings_selected)
            buttons.addWidget(settings_button)
        duplicate_button = QPushButton("Duplicate", self)
        duplicate_button.clicked.connect(self._duplicate_selected)
        buttons.addWidget(duplicate_button)
        delete_button = QPushButton("Delete", self)
        delete_button.setProperty("kind", "danger")
        delete_button.clicked.connect(self._delete_selected)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _selected_id(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(1000) if item is not None else None

    def _rename_selected(self) -> None:
        workspace_id = self._selected_id()
        if workspace_id:
            self._on_rename(workspace_id)

    def _duplicate_selected(self) -> None:
        workspace_id = self._selected_id()
        if workspace_id:
            self._on_duplicate(workspace_id)

    def _delete_selected(self) -> None:
        workspace_id = self._selected_id()
        if workspace_id:
            self._on_delete(workspace_id)

    def _settings_selected(self) -> None:
        workspace_id = self._selected_id()
        if workspace_id and self._on_settings is not None:
            self._on_settings(workspace_id)

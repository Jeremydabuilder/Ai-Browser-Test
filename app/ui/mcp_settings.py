"""Settings UI for MCP servers: "Connected Tools".

Consumer-facing, not a JSON editor - adding a server is a small form (name,
transport, command-or-address, arguments, an optional secret), and every tool
a server offers is shown with the same classification app/mcp/safety.py
assigned it, so a write tool reads as blocked here for the same reason it is
blocked from Py: never because the server said "trust me".

Owned by SettingsDialog, which passes it the McpConnectionManager MainWindow
already owns - this module never constructs one itself, the same way it never
imports anything from app.agent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.mcp import config as mcp_config
from app.mcp.connection_manager import McpConnectionManager
from app.mcp.safety import describe_sensitivity
from app.mcp.types import ConnectionState, McpServerConfig, Permission, Scope, Sensitivity, Transport
from app.ui import theme

_STATUS_LABELS = {
    ConnectionState.DISCONNECTED: "Not connected",
    ConnectionState.CONNECTING: "Connecting…",
    ConnectionState.CONNECTED: "Connected",
    ConnectionState.AUTH_REQUIRED: "Needs authentication",
    ConnectionState.ERROR: "Connection error",
    ConnectionState.RECONNECTING: "Reconnecting…",
    ConnectionState.DISABLED: "Disabled",
}

_TRANSPORT_LABELS = {
    Transport.STDIO: "Local command",
    Transport.STREAMABLE_HTTP: "Web address (Streamable HTTP)",
}


def _status_color(c, state: str) -> str:
    if state == ConnectionState.CONNECTED:
        return c.success
    if state in (ConnectionState.ERROR, ConnectionState.AUTH_REQUIRED):
        return c.danger
    if state in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING):
        return c.warning
    return c.muted


def _permission_summary(manager: McpConnectionManager, server_id: str, tools) -> str:
    """"12 tools · 3 Always Allowed, 2 Ask, 1 Denied" - the at-a-glance
    breakdown the request's example shows, computed the same way for the
    server row here and for each server's heading in the global view."""
    if not tools:
        return "—"
    read_only = allow = ask = deny = 0
    for tool in tools:
        if tool.never_confirmed:
            read_only += 1
            continue
        permission = manager.permission_for(server_id, tool.name)
        if permission == Permission.ALLOW:
            allow += 1
        elif permission == Permission.DENY:
            deny += 1
        else:
            ask += 1
    parts = []
    if read_only:
        parts.append(f"{read_only} read-only")
    if allow:
        parts.append(f"{allow} Always Allowed")
    if ask:
        parts.append(f"{ask} Ask")
    if deny:
        parts.append(f"{deny} Denied")
    return ", ".join(parts)


class ConnectedToolsPanel(QWidget):
    """The "Connected Tools" tab: configured MCP servers and their tools."""

    def __init__(self, manager: McpConnectionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(m.space_2)

        intro = QLabel(
            "Connect Py to external tools over MCP (Model Context Protocol). "
            "Only read-only tools are ever available to Py in this version - "
            "anything that writes, deletes or moves money is shown but "
            "blocked.", self)
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        layout.addWidget(intro)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Server", "Status", "Tools"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self._view_tools())
        layout.addWidget(self.tree, 1)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(m.space_2)
        self.connect_button = QPushButton("Connect", self)
        self.disconnect_button = QPushButton("Disconnect", self)
        self.reconnect_button = QPushButton("Reconnect", self)
        self.toggle_button = QPushButton("Disable", self)
        self.view_tools_button = QPushButton("View tools", self)
        self.remove_button = QPushButton("Remove", self)
        self.remove_button.setProperty("kind", "danger")
        for button, handler in (
            (self.connect_button, self._connect_selected),
            (self.disconnect_button, self._disconnect_selected),
            (self.reconnect_button, self._reconnect_selected),
            (self.toggle_button, self._toggle_selected),
            (self.view_tools_button, self._view_tools),
            (self.remove_button, self._remove_selected),
        ):
            button.clicked.connect(handler)
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        global_row = QHBoxLayout()
        global_row.setSpacing(m.space_2)
        self.permissions_button = QPushButton("Manage all permissions…", self)
        self.permissions_button.clicked.connect(self._open_global_permissions)
        self.audit_button = QPushButton("View activity log…", self)
        self.audit_button.clicked.connect(self._open_audit_log)
        global_row.addWidget(self.permissions_button)
        global_row.addWidget(self.audit_button)
        global_row.addStretch(1)
        layout.addLayout(global_row)

        add_row = QHBoxLayout()
        add_row.addStretch(1)
        self.add_button = QPushButton("+ Add MCP Server…", self)
        self.add_button.setProperty("kind", "primary")
        self.add_button.clicked.connect(self._add_server)
        add_row.addWidget(self.add_button)
        layout.addLayout(add_row)

        self._manager.server_changed.connect(self._refresh)
        self._refresh()

    # -- rendering ----------------------------------------------------------
    def _refresh(self, *_args: Any) -> None:
        selected_id = self._selected_server_id()
        self.tree.clear()
        c = theme.palette_for(QApplication.instance())
        for config in self._manager.configured_servers():
            state = self._manager.state(config.id)
            connection = self._manager.connection(config.id)
            tools = connection.tools if connection else []
            item = QTreeWidgetItem([
                config.name,
                _STATUS_LABELS.get(state, state),
                _permission_summary(self._manager, config.id, tools),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, config.id)
            from PySide6.QtGui import QColor
            item.setForeground(1, QColor(_status_color(c, state)))
            self.tree.addTopLevelItem(item)
            if config.id == selected_id:
                item.setSelected(True)
        self._sync_buttons()

    def _selected_server_id(self) -> str | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _sync_buttons(self) -> None:
        server_id = self._selected_server_id()
        has_selection = server_id is not None
        state = self._manager.state(server_id) if has_selection else ConnectionState.DISCONNECTED
        config = self._manager.connection(server_id).config if has_selection else None
        self.connect_button.setEnabled(
            has_selection and config is not None and config.enabled
            and state not in (ConnectionState.CONNECTED, ConnectionState.CONNECTING))
        self.disconnect_button.setEnabled(has_selection and state == ConnectionState.CONNECTED)
        self.reconnect_button.setEnabled(
            has_selection and config is not None and config.enabled)
        self.view_tools_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)
        if has_selection and config is not None:
            self.toggle_button.setEnabled(True)
            self.toggle_button.setText("Disable" if config.enabled else "Enable")
        else:
            self.toggle_button.setEnabled(False)
            self.toggle_button.setText("Disable")

    # -- actions --------------------------------------------------------
    def _connect_selected(self) -> None:
        server_id = self._selected_server_id()
        if server_id:
            self._manager.connect_server(server_id)

    def _disconnect_selected(self) -> None:
        server_id = self._selected_server_id()
        if server_id:
            self._manager.disconnect_server(server_id)

    def _reconnect_selected(self) -> None:
        server_id = self._selected_server_id()
        if server_id:
            self._manager.reconnect_server(server_id)

    def _toggle_selected(self) -> None:
        server_id = self._selected_server_id()
        if not server_id:
            return
        connection = self._manager.connection(server_id)
        if connection is None:
            return
        self._manager.set_enabled(server_id, not connection.config.enabled)

    def _remove_selected(self) -> None:
        server_id = self._selected_server_id()
        if not server_id:
            return
        connection = self._manager.connection(server_id)
        name = connection.config.name if connection else server_id
        box = QMessageBox(self)
        box.setWindowTitle("Remove MCP server")
        box.setText(f"Remove “{name}”? Any stored credential for it is deleted too.")
        remove = box.addButton("Remove", QMessageBox.ButtonRole.DestructiveRole)
        remove.setProperty("kind", "danger")
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is remove:
            self._manager.remove_server(server_id)

    def _view_tools(self) -> None:
        server_id = self._selected_server_id()
        if not server_id:
            return
        connection = self._manager.connection(server_id)
        if connection is None:
            return
        ToolListDialog(self._manager, server_id, connection.config.name, self).exec()

    def _open_global_permissions(self) -> None:
        GlobalPermissionsDialog(self._manager, self).exec()

    def _open_audit_log(self) -> None:
        AuditLogDialog(self._manager, self).exec()

    def _add_server(self) -> None:
        dialog = AddServerDialog(self._manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh()


_PERMISSION_LABELS = {
    Permission.ALLOW: "Allow",
    Permission.ASK: "Ask every time",
    Permission.DENY: "Deny",
}


def build_permission_combo(manager: McpConnectionManager, server_id: str, tool_name: str,
                           sensitivity: str, *, parent: QWidget | None = None) -> QComboBox:
    """One Allow/Ask/Deny control, shared by the per-server tool list and
    the global permissions view so the two can never quietly drift apart.

    A DESTRUCTIVE tool simply never has "Allow" as an option here - not
    grayed out, not present - matching set_tool_permission's own refusal
    of that exact combination one layer down. Removing the option and the
    registry refusing it are two guards for the same rule; neither one
    depends on the other actually catching it.
    """
    combo = QComboBox(parent)
    values = ((Permission.ASK, Permission.DENY) if sensitivity == Sensitivity.DESTRUCTIVE
             else (Permission.ALLOW, Permission.ASK, Permission.DENY))
    for value in values:
        combo.addItem(_PERMISSION_LABELS[value], value)
    current = manager.permission_for(server_id, tool_name)
    index = combo.findData(current)
    combo.setCurrentIndex(index if index >= 0 else combo.findData(Permission.ASK))
    combo.currentIndexChanged.connect(
        lambda _i, name=tool_name, box=combo: manager.set_tool_permission(
            server_id, name, box.currentData()))
    return combo


class ToolListDialog(QDialog):
    """Every tool a server offers, its classification, and - for anything
    that isn't read-only - a per-tool Allow/Ask/Deny control.

    A read-only tool has no control at all: it always runs without asking,
    the same as it did in Phase 1, and showing a dropdown that can only
    ever say "Allow" would just be decoration. Everything else shows the
    *current effective* permission (a remembered decision if one exists,
    otherwise safety.default_permission's fallback - always Ask, never a
    silent Allow) and lets it be changed here, which stores it as Scope.
    ALWAYS - Settings has no Mission to scope a decision to.
    """

    def __init__(self, manager: McpConnectionManager, server_id: str, server_name: str,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._server_id = server_id
        self.setWindowTitle(f"Tools from {server_name}")
        self.resize(560, 380)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        tools = manager.all_tools(server_id)
        if not tools:
            empty = QLabel("This server has not reported any tools yet.", self)
            empty.setStyleSheet(f"color:{c.muted};")
            layout.addWidget(empty)
        else:
            self.tree = QTreeWidget(self)
            self.tree.setHeaderLabels(["Tool", "Classification", "Permission"])
            self.tree.setRootIsDecorated(False)
            self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            for tool in sorted(tools, key=lambda t: t.name):
                item = QTreeWidgetItem([tool.name, describe_sensitivity(tool.sensitivity), ""])
                self.tree.addTopLevelItem(item)
                if tool.never_confirmed:
                    always = QLabel("Always allowed", self.tree)
                    always.setStyleSheet(f"color:{c.success}; padding-left:4px;")
                    self.tree.setItemWidget(item, 2, always)
                else:
                    self.tree.setItemWidget(
                        item, 2, self._permission_combo(tool.name))
            layout.addWidget(self.tree, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

    def _permission_combo(self, tool_name: str) -> QComboBox:
        tool = self._manager.find_tool(self._server_id, tool_name)
        sensitivity = tool.sensitivity if tool is not None else Sensitivity.UNKNOWN
        return build_permission_combo(
            self._manager, self._server_id, tool_name, sensitivity, parent=self.tree)


class AddServerDialog(QDialog):
    """Name, transport, address, arguments, an optional secret. Nothing here
    is raw JSON - the config object is assembled from these fields."""

    def __init__(self, manager: McpConnectionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("Add MCP Server")
        self.resize(460, 420)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        def field_label(text: str) -> QLabel:
            label = QLabel(text, self)
            label.setStyleSheet(f"color:{c.text}; font-size:{m.text_sm}px; font-weight:600;")
            return label

        layout.addWidget(field_label("Name"))
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("e.g. GitHub")
        layout.addWidget(self.name_edit)

        layout.addWidget(field_label("Connect using"))
        transport_row = QHBoxLayout()
        self._transport_group = QButtonGroup(self)
        self.stdio_radio = QRadioButton(_TRANSPORT_LABELS[Transport.STDIO], self)
        self.http_radio = QRadioButton(_TRANSPORT_LABELS[Transport.STREAMABLE_HTTP], self)
        self.stdio_radio.setChecked(True)
        self._transport_group.addButton(self.stdio_radio)
        self._transport_group.addButton(self.http_radio)
        transport_row.addWidget(self.stdio_radio)
        transport_row.addWidget(self.http_radio)
        layout.addLayout(transport_row)
        self.stdio_radio.toggled.connect(self._sync_transport_fields)

        layout.addWidget(field_label("Command"))
        self.command_edit = QLineEdit(self)
        self.command_edit.setPlaceholderText("npx")
        layout.addWidget(self.command_edit)

        layout.addWidget(field_label("Arguments (space-separated)"))
        self.args_edit = QLineEdit(self)
        self.args_edit.setPlaceholderText("-y @example/mcp-server")
        layout.addWidget(self.args_edit)

        layout.addWidget(field_label("Address"))
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("https://example.com/mcp")
        layout.addWidget(self.url_edit)

        layout.addWidget(field_label("Secret (API key or token, optional)"))
        secret_row = QHBoxLayout()
        self.secret_edit = QLineEdit(self)
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Stored securely, never shown in plain text")
        secret_row.addWidget(self.secret_edit)
        layout.addLayout(secret_row)

        self.secret_target_edit = QLineEdit(self)
        self.secret_target_edit.setPlaceholderText(
            "Environment variable to pass it as, e.g. GITHUB_TOKEN")
        layout.addWidget(self.secret_target_edit)

        note = QLabel(
            "For a web address, the secret is sent as an Authorization header. "
            "For a local command, it is passed only to that command as the "
            "environment variable named above.", self)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{c.muted}; font-size:{m.text_xs}px;")
        layout.addWidget(note)

        self.problem = QLabel("", self)
        self.problem.setStyleSheet(f"color:{c.danger}; font-size:{m.text_sm}px;")
        self.problem.setWordWrap(True)
        layout.addWidget(self.problem)

        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setProperty("kind", "primary")
            save_button.setDefault(True)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_transport_fields()

    def _sync_transport_fields(self) -> None:
        is_stdio = self.stdio_radio.isChecked()
        self.command_edit.setEnabled(is_stdio)
        self.args_edit.setEnabled(is_stdio)
        self.url_edit.setEnabled(not is_stdio)
        self.secret_target_edit.setPlaceholderText(
            "Environment variable to pass it as, e.g. GITHUB_TOKEN" if is_stdio
            else "Header name, e.g. Authorization")

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.problem.setText("Give the server a name.")
            return
        is_stdio = self.stdio_radio.isChecked()
        server_id = mcp_config.slugify(name)
        existing = {s.id for s in self._manager.configured_servers()}
        if server_id in existing:
            suffix = 2
            while f"{server_id}-{suffix}" in existing:
                suffix += 1
            server_id = f"{server_id}-{suffix}"

        secret = self.secret_edit.text().strip()
        secret_target = self.secret_target_edit.text().strip()

        if is_stdio:
            command = self.command_edit.text().strip()
            if not command:
                self.problem.setText("Enter the command to run.")
                return
            args = tuple(self.args_edit.text().split())
            config = McpServerConfig(
                id=server_id, name=name, transport=Transport.STDIO,
                command=command, args=args,
                secret_env_vars=(secret_target,) if secret and secret_target else ())
        else:
            url = self.url_edit.text().strip()
            if not url:
                self.problem.setText("Enter the server's address.")
                return
            config = McpServerConfig(
                id=server_id, name=name, transport=Transport.STREAMABLE_HTTP,
                url=url, auth_header=secret_target if secret and secret_target else "")

        if secret:
            if not secret_target:
                self.problem.setText(
                    "Name the environment variable or header the secret should be sent as.")
                return
            try:
                mcp_config.set_secret(server_id, secret)
            except mcp_config.KeyringUnavailable:
                self.problem.setText(
                    "This system has no secure credential storage available, so the "
                    "secret could not be saved. The server was not added.")
                return
            except ValueError:
                pass  # an empty secret after stripping - treated as "no secret"

        self._manager.add_or_update_server(config)
        self.accept()


class GlobalPermissionsDialog(QDialog):
    """Every server's every tool, one table - "review permissions" without
    opening each server in turn. Server/Tool/Classification/Permission match
    the per-server ToolListDialog exactly (same build_permission_combo), plus
    a Scope column ToolListDialog has no room to need, since it is always
    looking at one server already."""

    def __init__(self, manager: McpConnectionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("MCP Permissions")
        self.resize(680, 460)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        intro = QLabel(
            "Every tool across every connected server, in one place. "
            "“Scope” shows how long a decision is remembered for - "
            "nothing here is remembered forever unless it says Always.", self)
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{c.muted}; font-size:{m.text_sm}px;")
        layout.addWidget(intro)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Server", "Tool", "Classification", "Permission", "Scope"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 140)
        self.tree.setColumnWidth(1, 110)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(m.space_2)
        self.reset_server_button = QPushButton("Reset selected server", self)
        self.reset_server_button.clicked.connect(self._reset_selected_server)
        self.reset_all_button = QPushButton("Reset all MCP permissions", self)
        self.reset_all_button.setProperty("kind", "danger")
        self.reset_all_button.clicked.connect(self._reset_all)
        actions_row.addWidget(self.reset_server_button)
        actions_row.addWidget(self.reset_all_button)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._manager.server_changed.connect(self._refresh)
        self._refresh()

    _SCOPE_LABELS = {Scope.ALWAYS: "Always", Scope.MISSION: "This Mission", "": "—"}

    def _refresh(self, *_args: Any) -> None:
        self.tree.clear()
        for config in self._manager.configured_servers():
            connection = self._manager.connection(config.id)
            tools = connection.tools if connection else []
            for tool in sorted(tools, key=lambda t: t.name):
                item = QTreeWidgetItem([
                    config.name, tool.name, describe_sensitivity(tool.sensitivity), "",
                    self._SCOPE_LABELS.get(
                        self._manager.permission_scope_for(config.id, tool.name), "—"),
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, config.id)
                self.tree.addTopLevelItem(item)
                if tool.never_confirmed:
                    always = QLabel("Always allowed", self.tree)
                    c = theme.palette_for(QApplication.instance())
                    always.setStyleSheet(f"color:{c.success}; padding-left:4px;")
                    self.tree.setItemWidget(item, 3, always)
                else:
                    self.tree.setItemWidget(
                        item, 3, build_permission_combo(
                            self._manager, config.id, tool.name, tool.sensitivity,
                            parent=self.tree))

    def _selected_server_id(self) -> str | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _reset_selected_server(self) -> None:
        server_id = self._selected_server_id()
        if not server_id:
            QMessageBox.information(self, "Reset permissions",
                                    "Select a row for the server you want to reset first.")
            return
        connection = self._manager.connection(server_id)
        name = connection.config.name if connection else server_id
        if not confirm_destructive_choice(
                self, "Reset permissions",
                f"Reset every remembered permission for “{name}”? "
                "Every tool goes back to its default (Ask, unless read-only).",
                "Reset"):
            return
        self._manager.reset_server_permissions(server_id)
        self._refresh()

    def _reset_all(self) -> None:
        if not confirm_destructive_choice(
                self, "Reset all MCP permissions",
                "Reset every remembered permission for every MCP server? "
                "Every tool goes back to its default (Ask, unless read-only).",
                "Reset all"):
            return
        self._manager.reset_all_permissions()
        self._refresh()


def confirm_destructive_choice(parent, title: str, text: str, confirm_label: str) -> bool:
    """A small local stand-in for app.ui.dialogs.confirm_destructive - not
    reused directly to avoid this module importing from app.ui.dialogs for
    one helper; the behaviour (Cancel is the default, the confirm button is
    styled danger) matches it exactly."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    confirm = box.addButton(confirm_label, QMessageBox.ButtonRole.DestructiveRole)
    confirm.setProperty("kind", "danger")
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is confirm


_DECISION_LABELS = {
    "auto": "Auto (read-only)",
    "allowed_remembered": "Allowed (remembered)",
    "allowed_once": "Allowed just now",
    "denied_remembered": "Denied (remembered)",
    "denied_once": "Denied just now",
}

_OUTCOME_LABELS = {
    "success": "Success",
    "error": "Error",
    "timeout": "Timed out",
    "not_executed": "Not executed",
}


class AuditLogDialog(QDialog):
    """The MCP activity/audit log: what ran, under which server, for which
    Mission, with what decision and outcome - never the arguments or
    results themselves. Filterable so a long history stays useful rather
    than becoming one huge scroll."""

    def __init__(self, manager: McpConnectionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("MCP Activity")
        self.resize(720, 480)
        m = theme.METRICS
        c = theme.palette_for(QApplication.instance())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        filters = QHBoxLayout()
        filters.setSpacing(m.space_2)

        self.server_filter = QComboBox(self)
        self.server_filter.addItem("All servers", None)
        for config in manager.configured_servers():
            self.server_filter.addItem(config.name, config.id)
        filters.addWidget(self.server_filter)

        self.decision_filter = QComboBox(self)
        self.decision_filter.addItem("Allowed or denied", None)
        self.decision_filter.addItem("Allowed only", True)
        self.decision_filter.addItem("Denied only", False)
        filters.addWidget(self.decision_filter)

        self.kind_filter = QComboBox(self)
        self.kind_filter.addItem("Read and write", None)
        self.kind_filter.addItem("Read-only", (Sensitivity.READ_ONLY,))
        self.kind_filter.addItem("Write / sensitive / destructive",
                                 (Sensitivity.WRITE, Sensitivity.SENSITIVE,
                                  Sensitivity.DESTRUCTIVE, Sensitivity.UNKNOWN))
        filters.addWidget(self.kind_filter)

        self.errors_only = QPushButton("Errors only", self)
        self.errors_only.setCheckable(True)
        filters.addWidget(self.errors_only)
        filters.addStretch(1)
        layout.addLayout(filters)

        for combo in (self.server_filter, self.decision_filter, self.kind_filter):
            combo.currentIndexChanged.connect(self._refresh)
        self.errors_only.toggled.connect(self._refresh)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(
            ["Time", "Server", "Tool", "Mission", "Decision", "Outcome", "Duration"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(1, 130)
        self.tree.setColumnWidth(4, 140)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        clear_row = QHBoxLayout()
        self.clear_button = QPushButton("Clear activity log", self)
        self.clear_button.setProperty("kind", "danger")
        self.clear_button.clicked.connect(self._clear)
        clear_row.addWidget(self.clear_button)
        clear_row.addStretch(1)
        layout.addLayout(clear_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh()

    def _refresh(self, *_args: Any) -> None:
        self.tree.clear()
        entries = self._manager.audit_entries(
            server_id=self.server_filter.currentData(),
            allowed=self.decision_filter.currentData(),
            sensitivity_in=self.kind_filter.currentData(),
            errors_only=self.errors_only.isChecked(),
            limit=300,
        )
        c = theme.palette_for(QApplication.instance())
        from PySide6.QtGui import QColor

        for entry in entries:
            when = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
            duration = f"{entry.duration_ms:.0f} ms" if entry.duration_ms is not None else "—"
            item = QTreeWidgetItem([
                when, entry.server_name, entry.tool_name,
                entry.mission_title or "—",
                _DECISION_LABELS.get(entry.decision, entry.decision),
                _OUTCOME_LABELS.get(entry.outcome, entry.outcome),
                duration,
            ])
            if entry.is_error:
                item.setForeground(5, QColor(c.danger))
            elif entry.allowed:
                item.setForeground(5, QColor(c.success))
            self.tree.addTopLevelItem(item)

    def _clear(self) -> None:
        if not confirm_destructive_choice(
                self, "Clear activity log",
                "Delete every recorded MCP activity entry? This cannot be undone.",
                "Clear"):
            return
        self._manager.clear_audit()
        self._refresh()

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

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from app.mcp.types import ConnectionState, McpServerConfig, Transport
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
            tool_count = connection.tool_count if connection else 0
            visible_count = len(connection.agent_visible_tools()) if connection else 0
            item = QTreeWidgetItem([
                config.name,
                _STATUS_LABELS.get(state, state),
                (f"{visible_count} available, {tool_count - visible_count} blocked"
                 if tool_count else "—"),
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
        ToolListDialog(connection.config.name, connection.tools, self).exec()

    def _add_server(self) -> None:
        dialog = AddServerDialog(self._manager, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh()


class ToolListDialog(QDialog):
    """Read-only: every tool a server offers, and why it is or isn't usable."""

    def __init__(self, server_name: str, tools, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Tools from {server_name}")
        self.resize(480, 360)
        c = theme.palette_for(QApplication.instance())
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_3)
        layout.setSpacing(m.space_2)

        if not tools:
            empty = QLabel("This server has not reported any tools yet.", self)
            empty.setStyleSheet(f"color:{c.muted};")
            layout.addWidget(empty)
        else:
            from PySide6.QtGui import QColor

            listw = QListWidget(self)
            for tool in sorted(tools, key=lambda t: t.name):
                available = tool.agent_visible
                row = QListWidgetItem(
                    f"{'✓' if available else '✗'}  {tool.name} — "
                    f"{describe_sensitivity(tool.sensitivity)}")
                row.setForeground(QColor(c.success if available else c.muted))
                listw.addItem(row)
            layout.addWidget(listw, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)


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

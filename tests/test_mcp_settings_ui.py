"""Tests for the "Connected Tools" Settings UI (app/ui/mcp_settings.py).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_settings_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from app.mcp.config import McpServerStore  # noqa: E402
from app.mcp.connection_manager import McpConnectionManager  # noqa: E402
from app.mcp.types import ConnectionState, McpServerConfig, Permission, Transport  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402
from app.ui.mcp_settings import (  # noqa: E402
    AddServerDialog,
    AuditLogDialog,
    ConnectedToolsPanel,
    GlobalPermissionsDialog,
)
from app.ui.settings_dialog import SettingsDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class McpUiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.settings = SettingsStore(self.db)
        self.store = McpServerStore(self.settings)
        self.manager = McpConnectionManager(self.store)

    def tearDown(self) -> None:
        self.manager.shutdown()
        _app.processEvents()
        os.unlink(self._tmp.name)


class SettingsDialogTabTests(McpUiTestCase):
    def test_no_mcp_manager_means_no_tabs(self):
        dialog = SettingsDialog(self.settings)
        self.assertIsNone(dialog.findChild(QTabWidget))
        dialog.deleteLater()

    def test_mcp_manager_adds_a_connected_tools_tab(self):
        dialog = SettingsDialog(self.settings, mcp=self.manager)
        tabs = dialog.findChild(QTabWidget)
        self.assertIsNotNone(tabs)
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        self.assertEqual(labels, ["General", "Connected Tools"])
        self.assertIsNotNone(dialog.findChild(ConnectedToolsPanel))
        dialog.deleteLater()


class ConnectedToolsPanelTests(McpUiTestCase):
    def test_lists_configured_servers(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        panel = ConnectedToolsPanel(self.manager)
        self.assertEqual(panel.tree.topLevelItemCount(), 1)
        self.assertEqual(panel.tree.topLevelItem(0).text(0), "Server One")
        panel.deleteLater()

    def test_refreshes_when_manager_reports_a_change(self):
        panel = ConnectedToolsPanel(self.manager)
        self.assertEqual(panel.tree.topLevelItemCount(), 0)
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        self.assertEqual(panel.tree.topLevelItemCount(), 1)
        panel.deleteLater()

    def test_disabled_server_shows_disabled_status(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true",
            enabled=False))
        panel = ConnectedToolsPanel(self.manager)
        self.assertIn("Disabled", panel.tree.topLevelItem(0).text(1))
        panel.deleteLater()

    def test_buttons_disabled_with_no_selection(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        panel = ConnectedToolsPanel(self.manager)
        self.assertFalse(panel.connect_button.isEnabled())
        self.assertFalse(panel.remove_button.isEnabled())
        panel.deleteLater()

    def test_selecting_a_disconnected_server_enables_connect_not_disconnect(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        panel = ConnectedToolsPanel(self.manager)
        panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
        self.assertTrue(panel.connect_button.isEnabled())
        self.assertFalse(panel.disconnect_button.isEnabled())
        self.assertEqual(panel.toggle_button.text(), "Disable")
        panel.deleteLater()

    def test_toggle_disables_and_re_enables(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        panel = ConnectedToolsPanel(self.manager)
        panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
        panel._toggle_selected()
        self.assertEqual(self.manager.state("s1"), ConnectionState.DISABLED)
        self.assertEqual(panel.toggle_button.text(), "Enable")
        panel.deleteLater()

    def test_remove_via_manager_clears_the_row(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        panel = ConnectedToolsPanel(self.manager)
        self.manager.remove_server("s1")
        self.assertEqual(panel.tree.topLevelItemCount(), 0)
        panel.deleteLater()


class AddServerDialogTests(McpUiTestCase):
    def test_empty_name_is_refused(self):
        dialog = AddServerDialog(self.manager)
        dialog._save()
        self.assertIn("name", dialog.problem.text().lower())
        self.assertEqual(self.manager.configured_servers(), [])
        dialog.deleteLater()

    def test_stdio_without_command_is_refused(self):
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("My Server")
        dialog._save()
        self.assertIn("command", dialog.problem.text().lower())
        dialog.deleteLater()

    def test_valid_stdio_server_is_added(self):
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("My Server")
        dialog.command_edit.setText("true")
        dialog.args_edit.setText("--flag value")
        dialog._save()
        servers = self.manager.configured_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].name, "My Server")
        self.assertEqual(servers[0].command, "true")
        self.assertEqual(servers[0].args, ("--flag", "value"))
        dialog.deleteLater()

    def test_http_without_address_is_refused(self):
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("Web Server")
        dialog.http_radio.setChecked(True)
        dialog._save()
        self.assertIn("address", dialog.problem.text().lower())
        dialog.deleteLater()

    def test_valid_http_server_is_added(self):
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("Web Server")
        dialog.http_radio.setChecked(True)
        dialog.url_edit.setText("https://example.com/mcp")
        dialog._save()
        servers = self.manager.configured_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].transport, Transport.STREAMABLE_HTTP)
        self.assertEqual(servers[0].url, "https://example.com/mcp")
        dialog.deleteLater()

    def test_duplicate_name_gets_a_disambiguated_id(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="my-server", name="My Server", transport=Transport.STDIO, command="true"))
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("My Server")
        dialog.command_edit.setText("true")
        dialog._save()
        ids = {s.id for s in self.manager.configured_servers()}
        self.assertEqual(ids, {"my-server", "my-server-2"})
        dialog.deleteLater()

    def test_secret_without_a_named_target_is_refused(self):
        dialog = AddServerDialog(self.manager)
        dialog.name_edit.setText("My Server")
        dialog.command_edit.setText("true")
        dialog.secret_edit.setText("hunter2")
        dialog._save()
        self.assertIn("name", dialog.problem.text().lower())
        self.assertEqual(self.manager.configured_servers(), [])
        dialog.deleteLater()


def _add_connected_tool(manager, server_id, tool_name, sensitivity):
    """A UI test only needs a tool to exist and be classified - not a real
    subprocess round trip (see test_mcp_phase3.py for that level)."""
    from app.mcp.types import ConnectionState, McpToolDescriptor

    connection = manager.connection(server_id)
    connection.state = ConnectionState.CONNECTED
    connection.tools = list(connection.tools) + [McpToolDescriptor(
        server_id=server_id, name=tool_name, description="", sensitivity=sensitivity)]


class GlobalPermissionsDialogTests(McpUiTestCase):
    def test_lists_every_servers_tools(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "get_thing", Sensitivity.READ_ONLY)
        _add_connected_tool(self.manager, "s1", "make_thing", Sensitivity.WRITE)
        dialog = GlobalPermissionsDialog(self.manager)
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        dialog.deleteLater()

    def test_read_only_tool_shows_always_allowed_with_no_control(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "get_thing", Sensitivity.READ_ONLY)
        dialog = GlobalPermissionsDialog(self.manager)
        item = dialog.tree.topLevelItem(0)
        widget = dialog.tree.itemWidget(item, 3)
        self.assertIsInstance(widget, __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)
        self.assertIn("Always allowed", widget.text())
        dialog.deleteLater()

    def test_changing_permission_from_the_global_view_persists(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "make_thing", Sensitivity.WRITE)
        dialog = GlobalPermissionsDialog(self.manager)
        item = dialog.tree.topLevelItem(0)
        combo = dialog.tree.itemWidget(item, 3)
        allow_index = combo.findData(Permission.ALLOW)
        combo.setCurrentIndex(allow_index)
        self.assertEqual(self.manager.permission_for("s1", "make_thing"), Permission.ALLOW)
        dialog.deleteLater()

    def test_destructive_tool_has_no_allow_option(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "delete_thing", Sensitivity.DESTRUCTIVE)
        dialog = GlobalPermissionsDialog(self.manager)
        item = dialog.tree.topLevelItem(0)
        combo = dialog.tree.itemWidget(item, 3)
        self.assertEqual(combo.findData(Permission.ALLOW), -1)
        dialog.deleteLater()

    def test_reset_selected_server_clears_its_permissions(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "make_thing", Sensitivity.WRITE)
        self.manager.set_tool_permission("s1", "make_thing", Permission.ALLOW)
        dialog = GlobalPermissionsDialog(self.manager)
        dialog.tree.topLevelItem(0).setSelected(True)
        from unittest import mock
        with mock.patch("app.ui.mcp_settings.confirm_destructive_choice", return_value=True):
            dialog._reset_selected_server()
        self.assertEqual(self.manager.permission_for("s1", "make_thing"), Permission.ASK)
        dialog.deleteLater()

    def test_reset_all_clears_every_server(self):
        from app.mcp.types import Sensitivity

        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        self.manager.add_or_update_server(McpServerConfig(
            id="s2", name="Server Two", transport=Transport.STDIO, command="true"))
        _add_connected_tool(self.manager, "s1", "make_thing", Sensitivity.WRITE)
        _add_connected_tool(self.manager, "s2", "make_thing", Sensitivity.WRITE)
        self.manager.set_tool_permission("s1", "make_thing", Permission.ALLOW)
        self.manager.set_tool_permission("s2", "make_thing", Permission.DENY)
        dialog = GlobalPermissionsDialog(self.manager)
        from unittest import mock
        with mock.patch("app.ui.mcp_settings.confirm_destructive_choice", return_value=True):
            dialog._reset_all()
        self.assertEqual(self.manager.permission_for("s1", "make_thing"), Permission.ASK)
        self.assertEqual(self.manager.permission_for("s2", "make_thing"), Permission.ASK)
        dialog.deleteLater()


class AuditLogDialogTests(McpUiTestCase):
    def test_shows_recorded_entries(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        self.manager._audit.record(
            server_id="s1", server_name="Server One", tool_name="get_thing",
            sensitivity="read_only", mission_id=None, mission_title="",
            decision="auto", outcome="success", duration_ms=5.0)
        dialog = AuditLogDialog(self.manager)
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        dialog.deleteLater()

    def test_errors_only_filter(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        self.manager._audit.record(
            server_id="s1", server_name="Server One", tool_name="get_thing",
            sensitivity="read_only", mission_id=None, mission_title="",
            decision="auto", outcome="success", duration_ms=5.0)
        self.manager._audit.record(
            server_id="s1", server_name="Server One", tool_name="get_other",
            sensitivity="read_only", mission_id=None, mission_title="",
            decision="auto", outcome="error", duration_ms=5.0)
        dialog = AuditLogDialog(self.manager)
        dialog.errors_only.setChecked(True)
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        dialog.deleteLater()

    def test_clear_empties_the_log(self):
        self.manager.add_or_update_server(McpServerConfig(
            id="s1", name="Server One", transport=Transport.STDIO, command="true"))
        self.manager._audit.record(
            server_id="s1", server_name="Server One", tool_name="get_thing",
            sensitivity="read_only", mission_id=None, mission_title="",
            decision="auto", outcome="success", duration_ms=5.0)
        dialog = AuditLogDialog(self.manager)
        from unittest import mock
        with mock.patch("app.ui.mcp_settings.confirm_destructive_choice", return_value=True):
            dialog._clear()
        self.assertEqual(dialog.tree.topLevelItemCount(), 0)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

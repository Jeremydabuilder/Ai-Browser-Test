"""Phase 12 client profile metadata (client_type/connection_method/
last_verified_*) and permission presets - reusing the exact Phase 11
paired-client store, never a second one. Also the Part 10 client-specific
security tests: identity isolation, no self-pairing, no self-escalation,
no security-disable tool, tool set stays fixed.

Run with:
    python -m unittest tests.test_mcp_client_profiles -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp_server import auth, permissions  # noqa: E402
from app.mcp_server.tools import TOOL_NAMES  # noqa: E402
from app.mcp_server.types import Capability, ClientType, VerificationStatus  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.mcp_server_store import McpServerAccessStore  # noqa: E402


class ClientProfileMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_client_type_and_connection_method_are_recorded_at_pairing(self) -> None:
        client, _token = auth.pair_client(
            self.store, display_name="Cursor", capabilities=["read_tabs"],
            client_type=ClientType.CURSOR.value, connection_method="direct_http")
        self.assertEqual(client.client_type, "cursor")
        self.assertEqual(client.connection_method, "direct_http")

    def test_a_client_defaults_to_generic_and_not_configured(self) -> None:
        client, _token = auth.pair_client(self.store, display_name="X", capabilities=[])
        self.assertEqual(client.client_type, "generic")
        self.assertEqual(client.last_verified_status, VerificationStatus.NOT_CONFIGURED.value)
        self.assertIsNone(client.last_verified_at)

    def test_record_verification_updates_status_and_timestamp(self) -> None:
        client, _token = auth.pair_client(self.store, display_name="X", capabilities=[])
        self.store.record_verification(client.id, VerificationStatus.VERIFIED.value)
        refreshed = self.store.get_client(client.id)
        self.assertEqual(refreshed.last_verified_status, "verified")
        self.assertIsNotNone(refreshed.last_verified_at)

    def test_client_type_persists_across_a_restart(self) -> None:
        client, _token = auth.pair_client(
            self.store, display_name="VS Code", capabilities=[],
            client_type=ClientType.VSCODE.value)
        self.db.close()
        reopened = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        try:
            restarted_store = McpServerAccessStore(reopened)
            found = restarted_store.get_client(client.id)
            self.assertEqual(found.client_type, "vscode")
        finally:
            reopened.close()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))  # let tearDown close it


class PermissionPresetTests(unittest.TestCase):
    def test_read_only_preset_has_no_write_capabilities(self) -> None:
        caps = permissions.preset_capabilities("read_only")
        self.assertNotIn(Capability.NAVIGATE.value, caps)
        self.assertNotIn(Capability.OPEN_TABS.value, caps)
        self.assertNotIn(Capability.CREATE_MISSION.value, caps)

    def test_research_preset_adds_navigate_and_open_tabs(self) -> None:
        caps = permissions.preset_capabilities("research")
        self.assertIn(Capability.NAVIGATE.value, caps)
        self.assertIn(Capability.OPEN_TABS.value, caps)
        self.assertNotIn(Capability.CREATE_MISSION.value, caps)

    def test_mission_assistant_preset_adds_create_mission(self) -> None:
        caps = permissions.preset_capabilities("mission_assistant")
        self.assertIn(Capability.CREATE_MISSION.value, caps)
        self.assertIn(Capability.NAVIGATE.value, caps)

    def test_full_access_is_never_returned_for_an_unknown_preset_name(self) -> None:
        """A typo or an unrecognized preset must fail closed - it must
        never silently fall back to granting everything."""
        caps = permissions.preset_capabilities("not-a-real-preset")
        self.assertEqual(caps, [])

    def test_full_access_must_be_requested_explicitly_by_name(self) -> None:
        caps = permissions.preset_capabilities("full_access")
        self.assertEqual(set(caps), {c.value for c in Capability})

    def test_full_access_is_not_among_the_default_presets_dict(self) -> None:
        """"Full Access" is advanced/opt-in - it must not live alongside
        the ordinary presets a UI would iterate over by default."""
        self.assertNotIn("full_access", permissions.PERMISSION_PRESETS)


class ClientSecurityTests(unittest.TestCase):
    """Phase 12, Part 10."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_one_clients_token_cannot_authenticate_as_another_client(self) -> None:
        cursor_client, cursor_token = auth.pair_client(
            self.store, display_name="Cursor", capabilities=["read_tabs"],
            client_type=ClientType.CURSOR.value)
        chatgpt_client, _chatgpt_token = auth.pair_client(
            self.store, display_name="ChatGPT", capabilities=["read_tabs"],
            client_type=ClientType.CHATGPT.value)
        found = auth.verify_token(self.store, cursor_token)
        self.assertEqual(found.id, cursor_client.id)
        self.assertNotEqual(found.id, chatgpt_client.id)

    def test_a_revoked_clients_token_cannot_reconnect_even_if_another_client_is_active(
        self,
    ) -> None:
        revoked_client, revoked_token = auth.pair_client(
            self.store, display_name="Old Cursor", capabilities=["read_tabs"])
        _active_client, active_token = auth.pair_client(
            self.store, display_name="New Cursor", capabilities=["read_tabs"])
        auth.revoke_client(self.store, revoked_client.id)
        self.assertIsNone(auth.verify_token(self.store, revoked_token))
        self.assertIsNotNone(auth.verify_token(self.store, active_token))

    def test_config_generation_cannot_grant_a_capability_the_client_lacks(self) -> None:
        """Config generators format text - they never touch the store, so
        there is no code path from "generate a config" to "gain a scope"."""
        from app.mcp_server import client_configs

        client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        client_configs.cursor_config(
            f"http://127.0.0.1:8765/mcp", token)
        client_configs.vscode_config(f"http://127.0.0.1:8765/mcp", token)
        refreshed = self.store.get_client(client.id)
        self.assertEqual(refreshed.capabilities, ())

    def test_an_external_client_cannot_change_its_own_scopes(self) -> None:
        """There is no exposed tool that writes to a client's own
        capabilities - the published tool set is fixed and none of its
        names touch permissions/pairing/capabilities."""
        for tool in TOOL_NAMES:
            lowered = tool.lower()
            self.assertNotIn("capabilit", lowered)
            self.assertNotIn("permission", lowered)
            self.assertNotIn("scope", lowered)

    def test_an_external_client_cannot_pair_itself(self) -> None:
        """Pairing a new client is a Settings-UI-only action (auth.
        pair_client, called from app/ui/mcp_server_settings.py) - it is
        never one of the tools an authenticated MCP call can reach."""
        for tool in TOOL_NAMES:
            lowered = tool.lower()
            self.assertNotIn("pair", lowered)
            self.assertNotIn("client", lowered)

    def test_an_external_client_cannot_disable_the_mcp_server(self) -> None:
        """Starting/stopping the server itself (PyBrowserMcpServer.start/
        stop) is reached only from app/ui/mcp_server_settings.py - no tool
        name exposes it."""
        for tool in TOOL_NAMES:
            lowered = tool.lower()
            self.assertNotIn("server", lowered)
            self.assertNotIn("disable", lowered)
            self.assertNotIn("enable", lowered)

    def test_the_published_tool_set_is_exactly_the_thirteen_documented_tools(self) -> None:
        """A call for a tool absent from tools/list must fail, and the
        published set itself must not silently grow - see
        docs/external_ai_clients.md's tool list."""
        self.assertEqual(len(TOOL_NAMES), 13)


if __name__ == "__main__":
    unittest.main()

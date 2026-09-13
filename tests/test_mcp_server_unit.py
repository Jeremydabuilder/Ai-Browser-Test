"""PyBrowser-as-MCP-Server (Phase 11): the non-Qt layers - persistence,
pairing/auth, and capability enforcement. See test_mcp_server_tools.py for
the tool dispatcher and test_mcp_server_integration.py for the real HTTP
server and a genuine protocol-level client.

Run with:
    python -m unittest tests.test_mcp_server_unit -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp_server import auth, permissions  # noqa: E402
from app.mcp_server.audit import record_call  # noqa: E402
from app.mcp_server.types import Capability, PairedClient  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.mcp_server_store import McpServerAccessStore  # noqa: E402


class PairingAndAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_newly_paired_client_has_only_the_capabilities_it_was_given(self) -> None:
        client, _token = auth.pair_client(self.store, display_name="ChatGPT",
                                         capabilities=["read_pages"])
        self.assertEqual(client.capabilities, ("read_pages",))
        self.assertFalse(client.has(Capability.NAVIGATE))

    def test_pairing_with_no_capabilities_grants_none_by_default(self) -> None:
        client, _token = auth.pair_client(self.store, display_name="Claude Desktop",
                                         capabilities=[])
        self.assertEqual(client.capabilities, ())

    def test_the_plaintext_token_is_never_persisted(self) -> None:
        _client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        rows = self.db.query("SELECT * FROM mcp_server_clients")
        for row in rows:
            self.assertNotIn(token, row["token_hash"])
            self.assertNotEqual(row["token_hash"], token)

    def test_verify_token_finds_the_paired_client(self) -> None:
        client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        found = auth.verify_token(self.store, token)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, client.id)

    def test_an_unknown_token_is_rejected(self) -> None:
        self.assertIsNone(auth.verify_token(self.store, "not-a-real-token"))

    def test_an_empty_token_is_rejected(self) -> None:
        self.assertIsNone(auth.verify_token(self.store, ""))

    def test_a_revoked_client_cannot_reconnect(self) -> None:
        client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        auth.revoke_client(self.store, client.id)
        self.assertIsNone(auth.verify_token(self.store, token))

    def test_revoking_one_client_does_not_affect_another(self) -> None:
        client_a, token_a = auth.pair_client(self.store, display_name="A", capabilities=[])
        _client_b, token_b = auth.pair_client(self.store, display_name="B", capabilities=[])
        auth.revoke_client(self.store, client_a.id)
        self.assertIsNone(auth.verify_token(self.store, token_a))
        self.assertIsNotNone(auth.verify_token(self.store, token_b))

    def test_paired_clients_persist_across_a_restart(self) -> None:
        """A fresh McpServerAccessStore over the same database file must see
        clients paired before the "restart" - this is what makes losing the
        in-memory Phase-9-style limitation not apply here."""
        client, token = auth.pair_client(self.store, display_name="Persisted",
                                         capabilities=["read_pages"])
        self.db.close()
        reopened = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        try:
            restarted_store = McpServerAccessStore(reopened)
            found = auth.verify_token(restarted_store, token)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, client.id)
            self.assertEqual(found.display_name, "Persisted")
        finally:
            reopened.close()
        # Re-open a handle so tearDown's close() call is harmless.
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))


class CapabilityScopeTests(unittest.TestCase):
    def test_every_tool_maps_to_a_known_capability(self) -> None:
        from app.mcp_server.tools import TOOL_NAMES

        for tool in TOOL_NAMES:
            self.assertIsNotNone(permissions.required_capability(tool), tool)

    def test_a_client_without_the_capability_is_denied(self) -> None:
        client = PairedClient(id="c1", display_name="X", token_hash="h", capabilities=())
        self.assertFalse(permissions.is_permitted(client, "browser.navigate"))

    def test_a_client_with_the_capability_is_permitted(self) -> None:
        client = PairedClient(id="c1", display_name="X", token_hash="h",
                              capabilities=("navigate",))
        self.assertTrue(permissions.is_permitted(client, "browser.navigate"))

    def test_an_unknown_tool_is_never_permitted(self) -> None:
        client = PairedClient(id="c1", display_name="X", token_hash="h",
                              capabilities=tuple(c.value for c in Capability))
        self.assertFalse(permissions.is_permitted(client, "browser.eval_js"))

    def test_no_tool_grants_arbitrary_javascript_click_type_or_submit(self) -> None:
        """The Phase 11 spec's explicit exclusion list - never present in the
        tool set at all, regardless of capability."""
        from app.mcp_server.tools import TOOL_NAMES

        forbidden_substrings = ("eval", "javascript", "click", "type", "submit", "download",
                                "file", "exec")
        for tool in TOOL_NAMES:
            lowered = tool.lower()
            for substring in forbidden_substrings:
                self.assertNotIn(substring, lowered, f"{tool} looks like an excluded capability")


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_record_call_writes_a_retrievable_entry(self) -> None:
        record_call(self.store, client_id="c1", tool="browser.navigate", outcome="ok",
                   duration_ms=42, approval_result="approved", detail="")
        entries = self.store.recent_audit()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].tool, "browser.navigate")
        self.assertEqual(entries[0].outcome, "ok")
        self.assertEqual(entries[0].duration_ms, 42)

    def test_a_secret_passed_as_detail_is_never_silently_widened(self) -> None:
        """record_call truncates aggressively - a caller that accidentally
        passed something huge (a page body, say) does not get to blow past
        the audit log's own size discipline."""
        huge = "x" * 100_000
        record_call(self.store, client_id="c1", tool="browser.read_page", outcome="ok",
                   duration_ms=1, detail=huge)
        entry = self.store.recent_audit()[0]
        self.assertLess(len(entry.detail), 300)

    def test_recent_audit_orders_newest_first(self) -> None:
        record_call(self.store, client_id="c1", tool="a", outcome="ok", duration_ms=1)
        record_call(self.store, client_id="c1", tool="b", outcome="ok", duration_ms=1)
        entries = self.store.recent_audit()
        self.assertEqual(entries[0].tool, "b")


if __name__ == "__main__":
    unittest.main()

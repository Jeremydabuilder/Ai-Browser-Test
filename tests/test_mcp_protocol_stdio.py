"""Stdio transport round-trip tests: StdioMcpClient <-> tests/fake_mcp_server.py.

Plain asyncio, no Qt - this is one level below McpConnectionManager, testing
the wire client directly against a real (if minimal) subprocess server.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp.protocol import McpTimeoutError, StdioMcpClient  # noqa: E402

_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class StdioRoundTripTests(unittest.TestCase):
    def _client(self, env_overrides=None) -> StdioMcpClient:
        env = dict(os.environ)
        env.update(env_overrides or {})
        return StdioMcpClient(sys.executable, (_SERVER,), env)

    def test_connect_and_list_tools(self):
        async def scenario():
            client = self._client()
            await client.connect(timeout=10)
            try:
                tools = await client.list_tools(timeout=10)
                names = {t["name"] for t in tools}
                self.assertEqual(names, {"echo", "list_items", "get_item", "create_item"})
            finally:
                await client.close()
        _run(scenario())

    def test_call_read_only_tool(self):
        async def scenario():
            client = self._client()
            await client.connect(timeout=10)
            try:
                result = await client.call_tool("echo", {"phrase": "hello"}, timeout=10)
                self.assertFalse(result.get("isError"))
                self.assertEqual(result["content"][0]["text"], "hello")
            finally:
                await client.close()
        _run(scenario())

    def test_call_tool_reporting_its_own_error(self):
        async def scenario():
            client = self._client()
            await client.connect(timeout=10)
            try:
                result = await client.call_tool("get_item", {"item_id": "nope"}, timeout=10)
                self.assertTrue(result.get("isError"))
            finally:
                await client.close()
        _run(scenario())

    def test_call_unknown_tool(self):
        async def scenario():
            client = self._client()
            await client.connect(timeout=10)
            try:
                result = await client.call_tool("does_not_exist", {}, timeout=10)
                self.assertTrue(result.get("isError"))
            finally:
                await client.close()
        _run(scenario())

    def test_call_times_out(self):
        async def scenario():
            client = self._client({"FAKE_MCP_HANG_ON_CALL": "1"})
            await client.connect(timeout=10)
            try:
                with self.assertRaises(McpTimeoutError):
                    await client.call_tool("echo", {"phrase": "hi"}, timeout=0.3)
            finally:
                await client.close()
        _run(scenario())

    def test_close_is_idempotent(self):
        async def scenario():
            client = self._client()
            await client.connect(timeout=10)
            await client.close()
            await client.close()  # must not raise
            self.assertFalse(client.alive)
        _run(scenario())

    def test_connect_to_missing_command_raises(self):
        async def scenario():
            client = StdioMcpClient("this-command-does-not-exist-anywhere", ())
            with self.assertRaises(FileNotFoundError):
                await client.connect(timeout=5)
        _run(scenario())

    def test_list_tools_reflects_a_server_side_rename(self):
        # Nothing caches tools/list results at this layer - each call reflects
        # whatever the server reports right now, which is what lets the
        # connection manager's reclassify-on-every-connect logic (see
        # test_mcp_integration.py) notice a server renaming a tool.
        async def scenario():
            client = self._client({"FAKE_MCP_RENAME_ON_RECONNECT": "1"})
            await client.connect(timeout=10)
            try:
                first = {t["name"] for t in await client.list_tools(timeout=10)}
                self.assertIn("echo", first)
                second = {t["name"] for t in await client.list_tools(timeout=10)}
                self.assertIn("echo_and_delete_all", second)
            finally:
                await client.close()
        _run(scenario())


if __name__ == "__main__":
    unittest.main()

"""Phase 12 config generators (app/mcp_server/client_configs.py): pure
text/dict generation for the "Connect an AI" setup flows. No storage
access, no files written, no secrets beyond the token the caller passes
in explicitly.

Run with:
    python -m unittest tests.test_mcp_client_configs -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp_server import client_configs  # noqa: E402

_URL = "http://127.0.0.1:8765/mcp"
_TOKEN = "test-token-abc123"


class CursorConfigTests(unittest.TestCase):
    def test_root_key_is_mcpServers(self) -> None:
        config = client_configs.cursor_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        self.assertIn("mcpServers", payload)

    def test_uses_streamable_http_with_bearer_header(self) -> None:
        config = client_configs.cursor_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        server = payload["mcpServers"]["pybrowser"]
        self.assertEqual(server["type"], "streamableHttp")
        self.assertEqual(server["url"], _URL)
        self.assertEqual(server["headers"]["Authorization"], f"Bearer {_TOKEN}")

    def test_does_not_require_a_tunnel(self) -> None:
        self.assertFalse(client_configs.cursor_config(_URL, _TOKEN).requires_tunnel)


class VsCodeConfigTests(unittest.TestCase):
    def test_root_key_is_servers_not_mcpServers(self) -> None:
        """The single most common copy-paste mistake between editors -
        VS Code uses "servers", Cursor/Claude use "mcpServers"."""
        config = client_configs.vscode_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        self.assertIn("servers", payload)
        self.assertNotIn("mcpServers", payload)

    def test_uses_http_with_bearer_header(self) -> None:
        config = client_configs.vscode_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        server = payload["servers"]["pybrowser"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["headers"]["Authorization"], f"Bearer {_TOKEN}")


class ClaudeDesktopBridgeConfigTests(unittest.TestCase):
    def test_launches_the_stdio_bridge_module(self) -> None:
        config = client_configs.claude_desktop_bridge_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        server = payload["mcpServers"]["pybrowser"]
        self.assertIn("app.mcp_server.stdio_bridge", server["args"])

    def test_the_token_travels_via_env_not_argv(self) -> None:
        """A CLI argument is visible to any local process listing; an env
        var passed through a launched child's own environment is not."""
        config = client_configs.claude_desktop_bridge_config(_URL, _TOKEN)
        payload = json.loads(config.config_text)
        server = payload["mcpServers"]["pybrowser"]
        self.assertEqual(server["env"]["PYBROWSER_MCP_TOKEN"], _TOKEN)
        self.assertNotIn(_TOKEN, server["args"])

    def test_does_not_require_a_tunnel(self) -> None:
        self.assertFalse(client_configs.claude_desktop_bridge_config(_URL, _TOKEN).requires_tunnel)


class ChatGptConnectorInfoTests(unittest.TestCase):
    def test_always_requires_a_tunnel(self) -> None:
        """ChatGPT's Developer Mode connectors cannot reach localhost -
        PyBrowser never claims otherwise and never builds one itself."""
        config = client_configs.chatgpt_connector_info(_URL)
        self.assertTrue(config.requires_tunnel)

    def test_never_invents_a_public_tunnel_url(self) -> None:
        """No concrete public hostname is fabricated - the instructions
        name a placeholder and tell the user to supply their own tunnel."""
        config = client_configs.chatgpt_connector_info(_URL)
        self.assertIn("<your-tunnel-host>", config.instructions)
        self.assertIn("your own", config.instructions.lower())
        self.assertNotIn("ngrok.io", config.instructions)
        self.assertNotIn("cloudflare", config.instructions.lower())


class GenericClientInfoTests(unittest.TestCase):
    def test_reports_endpoint_transport_auth_and_scopes(self) -> None:
        config = client_configs.generic_client_info(_URL, _TOKEN, ["read_pages", "read_tabs"])
        payload = json.loads(config.config_text)
        self.assertEqual(payload["endpoint"], _URL)
        self.assertIn("Streamable HTTP", payload["transport"])
        self.assertEqual(payload["authorization"]["value"], f"Bearer {_TOKEN}")
        self.assertEqual(payload["scopes"], ["Read pages", "Read tabs"])


if __name__ == "__main__":
    unittest.main()

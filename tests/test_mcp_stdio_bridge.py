"""The optional stdio-to-HTTP bridge (app/mcp_server/stdio_bridge.py, Phase
12 Part 11) - contains no browser/Mission logic, only transport adaptation
and auth attachment. Tested against a real running PyBrowserMcpServer so
the round trip is genuine.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_stdio_bridge -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.mcp_server import auth  # noqa: E402
from app.mcp_server.server import PyBrowserMcpServer  # noqa: E402
from app.mcp_server.stdio_bridge import forward_line, run  # noqa: E402
from app.storage import Database, McpServerAccessStore  # noqa: E402

_app: QApplication | None = None
_next_port = [8960]


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _free_port() -> int:
    _next_port[0] += 1
    return _next_port[0]


class ForwardLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, port=_free_port())
        self.assertTrue(self.server.start())
        _client, self.token = auth.pair_client(self.store, display_name="X", capabilities=[])

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def _url(self) -> str:
        return f"http://{self.server.host}:{self.server.port}/mcp"

    def test_forwards_a_request_and_returns_the_real_response(self) -> None:
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        response = json.loads(forward_line(self._url(), self.token, line))
        self.assertEqual(response["id"], 1)
        self.assertIn("protocolVersion", response["result"])

    def test_an_invalid_token_still_forwards_but_the_server_refuses_it(self) -> None:
        line = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response = json.loads(forward_line(self._url(), "garbage-token", line))
        self.assertIn("error", response)

    def test_an_unreachable_server_returns_a_synthetic_error_not_a_crash(self) -> None:
        line = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
        response = json.loads(forward_line("http://127.0.0.1:1/mcp", self.token, line, timeout=1.0))
        self.assertEqual(response["id"], 3)
        self.assertIn("error", response)

    def test_run_forwards_every_line_from_stdin_to_stdout(self) -> None:
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        ]
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        run(self._url(), self.token, in_stream=stdin, out_stream=stdout)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["id"], 2)


if __name__ == "__main__":
    unittest.main()

"""scripts/team_live_check.py against a LOCAL OpenAI-compatible server.

This is not a live Groq test - no real provider is contacted - but it drives
the real GroqClient over a real HTTP socket through the script's whole path:
credential lookup, preflight, a full Coordinator -> Researcher -> Writer ->
Reviewer mission, and the BLOCKED outcomes (bad key, unreachable host).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_team_live_check -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.agent.openai_compatible import GroqClient  # noqa: E402

KEY = "gsk_local_fake_key_0123456789abcdef"
PLAN = json.dumps({"summary": "s", "success_criteria": ["Recommends one widget"], "tasks": [
    {"id": "T1", "title": "Read both pages", "agent": "researcher", "instructions": "extract", "depends_on": [],
     "sources": ["S1", "S2"]},
    {"id": "T2", "title": "Write the recommendation", "agent": "writer", "instructions": "write", "depends_on": ["T1"]},
    {"id": "T3", "title": "Review", "agent": "reviewer", "instructions": "check", "depends_on": ["T2"]}]})


def reply_for(system: str) -> str:
    if "ROLE: Coordinator. Turn" in system:
        return PLAN
    if "ROLE: Researcher" in system:
        return "- A: $10, 2 years [S1]\n- B: $14, 5 years, 90 day returns [S2]"
    if "ROLE: Writer" in system:
        return "Buy Widget B: it lasts 5 years [S2] versus 2 [S1]."
    if "ROLE: Reviewer" in system:
        return json.dumps({"verdict": "approve", "summary": "Sound.", "issues": []})
    return "**Recommendation:** Widget B. [S2]"


class Handler(BaseHTTPRequestHandler):
    seen_auth: list[str] = []
    requests: list[dict] = []

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen_auth.append(self.headers.get("Authorization", ""))
        Handler.requests.append(body)
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._send(401, {"error": {"message": "Invalid API Key"}})
        system = body["messages"][0]["content"] if body["messages"][0]["role"] == "system" else ""
        text = reply_for(system) if system else "ready"
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 20, "completion_tokens": 9}})

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def load_script():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "team_live_check.py")
    spec = importlib.util.spec_from_file_location("team_live_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(env: dict) -> tuple[int, str]:
    script = load_script()
    out = io.StringIO()
    clean = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY",)}
    clean.update({"PYBROWSER_DISABLE_KEYRING": "1", **env})
    with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(sys, "argv", ["team_live_check.py"]), \
            contextlib.redirect_stdout(out):
        code = script.main()
    return code, out.getvalue()


class LiveCheckScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/openai/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        Handler.seen_auth.clear()
        Handler.requests.clear()

    def test_no_credential_is_reported_as_blocked_and_nothing_is_sent(self) -> None:
        code, output = run_script({})
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED - no usable credential", output)
        self.assertIn("GROQ_API_KEY", output)
        self.assertEqual(Handler.requests, [])

    def test_a_working_key_runs_the_full_four_agent_mission_over_real_http(self) -> None:
        with mock.patch.object(GroqClient, "base_url", self.base):
            code, output = run_script({"GROQ_API_KEY": KEY})
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT   : PASS", output)
        for agent in ("researcher", "writer", "reviewer"):
            self.assertRegex(output, rf"{agent}\s+done")
        self.assertIn("Preflight: ok", output)
        self.assertIn("Recommendation", output)
        self.assertNotIn(KEY, output)                                    # the key is never printed
        self.assertTrue(set(Handler.seen_auth) == {f"Bearer {KEY}"})     # ...but it is what authenticated the calls
        system_prompts = [r["messages"][0]["content"] for r in Handler.requests if r["messages"][0]["role"] == "system"]
        roles = [next((n for m, n in (("Coordinator. Turn", "plan"), ("Researcher", "researcher"), ("Writer", "writer"),
                                      ("Reviewer", "reviewer"), ("assembling", "final")) if m in p), "?")
                 for p in system_prompts]
        self.assertEqual(roles, ["plan", "researcher", "writer", "reviewer", "final"])
        self.assertTrue(all("tools" not in r for r in Handler.requests[1:]))   # the team's calls offer no tools

    def test_a_rejected_key_is_blocked_at_preflight(self) -> None:
        with mock.patch.object(GroqClient, "base_url", self.base):
            code, output = run_script({"GROQ_API_KEY": "gsk_wrong_key_999999999999"})
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED - the provider rejected the request", output)
        self.assertNotIn("gsk_wrong_key", output)

    def test_an_unreachable_provider_is_blocked_not_failed(self) -> None:
        with mock.patch.object(GroqClient, "base_url", "http://127.0.0.1:9/openai/v1"):
            code, output = run_script({"GROQ_API_KEY": KEY})
        self.assertEqual(code, 2)
        self.assertIn("could not be reached from this machine", output)
        self.assertNotIn(KEY, output)


if __name__ == "__main__":
    unittest.main()

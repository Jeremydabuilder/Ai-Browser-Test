"""Tests for the API preflight script.

The preflight is the only check that talks to the real API, so nothing else can
catch it being wrong - and a preflight that reports green because its own
conversation walker is broken is worse than no preflight at all. These tests run
it against a scripted SDK client and assert the two things that matter: that the
conversation it builds is well formed, and that a rejection anywhere in that
conversation is reported as a failure rather than swallowed.

Run with:
    python -m unittest tests.test_preflight -v
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import api_preflight  # noqa: E402
from app.agent.claude_client import ClaudeClient  # noqa: E402
from app.agent.credentials import Credential, Mode  # noqa: E402


class _Block:
    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


def _text(body: str) -> _Block:
    return _Block(type="text", text=body)


def _tool_use(call_id: str, name: str) -> _Block:
    return _Block(type="tool_use", id=call_id, name=name, input={"url": "https://example.com"})


class _Api:
    """A scripted stand-in for the SDK client, recording every request."""

    def __init__(self, script) -> None:
        self.calls: list[dict] = []
        self._script = list(script)
        self.messages = self
        self.beta = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._script.pop(0) if self._script else []
        if isinstance(step, Exception):
            raise step
        return _Block(content=step, stop_reason="end_turn", usage=_Block(
            input_tokens=10, output_tokens=5))


@contextlib.contextmanager
def _api(script):
    """Run the preflight against a scripted API, and hand back the recorder."""
    recorder = _Api(script)
    real = api_preflight.ClaudeClient

    def build(credential, config):
        client = real(credential, config)
        client._client = recorder
        return client

    api_preflight.ClaudeClient = build
    try:
        yield recorder
    finally:
        api_preflight.ClaudeClient = real


CREDENTIAL = Credential(Mode.ENV_KEY, "test key", secret="sk-test")


def _run(script) -> tuple[bool, str, _Api]:
    with _api(script) as recorder:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = api_preflight.check("claude-opus-5", CREDENTIAL)
    return ok, out.getvalue(), recorder


class ConversationShapeTests(unittest.TestCase):
    """The messages the preflight sends must be a conversation the API accepts."""

    SCRIPT = [
        [_tool_use("toolu_1", "browser_open_tab")],   # opening turn asks for a tool
        [_text("The title is Example Domain.")],      # answers once the result arrives
        [_text("ready")],                             # answers the follow-up
    ]

    def test_a_healthy_conversation_passes(self) -> None:
        ok, output, recorder = _run(self.SCRIPT)
        self.assertTrue(ok, output)
        self.assertEqual(len(recorder.calls), 3, "one request per conversation turn")

    def test_roles_alternate_and_every_turn_has_content(self) -> None:
        _, _, recorder = _run(self.SCRIPT)
        for request in recorder.calls:
            roles = [message["role"] for message in request["messages"]]
            self.assertEqual(roles[0], "user", "a conversation opens with the user")
            self.assertEqual(roles[-1], "user", "and the model answers last")
            for before, after in zip(roles, roles[1:]):
                self.assertNotEqual(before, after, f"roles must alternate, got {roles}")
            for message in request["messages"]:
                self.assertTrue(message["content"])

    def test_every_tool_use_is_answered_by_a_tool_result(self) -> None:
        _, _, recorder = _run(self.SCRIPT)
        messages = recorder.calls[-1]["messages"]
        wanted, answered = [], []
        for message in messages:
            for block in message["content"] if isinstance(message["content"], list) else []:
                kind = getattr(block, "type", None) or (
                    block.get("type") if isinstance(block, dict) else None)
                if kind == "tool_use":
                    wanted.append(block.id)
                elif kind == "tool_result":
                    answered.append(block["tool_use_id"])
        self.assertTrue(wanted, "the script should have produced a tool call")
        self.assertEqual(wanted, answered)

    def test_the_assistants_own_text_turn_is_carried_into_the_follow_up(self) -> None:
        # The bug this guards: dropping the final assistant turn left the model
        # unable to see what it had just said.
        _, _, recorder = _run(self.SCRIPT)
        messages = recorder.calls[-1]["messages"]
        spoken = [
            block.text
            for message in messages if message["role"] == "assistant"
            for block in message["content"]
            if getattr(block, "type", "") == "text"
        ]
        self.assertIn("The title is Example Domain.", spoken)

    def test_it_stops_answering_tools_rather_than_looping_forever(self) -> None:
        endless = [[_tool_use(f"toolu_{n}", "browser_get_page")] for n in range(20)]
        ok, output, recorder = _run(endless)
        self.assertTrue(ok, output)
        self.assertLessEqual(len(recorder.calls), api_preflight.MAX_TOOL_ROUNDS + 2)


class FailureReportingTests(unittest.TestCase):
    """A refusal at any point in the conversation has to be reported as one."""

    def _refusal(self):
        import anthropic

        request = getattr(anthropic, "_base_client", None)
        return anthropic.BadRequestError(
            "bad request",
            response=_Response400(),
            body={"error": {"type": "invalid_request_error",
                            "message": "thinking: unsupported parameter"}},
        ) if request is not None else RuntimeError("bad request")

    def test_a_rejected_opening_request_fails(self) -> None:
        ok, output, _ = _run([self._refusal()])
        self.assertFalse(ok)
        self.assertIn("FAILED", output)

    def test_a_rejected_tool_round_fails_and_names_the_stage(self) -> None:
        ok, output, _ = _run([
            [_tool_use("toolu_1", "browser_open_tab")],
            self._refusal(),
        ])
        self.assertFalse(ok)
        self.assertIn("tool round 1", output)

    def test_a_rejected_follow_up_fails_and_names_the_stage(self) -> None:
        ok, output, _ = _run([
            [_tool_use("toolu_1", "browser_open_tab")],
            [_text("The title is Example Domain.")],
            self._refusal(),
        ])
        self.assertFalse(ok)
        self.assertIn("follow-up", output)

    def test_the_apis_own_words_reach_the_operator(self) -> None:
        # Without this the operator sees "Claude rejected the request (400)" and
        # has nothing to act on - which is how the original outage stayed a
        # mystery.
        _, output, _ = _run([self._refusal()])
        self.assertIn("unsupported parameter", output)


class _Response400:
    """The minimum of an httpx response that the SDK's error type reads."""

    status_code = 400
    headers: dict = {}
    request = None

    def json(self):
        return {"error": {"type": "invalid_request_error",
                          "message": "thinking: unsupported parameter"}}


class PreflightCoverageTests(unittest.TestCase):
    def test_it_sends_the_whole_tool_surface_and_the_real_system_prompt(self) -> None:
        # A preflight that trims the request is not testing the browser's
        # request. The tool schemas are most of the payload and the likeliest
        # thing to be rejected.
        from app.agent.prompt import SYSTEM_PROMPT
        from app.agent.tools import TOOL_SCHEMAS

        _, _, recorder = _run(ConversationShapeTests.SCRIPT)
        request = recorder.calls[0]
        self.assertEqual(len(request["tools"]), len(TOOL_SCHEMAS))
        system = request["system"]
        blocks = system if isinstance(system, list) else [{"text": system}]
        self.assertIn(SYSTEM_PROMPT[:80], "".join(block["text"] for block in blocks))

    def test_it_uses_the_real_client_so_the_request_is_not_a_reimplementation(self) -> None:
        self.assertIs(api_preflight.ClaudeClient, ClaudeClient)


if __name__ == "__main__":
    unittest.main()

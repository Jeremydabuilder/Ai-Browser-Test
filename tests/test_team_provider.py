"""The Team against the real Groq client class over a mock HTTP transport:
request shape, credential handling, 429/Retry-After retries, auth and quota
errors, and that the key never reaches stored output.

No network: ``httpx2.MockTransport`` stands in for api.groq.com.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_team_provider -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx2 as httpx  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.openai_compatible import GroqClient  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.llm import (  # noqa: E402
    CancelToken, ErrorKind, TeamError, TeamLLM, make_client_factory, redact, resolve_provider,
)
from app.team.model import Mission, MissionStatus, Source, SourceKind  # noqa: E402
from app.team import sandbox as sandbox_mod  # noqa: E402

KEY = "gsk_unit_test_key_0123456789abcdef"


def completion(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5}})


def client_over(handler) -> GroqClient:
    config = AgentConfig(provider="groq", model="llama-3.3-70b-versatile", max_tokens=512)
    return GroqClient(KEY, config, transport=httpx.MockTransport(handler))


def llm_over(handler, **limits) -> tuple[TeamLLM, list[tuple[str, str, str]]]:
    events: list[tuple[str, str, str]] = []
    client = client_over(handler)
    llm = TeamLLM(lambda: client, TeamLimits(max_backoff_s=1.0, **limits), CancelToken(),
                  emit=lambda a, k, t: events.append((a, k, t)), secret=KEY, label="Groq")
    return llm, events


class RequestShapeTests(unittest.TestCase):
    def test_a_call_posts_an_openai_style_chat_request_with_the_key_in_the_header_only(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return completion("hello team")

        llm, _ = llm_over(handler)
        reply = llm.complete("You are the Writer.", "Write a haiku.", agent="writer")
        self.assertEqual(reply.text, "hello team")
        self.assertEqual((reply.input_tokens, reply.output_tokens), (12, 5))
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "llama-3.3-70b-versatile")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "Write a haiku.")
        self.assertNotIn("tools", body)                    # the team's agents are given no tools at all
        self.assertNotIn(KEY, request.content.decode())     # the key is never in the body

    def test_a_reply_that_echoes_the_key_is_scrubbed(self) -> None:
        llm, _ = llm_over(lambda request: completion(f"my key is {KEY} and also gsk_AnotherLookingKey99887766"))
        text = llm.complete("s", "u", agent="writer").text
        self.assertNotIn(KEY, text)
        self.assertNotIn("gsk_AnotherLookingKey99887766", text)
        self.assertEqual(redact(f"x {KEY} y", KEY), "x [redacted] y")


class RateLimitOverHttpTests(unittest.TestCase):
    def test_a_429_with_retry_after_is_retried_then_succeeds(self) -> None:
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(429, headers={"retry-after": "0.01"},
                                      json={"error": {"message": "Rate limit reached for model"}})
            return completion("finally")

        llm, events = llm_over(handler, max_retries=3)
        self.assertEqual(llm.complete("s", "u", agent="researcher").text, "finally")
        self.assertEqual(len(attempts), 3)
        waits = [t for a, k, t in events if k == "warning"]
        self.assertEqual(len(waits), 2)
        self.assertIn("rate limited", waits[0])
        self.assertIn("attempt 1/3", waits[0])

    def test_retries_stop_at_the_bound_with_an_actionable_message(self) -> None:
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={"error": {"message": "slow down"}})

        llm, _ = llm_over(handler, max_retries=2)
        with self.assertRaises(TeamError) as caught:
            llm.complete("s", "u", agent="researcher")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(caught.exception.kind, ErrorKind.RATE_LIMIT)
        self.assertIn("gave up after 2 retries", caught.exception.message)

    def test_a_server_error_is_retried_but_a_bad_key_and_exhausted_quota_are_not(self) -> None:
        for status, body, kind, expected_calls in (
            (401, {"error": {"message": "Invalid API Key"}}, ErrorKind.AUTH, 1),
            (429, {"error": {"message": "You exceeded your current quota"}}, ErrorKind.QUOTA, 1),
            (500, {"error": {"message": "boom"}}, ErrorKind.PROVIDER, 3),
        ):
            calls = []

            def handler(request, status=status, body=body):
                calls.append(1)
                return httpx.Response(status, headers={"retry-after": "0.01"}, json=body)

            llm, _ = llm_over(handler, max_retries=2)
            with self.assertRaises(TeamError) as caught:
                llm.complete("s", "u", agent="writer")
            self.assertEqual(caught.exception.kind, kind, status)
            self.assertEqual(len(calls), expected_calls, status)
            self.assertNotIn(KEY, caught.exception.message)

    def test_a_whole_mission_survives_a_rate_limit_in_the_middle_of_a_run(self) -> None:
        roles_seen: list[str] = []
        limited = {"done": False}
        plan = json.dumps({"summary": "s", "success_criteria": ["ok"], "tasks": [
            {"id": "T1", "title": "Write", "agent": "writer", "instructions": "write it", "depends_on": []},
            {"id": "T2", "title": "Review", "agent": "reviewer", "instructions": "check", "depends_on": ["T1"]}]})

        def handler(request):
            system = json.loads(request.content)["messages"][0]["content"]
            if "ROLE: Coordinator. Turn" in system:
                roles_seen.append("plan")
                return completion(plan)
            if "ROLE: Writer" in system:
                if not limited["done"]:
                    limited["done"] = True
                    return httpx.Response(429, headers={"retry-after": "0.01"},
                                          json={"error": {"message": "Rate limit reached"}})
                roles_seen.append("writer")
                return completion("The finished draft.")
            if "ROLE: Reviewer" in system:
                roles_seen.append("reviewer")
                return completion(json.dumps({"verdict": "approve", "summary": "fine", "issues": []}))
            roles_seen.append("final")
            return completion("All done.")

        client = client_over(handler)
        mission = Mission(goal="Write something", sources=[Source("S1", SourceKind.PASTE, "Note", "", "text")])
        engine = TeamEngine(mission, lambda: client, TeamLimits(max_backoff_s=1.0),
                            Capabilities(sandbox_mod.SandboxStatus(False, "none")), secret=KEY, provider_label="Groq")
        engine.run()
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        self.assertEqual(roles_seen, ["plan", "writer", "reviewer", "final"])
        self.assertTrue(any("rate limited" in e.text for e in mission.events))
        self.assertNotIn(KEY, json.dumps(mission.to_dict()))


class CredentialResolutionTests(unittest.TestCase):
    class Settings:
        def __init__(self, **values):
            self.values = values

        def get(self, key, default=""):
            return self.values.get(key, default)

    def test_the_default_is_groq_with_the_existing_credential_lookup(self) -> None:
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}, clear=False):
            status = resolve_provider(self.Settings())
        self.assertEqual((status.provider, status.available), ("groq", True))
        self.assertEqual(status.model, "llama-3.3-70b-versatile")
        self.assertNotIn(KEY, status.detail)

    def test_the_team_model_and_provider_are_configurable(self) -> None:
        settings = self.Settings(team_model="openai/gpt-oss-20b", team_provider="openrouter")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY}):
            status = resolve_provider(settings)
        self.assertEqual((status.provider, status.model), ("openrouter", "openai/gpt-oss-20b"))
        with patch.dict(os.environ, {"PYBROWSER_TEAM_MODEL": "llama-3.1-8b-instant", "GROQ_API_KEY": KEY}):
            self.assertEqual(resolve_provider(self.Settings()).model, "llama-3.1-8b-instant")
        # the AI agent's own remembered Groq model is honoured when the Team has none
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}):
            self.assertEqual(resolve_provider(self.Settings(agent_model_groq="qwen/qwen3.6-27b")).model,
                             "qwen/qwen3.6-27b")

    def test_an_unsupported_provider_falls_back_to_groq(self) -> None:
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}):
            self.assertEqual(resolve_provider(self.Settings(team_provider="anthropic")).provider, "groq")

    def test_the_client_factory_builds_the_real_groq_client_without_exposing_the_key(self) -> None:
        with patch.dict(os.environ, {"GROQ_API_KEY": KEY}):
            status = resolve_provider(self.Settings())
            client = make_client_factory(status, self.Settings(), TeamLimits(max_output_tokens=1234))()
        self.assertIsInstance(client, GroqClient)
        self.assertEqual(client.config.max_tokens, 1234)
        self.assertEqual(client.config.model, status.model)
        self.assertNotIn(KEY, repr(status))                     # dataclass repr must not leak the secret
        self.assertNotIn(KEY, status.detail)

    def test_without_a_key_the_factory_refuses_instead_of_inventing_one(self) -> None:
        from app.agent.credentials import Credential, Mode
        none = Credential(Mode.NONE, "no Groq credential configured", provider="groq")
        with patch("app.agent.credentials.resolve_for", return_value=none):
            status = resolve_provider(self.Settings())
            self.assertFalse(status.available)
            self.assertIn("GROQ_API_KEY", status.detail)
            with self.assertRaises(TeamError) as caught:
                make_client_factory(status, self.Settings())()
        self.assertEqual(caught.exception.kind, ErrorKind.NO_CREDENTIAL)


if __name__ == "__main__":
    unittest.main()

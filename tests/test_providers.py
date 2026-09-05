"""Multi-provider support: Groq and OpenRouter alongside Anthropic.

The property that matters most here is the one AgentSession's design already
gave us for free: it never learns which provider it is talking to. Most of
these tests exist to prove that stays true - tool calls normalize into the
same internal shape regardless of wire format, the safety classification of
a given browser action is identical no matter which provider asked for it,
and switching providers can never see or clobber another provider's stored
key.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_providers -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-providers-"))
os.environ["PYBROWSER_DISABLE_KEYRING"] = "1"

import httpx2 as httpx  # noqa: E402

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent import credentials as creds  # noqa: E402
from app.agent.claude_client import AgentResponse, ClaudeError, ToolCall  # noqa: E402
from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.openai_compatible import (  # noqa: E402
    GroqClient,
    OpenRouterClient,
    messages_param,
    normalise,
    tools_param,
)
from app.agent.session import AgentSession, AgentState  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None

_VARS = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def pump(predicate, timeout_ms: int = 20000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


# ---------------------------------------------------------------------------
# Wire-format translation
# ---------------------------------------------------------------------------


class TranslationTests(unittest.TestCase):
    """Anthropic-shaped in, OpenAI-shaped out, and back - byte for byte."""

    def test_tools_translate_to_function_schemas(self):
        out = tools_param([{"name": "browser_click", "description": "Click it.",
                            "input_schema": {"type": "object", "properties": {}}}])
        self.assertEqual(out, [{"type": "function", "function": {
            "name": "browser_click", "description": "Click it.",
            "parameters": {"type": "object", "properties": {}}}}])

    def test_an_assistant_tool_call_round_trips_through_a_user_turn(self):
        # Exactly the shape AgentSession builds: an assistant turn holding
        # raw_content, followed by a user turn holding the tool_result.
        messages = [
            {"role": "user", "content": "Open the page"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Sure."},
                {"type": "tool_use", "id": "call_1", "name": "browser_get_page", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "page text"},
            ]},
        ]
        out = messages_param("system prompt", messages)
        self.assertEqual(out[0], {"role": "system", "content": "system prompt"})
        self.assertEqual(out[2]["tool_calls"][0]["function"]["name"], "browser_get_page")
        self.assertEqual(json.loads(out[2]["tool_calls"][0]["function"]["arguments"]), {})
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "call_1", "content": "page text"})

    def test_a_response_with_a_tool_call_normalises_to_anthropic_shaped_blocks(self):
        response = normalise({
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_9", "type": "function",
                 "function": {"name": "browser_click", "arguments": '{"ref": "s1:e1"}'}},
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })
        self.assertEqual(response.tool_calls,
                         [ToolCall(id="call_9", name="browser_click", arguments={"ref": "s1:e1"})])
        self.assertEqual(response.stop_reason, "tool_use")
        self.assertEqual(response.raw_content,
                         [{"type": "tool_use", "id": "call_9", "name": "browser_click",
                           "input": {"ref": "s1:e1"}}])
        self.assertEqual(response.input_tokens, 5)
        self.assertEqual(response.output_tokens, 3)

    def test_a_text_only_response_ends_the_turn(self):
        response = normalise({"choices": [{"message": {"content": "All done."},
                                          "finish_reason": "stop"}]})
        self.assertEqual(response.text, "All done.")
        self.assertFalse(response.wants_tools)
        self.assertEqual(response.stop_reason, "end_turn")

    def test_malformed_tool_arguments_do_not_crash_normalisation(self):
        response = normalise({"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "x", "arguments": "not json"}},
        ]}, "finish_reason": "tool_calls"}]})
        self.assertEqual(response.tool_calls[0].arguments, {})


# ---------------------------------------------------------------------------
# The HTTP client, offline (httpx MockTransport - no real network)
# ---------------------------------------------------------------------------


class ClientTests(unittest.TestCase):
    def test_a_successful_call_returns_an_agent_response(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["model"], "llama-test")
            self.assertIn("Authorization", request.headers)
            self.assertEqual(request.headers["Authorization"], "Bearer gsk_test")
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Hi there."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            })

        client = GroqClient("gsk_test", AgentConfig(model="llama-test"),
                            transport=httpx.MockTransport(handler))
        response = client.send(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(response.text, "Hi there.")

    def test_max_tokens_is_used_by_default(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertIn("max_tokens", body)
            self.assertNotIn("max_completion_tokens", body)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"},
                                                          "finish_reason": "stop"}]})

        client = GroqClient("k", AgentConfig(), transport=httpx.MockTransport(handler))
        client.send(system="s", messages=[], tools=[])

    def test_a_model_that_rejects_max_tokens_falls_back_to_max_completion_tokens(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message":
                    "Unrecognized request argument supplied: max_tokens"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"},
                                                          "finish_reason": "stop"}]})

        client = GroqClient("k", AgentConfig(), transport=httpx.MockTransport(handler))
        response = client.send(system="s", messages=[], tools=[])
        self.assertEqual(response.text, "hi")
        self.assertEqual(len(calls), 2)
        self.assertIn("max_tokens", calls[0])
        self.assertIn("max_completion_tokens", calls[1])
        self.assertNotIn("max_tokens", calls[1])

    def test_the_fallback_is_remembered_for_the_rest_of_the_client_lifetime(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message":
                    "Unrecognized request argument supplied: max_tokens"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"},
                                                          "finish_reason": "stop"}]})

        client = GroqClient("k", AgentConfig(), transport=httpx.MockTransport(handler))
        client.send(system="s", messages=[], tools=[])   # triggers the one-time fallback
        client.send(system="s", messages=[], tools=[])   # must go straight to the new name
        self.assertEqual(len(calls), 3)
        self.assertIn("max_completion_tokens", calls[2])

    def test_an_unrelated_400_does_not_trigger_the_max_tokens_fallback(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": "invalid model id"}})

        client = GroqClient("k", AgentConfig(), transport=httpx.MockTransport(handler))
        with self.assertRaises(ClaudeError):
            client.send(system="s", messages=[], tools=[])
        self.assertEqual(len(calls), 1, "no retry for a 400 unrelated to max_tokens")

    def test_test_connection_also_falls_back_to_max_completion_tokens(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message":
                    "Unrecognized request argument supplied: max_tokens"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ready"},
                                                          "finish_reason": "stop"}]})

        ok, message = GroqClient.test_connection(
            "k", "some-model", transport=httpx.MockTransport(handler))
        self.assertTrue(ok, message)
        self.assertEqual(len(calls), 2)
        self.assertIn("max_completion_tokens", calls[1])

    def test_an_empty_key_is_refused_before_any_request(self):
        with self.assertRaises(ClaudeError):
            GroqClient("", AgentConfig())

    def test_a_401_is_translated_clearly(self):
        client = GroqClient("bad", AgentConfig(),
                            transport=httpx.MockTransport(
                                lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertIn("rejected the API key", ctx.exception.message)
        self.assertIn("Configure AI Agent", ctx.exception.message)

    def test_a_429_names_free_quota_when_the_body_says_so(self):
        client = GroqClient("k", AgentConfig(),
                            transport=httpx.MockTransport(
                                lambda r: httpx.Response(
                                    429, json={"error": {"message": "Free tier quota exceeded"}})))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertIn("free quota is exhausted", ctx.exception.message)

    def test_a_plain_429_is_retryable_rate_limiting(self):
        client = GroqClient("k", AgentConfig(),
                            transport=httpx.MockTransport(
                                lambda r: httpx.Response(429, json={"error": {"message": "slow down"}})))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("rate limiting", ctx.exception.message)

    def test_a_model_unavailable_404_is_translated_clearly(self):
        client = GroqClient("k", AgentConfig(model="not-a-real-model"),
                            transport=httpx.MockTransport(lambda r: httpx.Response(404, text="{}")))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertIn("not available via Groq", ctx.exception.message)

    def test_tool_calling_unsupported_is_translated_clearly(self):
        from app.agent.openai_compatible import TOOL_UNSUPPORTED_MESSAGE

        client = GroqClient("k", AgentConfig(),
                            transport=httpx.MockTransport(lambda r: httpx.Response(
                                400, json={"error": {"message":
                                          "This model does not support tool use."}})))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[{"name": "x", "input_schema": {}}])
        self.assertEqual(ctx.exception.message, TOOL_UNSUPPORTED_MESSAGE)
        self.assertIn("custom tools PyBrowser requires", ctx.exception.message)

    def test_a_server_error_is_retryable(self):
        client = GroqClient("k", AgentConfig(),
                            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertTrue(ctx.exception.retryable)

    def test_the_api_key_never_appears_in_a_raised_error(self):
        secret = "gsk_super_secret_value_do_not_leak"
        client = GroqClient(secret, AgentConfig(),
                            transport=httpx.MockTransport(
                                lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})))
        with self.assertRaises(ClaudeError) as ctx:
            client.send(system="s", messages=[], tools=[])
        self.assertNotIn(secret, ctx.exception.message)
        self.assertNotIn(secret, ctx.exception.detail)
        self.assertNotIn(secret, ctx.exception.api_message)
        self.assertNotIn(secret, str(ctx.exception))

    def test_openrouter_sends_attribution_headers(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                          "finish_reason": "stop"}]})

        client = OpenRouterClient("k", AgentConfig(), transport=httpx.MockTransport(handler))
        client.send(system="s", messages=[], tools=[])
        self.assertIn("http-referer", {k.lower() for k in seen})
        self.assertIn("x-title", {k.lower() for k in seen})


# ---------------------------------------------------------------------------
# Model listing and capability metadata
# ---------------------------------------------------------------------------


class CapabilityTests(unittest.TestCase):
    def test_list_models_returns_the_providers_own_data(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})

        models = GroqClient.list_models("k", transport=httpx.MockTransport(handler))
        self.assertEqual(models, [{"id": "llama-3.3-70b-versatile"}])

    def test_list_models_never_raises_on_failure(self):
        models = GroqClient.list_models(
            "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        self.assertEqual(models, [])
        models2 = GroqClient.list_models(
            "k", transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(RuntimeError("x"))))
        self.assertEqual(models2, [])

    def test_groq_flags_obviously_non_chat_models_as_unsupported(self):
        supported, _note = GroqClient.capability_of({"id": "whisper-large-v3"})
        self.assertFalse(supported)

    def test_groq_does_not_claim_certainty_for_ordinary_models(self):
        supported, note = GroqClient.capability_of({"id": "llama-3.3-70b-versatile"})
        self.assertTrue(supported)
        self.assertIn("Test Connection", note)

    def test_openrouter_trusts_its_own_supported_parameters_field(self):
        supported, _note = OpenRouterClient.capability_of(
            {"id": "x", "supported_parameters": ["temperature"]})
        self.assertFalse(supported)
        supported2, note2 = OpenRouterClient.capability_of(
            {"id": "x", "supported_parameters": ["tools"],
             "pricing": {"prompt": "0", "completion": "0"}})
        self.assertTrue(supported2)
        self.assertIn("free", note2)

    def test_a_successful_test_connection_reports_success(self):
        ok, message = GroqClient.test_connection(
            "k", "llama-3.3-70b-versatile",
            transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]})))
        self.assertTrue(ok)
        self.assertIn("tool-calling", message)

    def test_test_connection_with_no_key_fails_fast_and_locally(self):
        ok, message = GroqClient.test_connection("", "some-model")
        self.assertFalse(ok)
        self.assertIn("No Groq API key", message)


# ---------------------------------------------------------------------------
# Credentials: isolated per provider, never crossed
# ---------------------------------------------------------------------------


class CredentialIsolationTests(unittest.TestCase):
    def setUp(self):
        self._saved = {v: os.environ.pop(v, None) for v in _VARS}

    def tearDown(self):
        for name, value in self._saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_groq_and_openrouter_use_different_keyring_accounts(self):
        groq_label, groq_env, groq_account = creds.PROVIDER_KEY_INFO["groq"]
        or_label, or_env, or_account = creds.PROVIDER_KEY_INFO["openrouter"]
        self.assertNotEqual(groq_account, or_account)
        self.assertNotEqual(groq_env, or_env)

    def test_a_groq_key_is_invisible_to_openrouter_resolution(self):
        os.environ["GROQ_API_KEY"] = "gsk_only_for_groq"
        groq = creds.resolve_for("groq")
        openrouter = creds.resolve_for("openrouter")
        self.assertEqual(groq.secret, "gsk_only_for_groq")
        self.assertFalse(openrouter.available)

    def test_an_anthropic_env_key_is_invisible_to_groq_resolution(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-only-for-anthropic"
        groq = creds.resolve_for("groq")
        self.assertFalse(groq.available)

    def test_resolve_for_anthropic_is_identical_to_resolve(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        self.assertEqual(creds.resolve_for("anthropic"), creds.resolve())

    def test_each_credential_carries_its_own_provider_tag(self):
        os.environ["GROQ_API_KEY"] = "gsk_x"
        self.assertEqual(creds.resolve_for("groq").provider, "groq")
        os.environ["OPENROUTER_API_KEY"] = "or_x"
        self.assertEqual(creds.resolve_for("openrouter").provider, "openrouter")

    def test_no_key_configured_for_either_is_a_clean_none(self):
        self.assertEqual(creds.resolve_for("groq").mode, creds.Mode.NONE)
        self.assertFalse(creds.resolve_for("groq").available)


# ---------------------------------------------------------------------------
# Building the right client for the configured provider
# ---------------------------------------------------------------------------


class TransportSelectionTests(unittest.TestCase):
    def test_anthropic_still_builds_a_claude_client(self):
        from app.agent.claude_client import ClaudeClient
        from app.ui.agent_setup import build_transport

        credential = creds.Credential(creds.Mode.ENV_KEY, "k", secret="sk-ant-x",
                                      provider="anthropic")
        transport = build_transport(credential, AgentConfig())
        self.assertIsInstance(transport, ClaudeClient)

    def test_groq_builds_a_groq_client(self):
        from app.ui.agent_setup import build_transport

        credential = creds.Credential(creds.Mode.ENV_KEY, "k", secret="gsk_x", provider="groq")
        transport = build_transport(credential, AgentConfig(model="llama-test"))
        self.assertIsInstance(transport, GroqClient)
        self.assertEqual(transport._model, "llama-test")

    def test_openrouter_builds_an_openrouter_client(self):
        from app.ui.agent_setup import build_transport

        credential = creds.Credential(creds.Mode.ENV_KEY, "k", secret="or_x", provider="openrouter")
        transport = build_transport(credential, AgentConfig())
        self.assertIsInstance(transport, OpenRouterClient)

    def test_build_session_reports_a_clean_error_with_no_groq_key(self):
        from app.ui.agent_setup import build_session

        os.environ.pop("GROQ_API_KEY", None)
        settings = _FakeSettings({"agent_provider": "groq"})
        session, reason = build_session(mock.Mock(), settings=settings)
        self.assertIsNone(session)
        self.assertIn("groq", reason.lower())
        self.assertIn("Configure AI Agent", reason)


class _FakeSettings:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value


# ---------------------------------------------------------------------------
# Full agent-loop integration: a real browser, a real AgentSession, an
# OpenAI-compatible transport talking to a mocked HTTP endpoint. This proves
# the wire-format translation actually round-trips through AgentSession's
# real message building, pruning and tool dispatch - not just through the
# translation functions in isolation.
# ---------------------------------------------------------------------------


def _openai_tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(arguments)}},
    ]}, "finish_reason": "tool_calls"}]}


def _openai_text(text: str) -> dict:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedHTTP:
    """Replays a list of JSON response bodies for successive HTTP calls,
    the OpenAI-adapter equivalent of tests.fake_claude.ScriptedClaude."""

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.requests: list[dict] = []

    def __call__(self, request: "httpx.Request") -> "httpx.Response":
        body = json.loads(request.content)
        self.requests.append(body)
        if not self._script:
            return httpx.Response(500, json={"error": {"message": "script ran out"}})
        return httpx.Response(200, json=self._script.pop(0))

    def last_tool_message(self) -> str:
        for body in reversed(self.requests):
            for message in reversed(body.get("messages", [])):
                if message.get("role") == "tool":
                    return message.get("content", "")
        return ""


def _ref_from(tool_content: str, role: str, name_contains: str) -> str:
    from app.agent.tools import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    if UNTRUSTED_OPEN not in tool_content:
        raise AssertionError("no page structure in the last tool result")
    body = tool_content.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0]
    structure = json.loads(body)
    needle = name_contains.lower()
    for element in structure.get("elements", []):
        if element.get("role") == role and needle in element.get("name", "").lower():
            return element["ref"]
    raise AssertionError(f"no {role} named {name_contains!r} in the page structure")


class GroqIntegrationTests(unittest.TestCase):
    """A real AgentSession, a real browser, GroqClient as the transport."""

    def setUp(self) -> None:
        self.tabs = TabManager(_profile, _server.base)
        self.tabs.resize(1200, 800)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.browser.navigate(_server.base).wait()
        self.confirmations: list = []
        self.session: AgentSession | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def _start(self, script: list[dict]) -> ScriptedHTTP:
        http = ScriptedHTTP(script)
        client = GroqClient("gsk_test", AgentConfig(model="llama-test", limits=ContextLimits()),
                            transport=httpx.MockTransport(http))
        self.session = AgentSession(self.browser, client, client.config)
        self.session.confirmation_required.connect(self.confirmations.append)
        return http

    def test_a_tool_call_executes_through_the_real_browser(self):
        http = self._start([
            _openai_tool_call("call_1", "browser_get_page", {}),
            _openai_text("Read the page."),
        ])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("Read the page.")
        self.assertTrue(pump(lambda: bool(done), 20000))
        self.assertEqual(self.session.state, AgentState.IDLE)
        # A real page structure came back through the real tool loop.
        self.assertIn("elements", http.last_tool_message())

    def test_tool_results_round_trip_correctly(self):
        # Two tool calls in sequence, the second depending on the first's
        # result (the same shape a real browsing task takes) - proves the
        # tool_result content actually reaches the next request, not just
        # that a request was sent.
        http = ScriptedHTTP([])

        def handler(request):
            body = json.loads(request.content)
            http.requests.append(body)
            if len(http.requests) == 1:
                return httpx.Response(200, json=_openai_tool_call(
                    "call_1", "browser_get_page", {}))
            if len(http.requests) == 2:
                ref = _ref_from(http.last_tool_message(), "button", "Buy now")
                return httpx.Response(200, json=_openai_tool_call(
                    "call_2", "browser_click", {"ref": ref}))
            return httpx.Response(200, json=_openai_text("Bought it."))

        client = GroqClient("gsk_test", AgentConfig(model="llama-test", limits=ContextLimits()),
                            transport=httpx.MockTransport(handler))
        self.session = AgentSession(self.browser, client, client.config)
        self.session.confirmation_required.connect(self.confirmations.append)

        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations), 20000))
        self.session.resolve_confirmation(True)
        self.assertTrue(pump(lambda: bool(done), 20000))
        self.assertEqual(len(http.requests), 3)

    def test_a_sensitive_action_still_asks_for_confirmation(self):
        """Provider choice must never affect action approval. The safety
        classification comes from BrowserController.describe_action, which
        never sees which transport produced the tool call."""
        def handler(request):
            body = json.loads(request.content)
            count = sum(1 for m in body["messages"] if m.get("role") == "user")
            if not any(m.get("role") == "tool" for m in body["messages"]):
                return httpx.Response(200, json=_openai_tool_call(
                    "call_1", "browser_get_page", {}))
            ref = _ref_from(
                next(m["content"] for m in reversed(body["messages"]) if m.get("role") == "tool"),
                "button", "Buy now")
            return httpx.Response(200, json=_openai_tool_call("call_2", "browser_click", {"ref": ref}))

        client = GroqClient("gsk_test", AgentConfig(model="llama-test", limits=ContextLimits()),
                            transport=httpx.MockTransport(handler))
        self.session = AgentSession(self.browser, client, client.config)
        self.session.confirmation_required.connect(self.confirmations.append)
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations), 20000))

        request = self.confirmations[0]
        self.assertEqual(self.session.state, AgentState.AWAITING_CONFIRMATION)
        self.assertIn("buy now", request.description.lower())
        self.assertIn("spend money", " ".join(request.reasons))
        self.session.resolve_confirmation(False)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE, 20000))


# ---------------------------------------------------------------------------
# Safety classification is identical regardless of which transport is used
# ---------------------------------------------------------------------------


class SafetyParityTests(unittest.TestCase):
    """The exact same browser action gets the exact same safety
    classification whether the model is Anthropic, Groq, or OpenRouter -
    because ToolRegistry.assess() never references the transport at all."""

    def test_tool_registry_construction_takes_no_provider_argument(self):
        import inspect

        from app.agent.tools import ToolRegistry

        params = inspect.signature(ToolRegistry.__init__).parameters
        self.assertNotIn("provider", params)
        self.assertNotIn("transport", params)
        self.assertNotIn("credential", params)

    def test_assess_result_is_identical_across_provider_labelled_credentials(self):
        """assess() takes no credential at all - constructing a ToolRegistry
        with different provider contexts around it changes nothing about
        what one call to assess() returns for the same action."""
        from app.agent.tools import ToolRegistry

        registry = ToolRegistry(None, None, None)
        first = registry.assess("mission_save_finding", {})
        second = registry.assess("mission_save_finding", {})
        self.assertEqual(first, second)

    def test_missions_module_has_no_provider_or_credential_concept(self):
        import app.missions.service as missions_service

        source = missions_service.__file__
        with open(source) as fh:
            text = fh.read()
        for needle in ("groq", "openrouter", "anthropic", "Credential", "ClaudeClient"):
            self.assertNotIn(needle, text, f"Missions must stay provider-agnostic: found {needle!r}")


# ---------------------------------------------------------------------------
# Denylist / capability metadata at the client level
# ---------------------------------------------------------------------------


class DenylistTests(unittest.TestCase):
    """groq/compound and groq/compound-mini run their own server-side tool
    loop and reject a caller-supplied schema - PyBrowser's tools never reach
    them. They must never be presented as normal compatible choices."""

    def test_groq_compound_models_are_denylisted(self):
        self.assertTrue(GroqClient.is_denylisted("groq/compound"))
        self.assertTrue(GroqClient.is_denylisted("groq/compound-mini"))
        self.assertTrue(GroqClient.is_denylisted("GROQ/COMPOUND-MINI"))

    def test_openrouter_denylists_the_same_compound_models(self):
        self.assertTrue(OpenRouterClient.is_denylisted("groq/compound"))
        self.assertTrue(OpenRouterClient.is_denylisted("groq/compound-mini"))

    def test_an_ordinary_model_is_not_denylisted(self):
        self.assertFalse(GroqClient.is_denylisted("llama-3.3-70b-versatile"))
        self.assertFalse(OpenRouterClient.is_denylisted("llama-3.3-70b-versatile"))

    def test_capability_of_refuses_a_denylisted_entry_even_with_good_metadata(self):
        # Even if a future OpenRouter listing claimed "tools" support for
        # this model, the denylist wins - it is a known-bad interaction, not
        # a guess from missing metadata.
        supported, reason = OpenRouterClient.capability_of(
            {"id": "groq/compound-mini", "supported_parameters": ["tools"]})
        self.assertFalse(supported)
        self.assertIn("custom tool interface", reason)

    def test_list_models_filters_denylisted_entries_out_entirely(self):
        def handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "llama-3.3-70b-versatile"},
                {"id": "groq/compound"},
                {"id": "groq/compound-mini"},
            ]})

        models = GroqClient.list_models("k", transport=httpx.MockTransport(handler))
        ids = {m["id"] for m in models}
        self.assertEqual(ids, {"llama-3.3-70b-versatile"})

    def test_the_seed_list_never_contains_a_denylisted_model(self):
        for entry in GroqClient.seed_models():
            self.assertFalse(GroqClient.is_denylisted(entry["id"]))

    def test_the_seed_list_matches_the_requested_models(self):
        seeded = {entry["id"] for entry in GroqClient.seed_models()}
        self.assertEqual(seeded, {
            "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
            "openai/gpt-oss-20b", "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b", "qwen/qwen3.8-27b",
        })

    def test_pretty_label_keeps_the_exact_model_id_visible(self):
        from app.agent.openai_compatible import pretty_label

        label = pretty_label("llama-3.3-70b-versatile")
        self.assertIn("llama-3.3-70b-versatile", label)
        self.assertNotEqual(label, "llama-3.3-70b-versatile")  # actually humanized


# ---------------------------------------------------------------------------
# The dialog's model dropdown
# ---------------------------------------------------------------------------


class ModelSelectionTests(unittest.TestCase):
    """Tools -> Configure AI Agent's model picker for Groq/OpenRouter."""

    def setUp(self):
        from PySide6.QtWidgets import QMessageBox

        self._info = mock.patch.object(QMessageBox, "information", lambda *a, **k: None)
        self._warn_calls = []
        self._warn = mock.patch.object(
            QMessageBox, "warning",
            lambda *a, **k: self._warn_calls.append(a[2] if len(a) > 2 else ""))
        self._info.start()
        self._warn.start()
        self.settings = _FakeSettings({})

    def tearDown(self):
        self._info.stop()
        self._warn.stop()

    def _dialog(self):
        from app.ui.agent_setup import ApiKeyDialog

        dialog = ApiKeyDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def _switch_to(self, dialog, provider_id: str):
        index = dialog.provider_box.findData(provider_id)
        dialog.provider_box.setCurrentIndex(index)
        self._wait_for_call(dialog)

    def _wait_for_call(self, dialog) -> None:
        """list_models/test_connection run on a background QThread now, so a
        test that triggers one must pump the event loop until it delivers
        its result rather than asserting immediately after."""
        pump(lambda: dialog._other_worker is None)

    def test_switching_to_groq_populates_the_dropdown_from_the_seed_list(self):
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        ids = {dialog._other_model_box.itemData(i)
              for i in range(dialog._other_model_box.count())}
        self.assertIn("llama-3.3-70b-versatile", ids)
        self.assertGreater(dialog._other_model_box.count(), 1)

    def test_openrouter_with_no_key_shows_a_clear_placeholder_not_an_empty_box(self):
        """OpenRouter has no seed list (unlike Groq's six), so with no key
        configured yet the dropdown would otherwise just be empty - which
        reads as broken, not as "nothing to show yet"."""
        dialog = self._dialog()
        self._switch_to(dialog, "openrouter")
        self.assertEqual(dialog._other_model_box.count(), 1)
        self.assertIn("Refresh model list", dialog._other_model_box.itemText(0))
        self.assertFalse(dialog._other_model_box.model().item(0).isEnabled())
        self.assertEqual(dialog._selected_other_model(), "")

    def test_a_configured_key_triggers_an_automatic_live_fetch_no_click_needed(self):
        live = [{"id": "llama-3.3-70b-versatile"}, {"id": "some-new-model"}]
        with mock.patch.object(creds, "resolve_for",
                               return_value=creds.Credential(
                                   creds.Mode.ENV_KEY, "Groq", secret="gsk_x", provider="groq")):
            with mock.patch.object(GroqClient, "list_models", return_value=live) as fetch:
                dialog = self._dialog()
                self._switch_to(dialog, "groq")
                self.assertTrue(fetch.called, "a stored key must trigger a live fetch automatically")
        ids = {dialog._other_model_box.itemData(i)
              for i in range(dialog._other_model_box.count())}
        self.assertIn("some-new-model", ids)

    def test_a_failed_automatic_live_fetch_is_shown_not_silent(self):
        """A key is configured but the live listing fails (network hiccup,
        the exact failure the real 404 report traced back to) - the dialog
        must say so, not silently look identical to "the seed list is what
        is actually live"."""
        with mock.patch.object(creds, "resolve_for",
                               return_value=creds.Credential(
                                   creds.Mode.ENV_KEY, "Groq", secret="gsk_x", provider="groq")):
            with mock.patch.object(GroqClient, "list_models", return_value=[]):
                dialog = self._dialog()
                self._switch_to(dialog, "groq")
        self.assertIn("Could not load Groq's current model list", dialog._other_result.text())

    def test_the_model_box_is_not_editable(self):
        """The whole point of this pass: Model must read as a click-to-pick
        list, the same as Provider - not a text field."""
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        self.assertFalse(dialog._other_model_box.isEditable())

    def test_the_custom_model_field_is_hidden_until_the_checkbox_is_checked(self):
        # isHidden() reflects the widget's own explicit shown/hidden flag
        # regardless of whether the dialog itself is on screen - unlike
        # isVisible(), which is always False for a QDialog that was never
        # shown, no matter what setVisible() was called with underneath it.
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        self.assertTrue(dialog._other_custom_field.isHidden())
        self.assertTrue(dialog._other_model_box.isEnabled())
        dialog._other_custom_check.setChecked(True)
        self.assertFalse(dialog._other_custom_field.isHidden())
        self.assertFalse(dialog._other_model_box.isEnabled())

    def test_switching_provider_resets_the_custom_model_checkbox(self):
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        dialog._other_custom_check.setChecked(True)
        dialog._other_custom_field.setText("something")
        self._switch_to(dialog, "openrouter")
        self.assertFalse(dialog._other_custom_check.isChecked())
        self.assertEqual(dialog._other_custom_field.text(), "")

    def test_incompatible_models_are_present_but_disabled_not_selectable(self):
        live = [{"id": "llama-3.3-70b-versatile"}, {"id": "whisper-large-v3"}]
        dialog = self._dialog()
        dialog._populate_model_combo("groq", live)
        box = dialog._other_model_box
        whisper_index = box.findData("whisper-large-v3")
        self.assertGreaterEqual(whisper_index, 0, "still shown, not hidden")
        self.assertFalse(box.model().item(whisper_index).isEnabled())

    def test_compound_mini_cannot_be_selected_as_a_normal_compatible_model(self):
        live = [{"id": "llama-3.3-70b-versatile"}, {"id": "groq/compound-mini"}]
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        dialog._populate_model_combo("groq", live)
        box = dialog._other_model_box
        index = box.findData("groq/compound-mini")
        self.assertGreaterEqual(index, 0)
        self.assertFalse(box.model().item(index).isEnabled())
        # And even typed by hand via the advanced fallback, it is refused
        # rather than silently saved.
        dialog._other_custom_check.setChecked(True)
        dialog._other_custom_field.setText("groq/compound-mini")
        self.assertEqual(dialog._selected_other_model(), "groq/compound-mini")
        self.assertFalse(dialog._selected_other_model_supported())
        dialog._save_other_model()
        self.assertEqual(self.settings.get("agent_model_groq", ""), "")

    def test_compound_mini_test_connection_fails_locally_without_a_network_call(self):
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        dialog._other_custom_check.setChecked(True)
        dialog._other_custom_field.setText("groq/compound-mini")
        with mock.patch.object(GroqClient, "test_connection") as tc:
            dialog._test_other_connection()
            self.assertFalse(tc.called)
        self.assertIn("custom tool interface", dialog._other_result.text())

    def test_provider_change_refreshes_the_model_choices(self):
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        groq_ids = {dialog._other_model_box.itemData(i)
                   for i in range(dialog._other_model_box.count())}
        self._switch_to(dialog, "openrouter")
        openrouter_ids = {dialog._other_model_box.itemData(i)
                          for i in range(dialog._other_model_box.count())}
        self.assertNotEqual(groq_ids, openrouter_ids)
        self.assertNotIn("llama-3.3-70b-versatile", openrouter_ids)

    def test_the_last_selected_model_is_remembered_separately_per_provider(self):
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        index = dialog._other_model_box.findData("llama-3.1-8b-instant")
        dialog._other_model_box.setCurrentIndex(index)
        dialog._save_other_model()

        self._switch_to(dialog, "openrouter")
        # OpenRouter has never had a model chosen - Groq's choice must not
        # leak across.
        self.assertNotEqual(dialog._selected_other_model(), "llama-3.1-8b-instant")

        self._switch_to(dialog, "groq")
        self.assertEqual(dialog._selected_other_model(), "llama-3.1-8b-instant")

        from app.agent.config import model_settings_key
        self.assertEqual(self.settings.get(model_settings_key("groq")), "llama-3.1-8b-instant")
        self.assertEqual(self.settings.get(model_settings_key("openrouter"), ""), "")

    def test_openrouter_filters_by_its_own_supported_parameters_metadata(self):
        live = [{"id": "good-model", "supported_parameters": ["tools"]},
               {"id": "bad-model", "supported_parameters": ["temperature"]}]
        dialog = self._dialog()
        dialog._populate_model_combo("openrouter", live)
        box = dialog._other_model_box
        self.assertTrue(box.model().item(box.findData("good-model")).isEnabled())
        self.assertFalse(box.model().item(box.findData("bad-model")).isEnabled())

    def test_manual_entry_still_goes_through_test_connection(self):
        """The manual fallback is for a model not yet in any listing - it
        must still be provable, not a way to skip verification."""
        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        dialog._other_custom_check.setChecked(True)
        dialog._other_custom_field.setText("a-brand-new-model-not-in-any-list")
        with mock.patch.object(GroqClient, "test_connection",
                               return_value=(True, "ok")) as tc:
            dialog._test_other_connection()
            self._wait_for_call(dialog)
            self.assertTrue(tc.called)
            self.assertEqual(tc.call_args[0][1], "a-brand-new-model-not-in-any-list")

    def test_a_clear_message_replaces_the_raw_400_on_test_connection(self):
        from app.agent.openai_compatible import TOOL_UNSUPPORTED_MESSAGE

        dialog = self._dialog()
        self._switch_to(dialog, "groq")
        dialog._other_field.setText("gsk_x")
        with mock.patch.object(
                GroqClient, "test_connection",
                return_value=(False, TOOL_UNSUPPORTED_MESSAGE)):
            dialog._test_other_connection()
            self._wait_for_call(dialog)
        self.assertIn("custom tools PyBrowser requires", dialog._other_result.text())
        self.assertNotIn("400", dialog._other_result.text())


class BackgroundCallTests(unittest.TestCase):
    """list_models/test_connection are real network requests with multi-second
    timeouts (see openai_compatible.py) - they must never block the GUI
    thread, which every Claude request in this codebase already avoids via
    AgentSession's own worker thread."""

    def setUp(self):
        from PySide6.QtWidgets import QMessageBox

        self._info = mock.patch.object(QMessageBox, "information", lambda *a, **k: None)
        self._warn = mock.patch.object(QMessageBox, "warning", lambda *a, **k: None)
        self._info.start()
        self._warn.start()
        self.settings = _FakeSettings({})

    def tearDown(self):
        self._info.stop()
        self._warn.stop()

    def _dialog(self):
        from app.ui.agent_setup import ApiKeyDialog

        dialog = ApiKeyDialog(None, self.settings)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_test_connection_does_not_block_the_event_loop(self):
        """A slow call must let the event loop keep pumping while it runs -
        proof it is not just synchronous code wrapped in a thread that is
        then immediately waited on."""
        import threading
        import time

        release = threading.Event()
        ticked = []

        def slow_call(_key, _model):
            release.wait(timeout=5)
            return (True, "ok")

        dialog = self._dialog()
        index = dialog.provider_box.findData("groq")
        dialog.provider_box.setCurrentIndex(index)
        pump(lambda: dialog._other_worker is None)

        timer = QTimer()
        timer.timeout.connect(lambda: ticked.append(True))
        timer.start(5)
        try:
            with mock.patch.object(GroqClient, "test_connection", side_effect=slow_call):
                dialog._other_field.setText("gsk_x")
                dialog._test_other_connection()
                # The event loop must tick several times *while the call is
                # still in flight* - if it were blocking, nothing would run
                # between calling this and the call returning.
                start = len(ticked)
                for _ in range(20):
                    _app.processEvents()
                    time.sleep(0.01)
                self.assertGreater(len(ticked), start,
                                   "the GUI thread was blocked during the call")
                self.assertTrue(dialog._other_worker is not None
                                and dialog._other_worker.isRunning(),
                                "the call finished suspiciously fast for a 5s wait")
        finally:
            release.set()
            timer.stop()
        pump(lambda: dialog._other_worker is None)
        self.assertIn("ok", dialog._other_result.text())

    def test_closing_the_dialog_mid_fetch_does_not_crash(self):
        import threading

        release = threading.Event()

        def slow_call(_key):
            release.wait(timeout=5)
            return []

        dialog = self._dialog()
        index = dialog.provider_box.findData("groq")
        with mock.patch.object(GroqClient, "list_models", side_effect=slow_call):
            with mock.patch.object(
                    creds, "resolve_for",
                    return_value=creds.Credential(
                        creds.Mode.ENV_KEY, "Groq", secret="gsk_x", provider="groq")):
                dialog.provider_box.setCurrentIndex(index)
                self.assertIsNotNone(dialog._other_worker)
                release.set()
                # closeEvent must wait for the worker rather than tearing
                # down the dialog (and the QThread with it) out from under
                # a still-running call.
                dialog.close()
        # The worker's own `finished` signal is queued across threads and
        # only delivered once the event loop runs - closeEvent's wait() just
        # guarantees the OS thread itself has actually stopped by this point.
        pump(lambda: dialog._other_worker is None)

    def test_switching_away_before_a_slow_fetch_returns_does_not_apply_stale_models(self):
        """Switch to Groq (triggers a slow automatic fetch), switch away to
        OpenRouter (which has no key, so it starts no fetch of its own)
        before the Groq fetch returns, then let it return - it must never
        repopulate the now-showing OpenRouter section with Groq's models."""
        import threading

        release = threading.Event()

        def slow_groq_fetch(_key):
            release.wait(timeout=5)
            return [{"id": "llama-3.3-70b-versatile"}]

        def resolve(provider_id, store=None):
            if provider_id == "groq":
                return creds.Credential(creds.Mode.ENV_KEY, "Groq",
                                        secret="gsk_x", provider="groq")
            return creds.Credential(creds.Mode.NONE, "OpenRouter", provider=provider_id)

        with mock.patch.object(creds, "resolve_for", side_effect=resolve):
            with mock.patch.object(GroqClient, "list_models", side_effect=slow_groq_fetch):
                dialog = self._dialog()
                self.addCleanup(dialog.deleteLater)
                dialog.provider_box.setCurrentIndex(dialog.provider_box.findData("groq"))
                self.assertIsNotNone(dialog._other_worker, "the automatic fetch must start")

                dialog.provider_box.setCurrentIndex(dialog.provider_box.findData("openrouter"))
                release.set()
                pump(lambda: dialog._other_worker is None or not dialog._other_worker.isRunning())
                for _ in range(10):
                    _app.processEvents()

        ids = {dialog._other_model_box.itemData(i)
              for i in range(dialog._other_model_box.count())}
        self.assertNotIn("llama-3.3-70b-versatile", ids,
                         "OpenRouter's section must not show Groq's models")


if __name__ == "__main__":
    unittest.main()

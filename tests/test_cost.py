"""Tests for the cost controls: caching, effort, model choice, accounting.

These matter more than most tests, because a caching regression is silent. The
requests keep succeeding and the answers keep being correct; only the bill
changes, and nothing announces it. So the shape of the outgoing request is
asserted here directly, and the session's own token accounting alongside it.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_cost -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-cost-tests-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.claude_client import AgentResponse, ClaudeClient  # noqa: E402
from app.agent.config import (  # noqa: E402
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MODELS,
    AgentConfig,
    CacheSettings,
    ContextLimits,
    describe_model,
)
from app.agent.credentials import Credential, Mode  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.agent.usage import Usage  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _Settings:
    """The bit of SettingsStore the config actually uses."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str, default: str | None = None) -> str:
        return self.values.get(key, default if default is not None else "")

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class _Recorder:
    """Stands in for the SDK client and records the request it was given."""

    def __init__(self, errors=None) -> None:
        self.calls: list[dict] = []
        self._errors = list(errors or [])
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)

        class _Usage:
            input_tokens = 10
            output_tokens = 5
            cache_read_input_tokens = 900
            cache_creation_input_tokens = 90

        class _Response:
            content = []
            stop_reason = "end_turn"
            usage = _Usage()

        return _Response()


def _client(config: AgentConfig, errors=None) -> ClaudeClient:
    """A real ClaudeClient with its SDK client swapped for a recorder.

    Constructing anthropic.Anthropic performs no network I/O, so this exercises
    the genuine request-shaping code rather than a reimplementation of it.
    """
    client = ClaudeClient(Credential(Mode.ENV_KEY, "test key", secret="sk-test"), config)
    client._client = _Recorder(errors)
    return client


def _sent(client: ClaudeClient) -> dict:
    return client._client.calls[-1]


# ---------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_defaults_are_the_recommended_model_and_medium_effort(self) -> None:
        config = AgentConfig()
        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertEqual(config.effort, DEFAULT_EFFORT)
        self.assertTrue(config.cache.prefix)
        self.assertTrue(config.cache.conversation)

    def test_prefix_cache_uses_the_one_hour_ttl(self) -> None:
        # The agent stops and waits for a human on sensitive actions; a
        # five-minute entry would routinely expire during that pause.
        self.assertEqual(AgentConfig().cache.prefix_ttl, "1h")

    def test_effort_level_is_none_when_the_model_default_is_wanted(self) -> None:
        self.assertIsNone(AgentConfig(effort="default").effort_level)
        self.assertIsNone(AgentConfig(effort="nonsense").effort_level)
        self.assertEqual(AgentConfig(effort="low").effort_level, "low")

    def test_stored_preferences_are_read(self) -> None:
        settings = _Settings({"agent_model": "claude-haiku-4-5", "agent_effort": "low"})
        config = AgentConfig.from_environment(settings)
        self.assertEqual(config.model, "claude-haiku-4-5")
        self.assertEqual(config.effort, "low")

    def test_a_nonsense_stored_effort_falls_back_to_the_default(self) -> None:
        config = AgentConfig.from_environment(_Settings({"agent_effort": "banana"}))
        self.assertEqual(config.effort, DEFAULT_EFFORT)

    def test_broken_settings_do_not_stop_the_agent_starting(self) -> None:
        class Broken:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("settings table is unreadable")

        self.assertEqual(AgentConfig.from_environment(Broken()).model, DEFAULT_MODEL)

    def test_environment_overrides_the_stored_preference(self) -> None:
        settings = _Settings({"agent_model": "claude-haiku-4-5", "agent_effort": "low"})
        os.environ["PYBROWSER_AGENT_MODEL"] = "claude-sonnet-5"
        os.environ["PYBROWSER_AGENT_EFFORT"] = "max"
        try:
            config = AgentConfig.from_environment(settings)
        finally:
            del os.environ["PYBROWSER_AGENT_MODEL"], os.environ["PYBROWSER_AGENT_EFFORT"]
        self.assertEqual(config.model, "claude-sonnet-5")
        self.assertEqual(config.effort, "max")

    def test_caching_can_be_switched_off_for_debugging(self) -> None:
        os.environ["PYBROWSER_AGENT_CACHE"] = "off"
        try:
            config = AgentConfig.from_environment(None)
        finally:
            del os.environ["PYBROWSER_AGENT_CACHE"]
        self.assertFalse(config.cache.enabled)

    def test_every_catalogue_model_is_described(self) -> None:
        for choice in MODELS:
            self.assertTrue(choice.note.strip(), choice.model_id)
            self.assertIs(describe_model(choice.model_id), choice)

    def test_an_unknown_model_is_allowed_through(self) -> None:
        # So a model released after this code was written can still be tried.
        described = describe_model("claude-something-6")
        self.assertEqual(described.model_id, "claude-something-6")


class RequestShapeTests(unittest.TestCase):
    def test_the_static_prefix_carries_a_one_hour_breakpoint(self) -> None:
        client = _client(AgentConfig())
        client.send(system="PROMPT", messages=[{"role": "user", "content": "hi"}], tools=[])
        system = _sent(client)["system"]
        self.assertEqual(system, [{
            "type": "text", "text": "PROMPT",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }])

    def test_the_conversation_tail_uses_automatic_caching(self) -> None:
        client = _client(AgentConfig())
        client.send(system="PROMPT", messages=[{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(_sent(client)["cache_control"], {"type": "ephemeral"})

    def test_five_minute_ttl_is_sent_without_a_ttl_field(self) -> None:
        config = AgentConfig(cache=CacheSettings(prefix_ttl="5m"))
        client = _client(config)
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertEqual(_sent(client)["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_caching_off_sends_a_plain_string_system(self) -> None:
        config = AgentConfig(cache=CacheSettings(prefix=False, conversation=False))
        client = _client(config)
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertEqual(_sent(client)["system"], "PROMPT")
        self.assertNotIn("cache_control", _sent(client))

    def test_effort_is_sent_when_set(self) -> None:
        client = _client(AgentConfig(effort="low"))
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertEqual(_sent(client)["output_config"], {"effort": "low"})

    def test_effort_is_omitted_when_the_model_default_is_wanted(self) -> None:
        client = _client(AgentConfig(effort="default"))
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertNotIn("output_config", _sent(client))

    def test_effort_does_not_vary_between_requests(self) -> None:
        # Changing effort mid-conversation invalidates the message cache, so it
        # must be identical on every request a session makes.
        client = _client(AgentConfig(effort="medium"))
        for _ in range(3):
            client.send(system="PROMPT", messages=[], tools=[])
        efforts = {repr(call.get("output_config")) for call in client._client.calls}
        self.assertEqual(len(efforts), 1)

    def test_usage_meters_are_read_back(self) -> None:
        client = _client(AgentConfig())
        response = client.send(system="P", messages=[], tools=[])
        self.assertEqual(response.input_tokens, 10)
        self.assertEqual(response.cache_read_tokens, 900)
        self.assertEqual(response.cache_write_tokens, 90)
        self.assertEqual(response.prompt_tokens, 1000)


class DegradationTests(unittest.TestCase):
    """A platform that rejects a cost parameter must still work, just dearer."""

    def _bad_request(self, message: str):
        import anthropic

        return anthropic.BadRequestError(
            message, response=_FakeHttpResponse(), body=None)

    def test_a_platform_rejecting_cache_control_falls_back(self) -> None:
        client = _client(AgentConfig(),
                         errors=[self._bad_request("cache_control is not supported")])
        client.send(system="PROMPT", messages=[], tools=[])
        first, second = client._client.calls
        self.assertIn("cache_control", first)
        self.assertNotIn("cache_control", second)
        self.assertEqual(second["system"], "PROMPT")

    def test_a_model_without_effort_falls_back(self) -> None:
        client = _client(AgentConfig(effort="low"),
                         errors=[self._bad_request("output_config.effort is unsupported")])
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertNotIn("output_config", client._client.calls[-1])
        # Caching must survive: only the rejected parameter is given up.
        self.assertIn("cache_control", client._client.calls[-1])

    def test_the_fallback_is_remembered_for_later_requests(self) -> None:
        client = _client(AgentConfig(),
                         errors=[self._bad_request("cache_control is not supported")])
        client.send(system="PROMPT", messages=[], tools=[])
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertNotIn("cache_control", client._client.calls[-1])
        self.assertEqual(len(client._client.calls), 3)   # 2 attempts, then 1

    def test_an_unrelated_bad_request_is_still_an_error(self) -> None:
        from app.agent.claude_client import ClaudeError

        client = _client(AgentConfig(), errors=[self._bad_request("messages: too long")])
        with self.assertRaises(ClaudeError):
            client.send(system="PROMPT", messages=[], tools=[])
        self.assertEqual(len(client._client.calls), 1)   # no retry loop


class _FakeHttpResponse:
    status_code = 400
    headers: dict = {}
    request = None

    def json(self):
        return {}


class UsageTests(unittest.TestCase):
    def _response(self, **kwargs) -> AgentResponse:
        return AgentResponse(**kwargs)

    def test_prompt_tokens_sum_all_three_input_meters(self) -> None:
        usage = Usage()
        usage.add(self._response(input_tokens=100, cache_read_tokens=8000,
                                 cache_write_tokens=400, output_tokens=50))
        self.assertEqual(usage.prompt_tokens, 8500)
        self.assertEqual(usage.output_tokens, 50)

    def test_cache_hit_rate(self) -> None:
        usage = Usage()
        usage.add(self._response(input_tokens=100, cache_read_tokens=900))
        self.assertAlmostEqual(usage.cache_hit_rate, 0.9)

    def test_hit_rate_of_an_empty_usage_is_zero_not_an_error(self) -> None:
        self.assertEqual(Usage().cache_hit_rate, 0.0)

    def test_no_price_is_invented_for_a_model_without_a_published_one(self) -> None:
        usage = Usage()
        usage.add(self._response(input_tokens=1000, output_tokens=1000))
        self.assertIsNone(usage.estimated_cost("claude-haiku-4-5"))
        self.assertNotIn("$", usage.summary("claude-haiku-4-5"))

    def test_caching_is_cheaper_than_not_caching_for_the_same_tokens(self) -> None:
        usage = Usage()
        usage.add(self._response(input_tokens=200, cache_read_tokens=20000,
                                 cache_write_tokens=800, output_tokens=300))
        cached = usage.estimated_cost("claude-opus-5")
        uncached = usage.uncached_cost("claude-opus-5")
        self.assertLess(cached, uncached)

    def test_an_all_write_turn_is_reported_as_costing_more(self) -> None:
        # The first request of a conversation writes and reads nothing, and a
        # one-hour write really does cost 2x. The estimate must not pretend
        # otherwise, or the panel would claim a saving that did not happen.
        usage = Usage()
        usage.add(self._response(cache_write_tokens=5000, output_tokens=10))
        self.assertGreater(usage.estimated_cost("claude-opus-5"),
                           usage.uncached_cost("claude-opus-5"))

    def test_summary_is_empty_before_anything_is_sent(self) -> None:
        self.assertEqual(Usage().summary("claude-opus-5"), "")

    def test_summary_is_empty_when_no_usage_was_reported(self) -> None:
        # A transport that returns no usage figures must not be reported as a
        # task that cost nothing.
        usage = Usage()
        usage.add(self._response())
        self.assertEqual(usage.summary("claude-opus-5"), "")

    def test_reset_clears_everything(self) -> None:
        usage = Usage()
        usage.add(self._response(input_tokens=5, output_tokens=5))
        usage.reset()
        self.assertEqual(usage.requests, 0)
        self.assertEqual(usage.prompt_tokens, 0)


class _StubBrowser:
    """Enough of BrowserController for a session that never runs a tool."""


class SessionAccountingTests(unittest.TestCase):
    """Usage totals and history pruning, without a browser or a network."""

    def setUp(self) -> None:
        self.config = AgentConfig(limits=ContextLimits(prune_stale_after_chars=1000))
        self.session = AgentSession(_StubBrowser(), object(), self.config)

    def tearDown(self) -> None:
        self.session.shutdown()

    def _turn(self, **kwargs) -> AgentResponse:
        return AgentResponse(stop_reason="end_turn", **kwargs)

    def test_usage_accumulates_across_turns(self) -> None:
        seen: list = []
        self.session.usage_updated.connect(seen.append)
        self.session._on_response(self._turn(input_tokens=10, cache_read_tokens=100))
        self.session._on_response(self._turn(input_tokens=10, cache_read_tokens=100))
        self.assertEqual(self.session.task_usage.requests, 2)
        self.assertEqual(self.session.task_usage.prompt_tokens, 220)
        self.assertTrue(seen)

    def test_a_new_task_resets_the_task_total_but_not_the_session_total(self) -> None:
        self.session._on_response(self._turn(input_tokens=100))
        self.session._messages.clear()
        self.session.send("do something else")
        self.assertEqual(self.session.task_usage.prompt_tokens, 0)
        self.assertEqual(self.session.session_usage.prompt_tokens, 100)

    # -- pruning ---------------------------------------------------------
    def _snapshot_history(self, count: int, size: int = 600) -> None:
        self.session._messages = [{"role": "user", "content": "start"}]
        for index in range(count):
            call_id = f"call_{index}"
            self.session._result_tools[call_id] = "browser_get_page"
            self.session._messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": call_id,
                             "name": "browser_get_page", "input": {}}],
            })
            self.session._messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id,
                             "content": "x" * size}],
            })

    def _bodies(self) -> list[str]:
        out = []
        for message in self.session._messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out.append(block["content"])
        return out

    def test_nothing_is_pruned_below_the_threshold(self) -> None:
        self._snapshot_history(2)          # 1200 chars, but only 600 superseded
        self.session._prune_snapshots()
        self.assertTrue(all(body.startswith("xxx") for body in self._bodies()))

    def test_superseded_snapshots_are_collapsed_once_they_add_up(self) -> None:
        self._snapshot_history(4)          # 1800 chars superseded, over the 1000 limit
        self.session._prune_snapshots()
        bodies = self._bodies()
        self.assertEqual(len(bodies), 4)
        self.assertTrue(all("no longer valid" in body for body in bodies[:-1]))
        # The newest snapshot is always left whole: the agent is using it now.
        self.assertTrue(bodies[-1].startswith("xxx"))

    def test_pruning_is_idempotent(self) -> None:
        self._snapshot_history(4)
        self.session._prune_snapshots()
        before = list(self._bodies())
        self.session._prune_snapshots()
        self.assertEqual(before, self._bodies())

    def test_results_from_other_tools_are_never_touched(self) -> None:
        self._snapshot_history(4)
        self.session._result_tools["call_0"] = "browser_click"
        self.session._prune_snapshots()
        self.assertTrue(self._bodies()[0].startswith("xxx"))

    def test_the_first_user_message_survives_pruning(self) -> None:
        self._snapshot_history(4)
        self.session._prune_snapshots()
        self.assertEqual(self.session._messages[0]["content"], "start")


if __name__ == "__main__":
    unittest.main()


class RequestValidityTests(unittest.TestCase):
    """Every model in the picker must get a request it can actually accept.

    This is the test that was missing. The whole suite drives a scripted
    transport, so it proved the agent *loop* correct while nothing ever checked
    that the request being assembled was valid - and `thinking: adaptive` went
    out to a model that rejects it, which broke every AI feature for anyone who
    chose the cheapest model in the list.
    """

    #: What each offered model actually accepts, written down independently of
    #: the catalogue. Asserting the request matches `choice.supports_*` would
    #: only prove the code agrees with itself - and the bug *was* a wrong flag,
    #: which such a test would have happily confirmed. Adding a model to the
    #: picker without adding it here fails, which is the point.
    SUPPORT = {
        #  model id             adaptive thinking, effort
        "claude-opus-5":        (True, True),
        "claude-sonnet-5":      (True, True),
        "claude-fable-5":       (True, True),
        "claude-haiku-4-5":     (False, False),   # predates both
    }

    def _payload(self, model_id: str) -> dict:
        client = _client(AgentConfig(model=model_id))
        client.send(system="PROMPT", messages=[{"role": "user", "content": "hi"}],
                    tools=[])
        return _sent(client)

    def test_every_offered_model_has_its_capabilities_written_down(self) -> None:
        self.assertEqual(
            sorted(choice.model_id for choice in MODELS), sorted(self.SUPPORT),
            "a model was added to the picker without stating what it accepts")

    def test_adaptive_thinking_only_goes_to_models_that_have_it(self) -> None:
        for model_id, (adaptive, _) in self.SUPPORT.items():
            with self.subTest(model=model_id):
                thinking = self._payload(model_id).get("thinking")
                if adaptive:
                    self.assertEqual(thinking, {"type": "adaptive"})
                else:
                    self.assertIsNone(
                        thinking, f"{model_id} rejects adaptive thinking")

    def test_effort_only_goes_to_models_that_have_it(self) -> None:
        for model_id, (_, effort) in self.SUPPORT.items():
            with self.subTest(model=model_id):
                payload = self._payload(model_id)
                if effort:
                    self.assertIn("output_config", payload)
                else:
                    self.assertNotIn("output_config", payload,
                                     f"{model_id} has no effort control")

    def test_the_catalogue_flags_match_the_facts(self) -> None:
        for model_id, (adaptive, effort) in self.SUPPORT.items():
            with self.subTest(model=model_id):
                choice = describe_model(model_id)
                self.assertEqual(choice.supports_adaptive_thinking, adaptive)
                self.assertEqual(choice.supports_effort, effort)

    def test_budget_tokens_is_never_sent(self) -> None:
        # Removed on every model this browser offers, and a 400 where it is.
        for choice in MODELS:
            with self.subTest(model=choice.model_id):
                thinking = self._payload(choice.model_id).get("thinking") or {}
                self.assertNotIn("budget_tokens", thinking)

    def test_sampling_parameters_are_never_sent(self) -> None:
        # temperature / top_p / top_k are rejected on the current models.
        for choice in MODELS:
            with self.subTest(model=choice.model_id):
                payload = self._payload(choice.model_id)
                for name in ("temperature", "top_p", "top_k"):
                    self.assertNotIn(name, payload)

    def test_haiku_is_the_model_without_thinking_or_effort(self) -> None:
        """Pins the specific regression rather than only the general rule.

        If a later release gives Haiku adaptive thinking, this test should be
        deleted deliberately - not discovered by a user whose agent stopped
        working.
        """
        haiku = describe_model("claude-haiku-4-5")
        self.assertFalse(haiku.supports_adaptive_thinking)
        self.assertFalse(haiku.supports_effort)
        payload = self._payload("claude-haiku-4-5")
        self.assertNotIn("thinking", payload)
        self.assertNotIn("output_config", payload)
        # Caching is orthogonal and must survive.
        self.assertIn("cache_control", payload)

    def test_an_unknown_model_still_gets_a_sendable_request(self) -> None:
        payload = self._payload("claude-something-unreleased")
        self.assertEqual(payload["model"], "claude-something-unreleased")
        self.assertEqual(payload.get("thinking"), {"type": "adaptive"})


class ThinkingDegradationTests(unittest.TestCase):
    """A 400 about `thinking` must be survivable, like the other two."""

    def _bad_request(self, message: str):
        import anthropic

        return anthropic.BadRequestError(
            message, response=_FakeHttpResponse(), body=None)

    def test_a_model_rejecting_adaptive_thinking_falls_back(self) -> None:
        # Before the fix this raised: `thinking` was not in the retry table, so
        # unlike effort and cache_control there was nothing to drop and the
        # task died on a 400 it could have recovered from.
        client = _client(
            AgentConfig(),
            errors=[self._bad_request(
                "thinking.type: 'adaptive' is not supported for this model")])
        client.send(system="PROMPT", messages=[], tools=[])
        first, second = client._client.calls
        self.assertIn("thinking", first)
        self.assertNotIn("thinking", second)
        # Only the rejected parameter is given up.
        self.assertIn("cache_control", second)

    def test_the_fallback_is_remembered(self) -> None:
        client = _client(
            AgentConfig(),
            errors=[self._bad_request("thinking is not supported")])
        client.send(system="PROMPT", messages=[], tools=[])
        client.send(system="PROMPT", messages=[], tools=[])
        self.assertNotIn("thinking", client._client.calls[-1])


class ErrorDetailTests(unittest.TestCase):
    """The API's explanation must reach the user; the exception must not."""

    def _status_error(self, body):
        import anthropic

        return anthropic.BadRequestError(
            "Error code: 400", response=_FakeHttpResponse(), body=body)

    def test_the_api_message_is_lifted_out_of_the_body(self) -> None:
        from app.agent.claude_client import api_message_of

        exc = self._status_error(
            {"type": "error",
             "error": {"type": "invalid_request_error",
                       "message": "thinking.type: 'adaptive' is unsupported"}})
        self.assertEqual(api_message_of(exc),
                         "thinking.type: 'adaptive' is unsupported")

    def test_a_body_without_a_message_yields_nothing(self) -> None:
        from app.agent.claude_client import api_message_of

        self.assertEqual(api_message_of(self._status_error(None)), "")
        self.assertEqual(api_message_of(self._status_error({"error": {}})), "")
        self.assertEqual(api_message_of(object()), "")

    def test_the_api_message_is_bounded(self) -> None:
        from app.agent.claude_client import api_message_of

        exc = self._status_error({"error": {"message": "x" * 5000}})
        self.assertLessEqual(len(api_message_of(exc)), 400)

    def test_a_rejected_request_carries_the_reason(self) -> None:
        import anthropic

        from app.agent.claude_client import ClaudeError

        body = {"error": {"message": "max_tokens: must be at least 1"}}
        client = _client(AgentConfig())
        client._client = _Recorder([
            anthropic.BadRequestError("Error code: 400",
                                      response=_FakeHttpResponse(), body=body),
            anthropic.BadRequestError("Error code: 400",
                                      response=_FakeHttpResponse(), body=body),
        ])
        with self.assertRaises(ClaudeError) as caught:
            client.send(system="P", messages=[], tools=[])
        self.assertEqual(caught.exception.api_message,
                         "max_tokens: must be at least 1")
        # The generic sentence still leads; the detail is the second line.
        self.assertIn("400", caught.exception.message)

    def test_the_raw_exception_is_never_the_api_message(self) -> None:
        """`detail` can quote a request header, and a header can carry a key.

        It stays available for a developer and must never be what the UI shows.
        """
        from app.agent.claude_client import ClaudeError

        error = ClaudeError("nope", detail="x-api-key: sk-secret",
                            api_message="max_tokens: must be at least 1")
        self.assertNotIn("sk-secret", error.api_message)
        self.assertEqual(error.detail, "x-api-key: sk-secret")

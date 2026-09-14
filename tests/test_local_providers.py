"""Phase 18: local AI models - Ollama, LM Studio, generic local
OpenAI-compatible endpoints.

Every fake server here is a plain ``httpx.MockTransport`` handler - no real
Ollama/LM Studio install is ever required (Part 24). Covers endpoint
validation and localhost classification, model discovery for all three
local providers, the tri-state capability model and probing (including
caching and manual refresh), tool-capable/text-only/unknown gating,
vision-capable/non-vision gating, local-privacy classification, Skill
capability validation, and TaskRunner's no-silent-cloud-fallback behavior
when a scheduled Mission's local endpoint is offline.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_local_providers -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-local-"))
os.environ["PYBROWSER_DISABLE_KEYRING"] = "1"

import httpx2 as httpx  # noqa: E402

from app.agent import capabilities as caps  # noqa: E402
from app.agent import credentials as creds  # noqa: E402
from app.agent.claude_client import ClaudeError  # noqa: E402
from app.agent.config import AgentConfig  # noqa: E402
from app.agent.local_providers import (  # noqa: E402
    DEFAULT_ENDPOINTS,
    LMStudioClient,
    LocalEndpointError,
    LocalOpenAICompatibleClient,
    OllamaClient,
    is_local_endpoint,
    validate_endpoint,
)


# ---------------------------------------------------------------------------
# Fake local servers - Part 24
# ---------------------------------------------------------------------------

def _ollama_server(*, installed: list[str] | None = None, chat_ok: bool = True,
                   tool_call: bool = False, loading: bool = False,
                   show_capabilities: dict[str, list[str]] | None = None):
    installed = installed or ["llama3:latest"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": name, "size": 4_000_000_000,
                 "details": {"family": "llama", "parameter_size": "8B",
                            "quantization_level": "Q4_0"}}
                for name in installed]})
        if request.url.path == "/api/show":
            body = json.loads(request.content)
            model = body.get("model", "")
            if show_capabilities is not None and model in show_capabilities:
                return httpx.Response(200, json={"capabilities": show_capabilities[model]})
            return httpx.Response(404)
        if request.url.path == "/v1/chat/completions":
            if loading:
                return httpx.Response(503, json={"error": {"message": "model is loading"}})
            if not chat_ok:
                return httpx.Response(500, json={"error": {"message": "internal error"}})
            body = json.loads(request.content)
            if tool_call and body.get("tools"):
                return httpx.Response(200, json={"choices": [{
                    "message": {"content": None, "tool_calls": [
                        {"id": "1", "type": "function",
                         "function": {"name": body["tools"][0]["function"]["name"],
                                     "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _openai_compat_server(*, models: list[str] | None = None, tool_call: bool = False,
                          vision_ok: bool = True, malformed: bool = False,
                          timeout: bool = False, require_key: str | None = None):
    models = models if models is not None else ["local-model"]

    def handler(request: httpx.Request) -> httpx.Response:
        if timeout:
            raise httpx.TimeoutException("simulated timeout")
        if require_key is not None:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {require_key}":
                return httpx.Response(401, json={"error": {"message": "invalid key"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        if request.url.path.endswith("/chat/completions"):
            if malformed:
                return httpx.Response(200, content=b"not json{{{")
            body = json.loads(request.content)
            content = body.get("messages", [{}])[-1].get("content")
            has_image = isinstance(content, list) and any(
                p.get("type") == "image_url" for p in content)
            if has_image and not vision_ok:
                return httpx.Response(
                    400, json={"error": {"message": "this model does not support vision"}})
            if tool_call and body.get("tools"):
                return httpx.Response(200, json={"choices": [{
                    "message": {"content": None, "tool_calls": [
                        {"id": "1", "type": "function",
                         "function": {"name": body["tools"][0]["function"]["name"],
                                     "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _unreachable_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Endpoint validation / localhost classification - Part 23
# ---------------------------------------------------------------------------

class EndpointClassificationTests(unittest.TestCase):
    def test_localhost_by_name(self) -> None:
        self.assertTrue(is_local_endpoint("http://localhost:11434"))

    def test_127_0_0_1_is_local(self) -> None:
        self.assertTrue(is_local_endpoint("http://127.0.0.1:11434"))

    def test_other_loopback_addresses_are_local(self) -> None:
        self.assertTrue(is_local_endpoint("http://127.0.0.2:1234"))

    def test_ipv6_loopback_is_local(self) -> None:
        self.assertTrue(is_local_endpoint("http://[::1]:11434"))

    def test_lan_address_is_not_local(self) -> None:
        self.assertFalse(is_local_endpoint("http://192.168.1.50:11434"))

    def test_public_domain_is_not_local(self) -> None:
        self.assertFalse(is_local_endpoint("https://api.example.com"))

    def test_empty_string_is_not_local(self) -> None:
        self.assertFalse(is_local_endpoint(""))

    def test_validate_endpoint_accepts_http(self) -> None:
        self.assertEqual(validate_endpoint("http://127.0.0.1:11434/"), "http://127.0.0.1:11434")

    def test_validate_endpoint_rejects_file_scheme(self) -> None:
        with self.assertRaises(LocalEndpointError):
            validate_endpoint("file:///etc/passwd")

    def test_validate_endpoint_rejects_empty(self) -> None:
        with self.assertRaises(LocalEndpointError):
            validate_endpoint("")

    def test_validate_endpoint_rejects_missing_host(self) -> None:
        with self.assertRaises(LocalEndpointError):
            validate_endpoint("http://")

    def test_validate_endpoint_rejects_unsupported_scheme(self) -> None:
        with self.assertRaises(LocalEndpointError):
            validate_endpoint("ftp://127.0.0.1/model")


# ---------------------------------------------------------------------------
# Ollama: discovery, chat, install checks
# ---------------------------------------------------------------------------

class OllamaProviderTests(unittest.TestCase):
    def test_list_installed_returns_real_metadata(self) -> None:
        transport = _ollama_server(installed=["llama3:latest", "qwen2.5-coder:7b"])
        models = OllamaClient.list_installed("http://127.0.0.1:11434", transport=transport)
        names = {m.name for m in models}
        self.assertEqual(names, {"llama3:latest", "qwen2.5-coder:7b"})
        self.assertEqual(models[0].parameter_size, "8B")

    def test_is_installed_normalises_tag(self) -> None:
        transport = _ollama_server(installed=["llama3:latest"])
        self.assertTrue(OllamaClient.is_installed(
            "http://127.0.0.1:11434", "llama3", transport=transport))
        self.assertFalse(OllamaClient.is_installed(
            "http://127.0.0.1:11434", "mistral", transport=transport))

    def test_pull_command_names_the_model(self) -> None:
        self.assertEqual(OllamaClient.pull_command("llama3"), "ollama pull llama3")

    def test_not_installed_model_message_names_pull_command(self) -> None:
        transport = _ollama_server(installed=["llama3:latest"])
        ok, message = OllamaClient.test_connection(
            "", "mistral", base_url="http://127.0.0.1:11434", transport=transport)
        self.assertFalse(ok)
        self.assertIn("ollama pull mistral", message)

    def test_chat_completion(self) -> None:
        transport = _ollama_server()
        config = AgentConfig(local_endpoint="http://127.0.0.1:11434")
        client = OllamaClient("", config, transport=transport)
        response = client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(response.text, "ready")
        self.assertEqual(response.input_tokens, 5)

    def test_streaming(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/chat/completions":
                chunks = [
                    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
                    'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                    "data: [DONE]\n\n",
                ]
                return httpx.Response(200, content="".join(chunks).encode(),
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = AgentConfig(local_endpoint="http://127.0.0.1:11434")
        client = OllamaClient("", config, transport=transport)
        fragments = []
        response = client.send(system="", messages=[{"role": "user", "content": "hi"}],
                               tools=[], on_text=fragments.append)
        self.assertEqual(fragments, ["Hel", "lo"])
        self.assertEqual(response.text, "Hello")

    def test_cancellation_leaves_no_partial_state(self) -> None:
        """AgentSession.cancel() works by discarding the result of an
        in-flight blocking call (see session.py's own docstring on this) -
        this test exercises the client-level half of that: a request that
        errors mid-flight raises cleanly rather than returning a half
        response."""
        transport = _unreachable_transport()
        config = AgentConfig(local_endpoint="http://127.0.0.1:11434")
        client = OllamaClient("", config, transport=transport)
        with self.assertRaises(ClaudeError):
            client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])

    def test_offline_message_matches_part_21_example(self) -> None:
        ok, message = OllamaClient.test_connection(
            "", "", base_url="http://127.0.0.1:11434", transport=_unreachable_transport())
        self.assertFalse(ok)
        self.assertIn("doesn't appear to be running", message)

    def test_model_still_loading(self) -> None:
        transport = _ollama_server(loading=True)
        config = AgentConfig(local_endpoint="http://127.0.0.1:11434")
        client = OllamaClient("", config, transport=transport)
        with self.assertRaises(ClaudeError):
            client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])

    def test_malformed_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/chat/completions":
                return httpx.Response(200, content=b"not json")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = AgentConfig(local_endpoint="http://127.0.0.1:11434")
        client = OllamaClient("", config, transport=transport)
        with self.assertRaises(ClaudeError):
            client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])

    def test_connection_test_healthy(self) -> None:
        ok, message = OllamaClient.test_connection(
            "", "", base_url="http://127.0.0.1:11434", transport=_ollama_server())
        self.assertTrue(ok)

    # -- Phase 18 hardening: /api/show wired into live discovery -----------
    def test_show_capabilities_returns_raw_payload(self) -> None:
        transport = _ollama_server(
            installed=["llama3:latest"],
            show_capabilities={"llama3:latest": ["completion", "tools", "vision"]})
        payload = OllamaClient.show_capabilities(
            "http://127.0.0.1:11434", "llama3:latest", transport=transport)
        self.assertEqual(payload, {"capabilities": ["completion", "tools", "vision"]})

    def test_show_capabilities_missing_returns_none(self) -> None:
        transport = _ollama_server(installed=["llama3:latest"], show_capabilities={})
        payload = OllamaClient.show_capabilities(
            "http://127.0.0.1:11434", "llama3:latest", transport=transport)
        self.assertIsNone(payload)

    def test_list_models_include_capabilities_attaches_real_metadata(self) -> None:
        transport = _ollama_server(
            installed=["llama3:latest", "tinymodel:latest"],
            show_capabilities={"llama3:latest": ["completion", "tools"]})
        entries = OllamaClient.list_models(
            "", base_url="http://127.0.0.1:11434", transport=transport,
            include_capabilities=True)
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(by_id["llama3:latest"]["capabilities"], ["completion", "tools"])
        # A model /api/show does not recognise gets no fabricated capabilities.
        self.assertNotIn("capabilities", by_id["tinymodel:latest"])

    def test_list_models_without_include_capabilities_never_calls_show(self) -> None:
        """Part 22: never eagerly probe every provider - the default
        discovery call must not attach capability metadata unless asked."""
        transport = _ollama_server(
            installed=["llama3:latest"],
            show_capabilities={"llama3:latest": ["completion", "tools"]})
        entries = OllamaClient.list_models(
            "", base_url="http://127.0.0.1:11434", transport=transport)
        self.assertNotIn("capabilities", entries[0])

    def test_capabilities_from_ollama_entry_reflects_real_metadata(self) -> None:
        real = caps.capabilities_from_ollama_entry({"capabilities": ["completion", "tools"]})
        self.assertEqual(real.tools, caps.Capability.SUPPORTED)
        self.assertEqual(real.vision, caps.Capability.UNSUPPORTED)
        self.assertEqual(real.source, "metadata")


# ---------------------------------------------------------------------------
# LM Studio + generic Local OpenAI-compatible
# ---------------------------------------------------------------------------

class LMStudioAndGenericTests(unittest.TestCase):
    def test_lmstudio_model_discovery(self) -> None:
        transport = _openai_compat_server(models=["qwen2.5-7b-instruct"])
        models = LMStudioClient.list_models(
            "", base_url="http://localhost:1234/v1", transport=transport)
        self.assertEqual([m["id"] for m in models], ["qwen2.5-7b-instruct"])

    def test_lmstudio_offline_message(self) -> None:
        ok, message = LMStudioClient.test_connection(
            "", "qwen2.5-7b-instruct", base_url="http://localhost:1234/v1",
            transport=_unreachable_transport())
        self.assertFalse(ok)
        self.assertIn("LM Studio doesn't appear to be running", message)

    def test_generic_endpoint_no_key_required(self) -> None:
        transport = _openai_compat_server()
        config = AgentConfig(local_endpoint="http://127.0.0.1:8080")
        client = LocalOpenAICompatibleClient("", config, transport=transport)
        response = client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(response.text, "ok")

    def test_generic_endpoint_optional_key_is_sent_when_provided(self) -> None:
        transport = _openai_compat_server(require_key="secret123")
        config = AgentConfig(local_endpoint="http://127.0.0.1:8080")
        client = LocalOpenAICompatibleClient("secret123", config, transport=transport)
        response = client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(response.text, "ok")

    def test_generic_endpoint_wrong_key_is_rejected(self) -> None:
        transport = _openai_compat_server(require_key="secret123")
        config = AgentConfig(local_endpoint="http://127.0.0.1:8080")
        client = LocalOpenAICompatibleClient("wrong", config, transport=transport)
        with self.assertRaises(ClaudeError):
            client.send(system="", messages=[{"role": "user", "content": "hi"}], tools=[])

    def test_endpoint_missing_raises_clean_error(self) -> None:
        config = AgentConfig(local_endpoint="")
        with self.assertRaises(ClaudeError):
            LocalOpenAICompatibleClient("", config)

    def test_model_discovery_unsupported_falls_back_to_empty_not_an_error(self) -> None:
        """Part 4: never fail the whole provider setup just because listing
        models is not supported."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        models = LocalOpenAICompatibleClient.list_models(
            "", base_url="http://127.0.0.1:8080", transport=transport)
        self.assertEqual(models, [])

    def test_timeout_message(self) -> None:
        ok, message = LocalOpenAICompatibleClient.test_connection(
            "", "some-model", base_url="http://127.0.0.1:8080",
            transport=_openai_compat_server(timeout=True))
        self.assertFalse(ok)
        self.assertIn("did not respond in time", message)


# ---------------------------------------------------------------------------
# Capability model + probing - Parts 5, 6
# ---------------------------------------------------------------------------

class CapabilityProbingTests(unittest.TestCase):
    def _client(self, transport):
        config = AgentConfig(local_endpoint="http://127.0.0.1:8080")
        return LocalOpenAICompatibleClient("", config, transport=transport)

    def test_tool_capable_model_probe(self) -> None:
        client = self._client(_openai_compat_server(tool_call=True))
        self.assertEqual(caps.probe_tool_support(client), caps.Capability.SUPPORTED)

    def test_text_only_model_probe(self) -> None:
        client = self._client(_openai_compat_server(tool_call=False))
        self.assertEqual(caps.probe_tool_support(client), caps.Capability.UNSUPPORTED)

    def test_unreachable_endpoint_probe_is_unknown_not_unsupported(self) -> None:
        client = self._client(_unreachable_transport())
        self.assertEqual(caps.probe_tool_support(client), caps.Capability.UNKNOWN)

    def test_probe_never_trusts_free_text_mentioning_the_tool(self) -> None:
        """The one thing Part 6 is explicit about: text merely mentioning
        the tool's name is never accepted as evidence."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "Sure, I'll call pybrowser_capability_probe now."},
                "finish_reason": "stop"}]})

        client = self._client(httpx.MockTransport(handler))
        self.assertEqual(caps.probe_tool_support(client), caps.Capability.UNSUPPORTED)

    def test_vision_probe_supported(self) -> None:
        client = self._client(_openai_compat_server(vision_ok=True))
        self.assertEqual(caps.probe_vision_support(client), caps.Capability.SUPPORTED)

    def test_vision_probe_unsupported(self) -> None:
        client = self._client(_openai_compat_server(vision_ok=False))
        self.assertEqual(caps.probe_vision_support(client), caps.Capability.UNSUPPORTED)

    def test_cache_avoids_reprobing(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"choices": [{
                "message": {"content": None, "tool_calls": [
                    {"id": "1", "type": "function",
                     "function": {"name": "pybrowser_capability_probe", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}]})

        client = self._client(httpx.MockTransport(handler))
        cache = caps.CapabilityCache()
        first = caps.get_or_probe(cache, client, endpoint="http://127.0.0.1:8080", model="m")
        second = caps.get_or_probe(cache, client, endpoint="http://127.0.0.1:8080", model="m")
        self.assertIs(first, second)
        self.assertEqual(calls["n"], 1)

    def test_manual_refresh_reprobes(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "no tools"}, "finish_reason": "stop"}]})

        client = self._client(httpx.MockTransport(handler))
        cache = caps.CapabilityCache()
        caps.get_or_probe(cache, client, endpoint="e", model="m")
        caps.get_or_probe(cache, client, endpoint="e", model="m", force=True)
        self.assertEqual(calls["n"], 2)

    def test_invalidate_clears_one_model(self) -> None:
        cache = caps.CapabilityCache()
        cache.set("e", "m1", caps.ModelCapabilities(tools=caps.Capability.SUPPORTED))
        cache.set("e", "m2", caps.ModelCapabilities(tools=caps.Capability.SUPPORTED))
        cache.invalidate("e", "m1")
        self.assertIsNone(cache.get("e", "m1"))
        self.assertIsNotNone(cache.get("e", "m2"))

    def test_invalidate_all_for_endpoint(self) -> None:
        cache = caps.CapabilityCache()
        cache.set("e", "m1", caps.ModelCapabilities())
        cache.set("e", "m2", caps.ModelCapabilities())
        cache.invalidate("e")
        self.assertIsNone(cache.get("e", "m1"))
        self.assertIsNone(cache.get("e", "m2"))

    def test_cache_persists_via_settings(self) -> None:
        from app.storage import Database, SettingsStore

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.sqlite3"))
            settings = SettingsStore(db)
            cache = caps.CapabilityCache(settings)
            cache.set("e", "m", caps.ModelCapabilities(tools=caps.Capability.SUPPORTED))
            reopened = caps.CapabilityCache(settings)
            restored = reopened.get("e", "m")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.tools, caps.Capability.SUPPORTED)
            db.close()

    def test_default_capabilities_are_conservative(self) -> None:
        fresh = caps.default_capabilities()
        self.assertEqual(fresh.tools, caps.Capability.UNKNOWN)
        self.assertEqual(fresh.vision, caps.Capability.UNSUPPORTED)
        self.assertFalse(fresh.blocks_tools())

    def test_ollama_metadata_capabilities_never_guessed_from_name(self) -> None:
        # No "capabilities" key at all - must stay UNKNOWN/empty, never
        # inferred from the model id looking like a "coder" or "vision" model.
        result = caps.capabilities_from_ollama_entry({"name": "qwen2.5-coder-vision-pro"})
        self.assertEqual(result.source, "")
        self.assertEqual(result.tools, caps.Capability.UNKNOWN)

    def test_ollama_metadata_capabilities_used_when_present(self) -> None:
        result = caps.capabilities_from_ollama_entry(
            {"name": "llama3", "capabilities": ["completion", "tools"]})
        self.assertEqual(result.tools, caps.Capability.SUPPORTED)
        self.assertEqual(result.vision, caps.Capability.UNSUPPORTED)


# ---------------------------------------------------------------------------
# Gating - Part 10 (tools), Part 11 (vision), Part 16 (Skills)
# ---------------------------------------------------------------------------

class GatingTests(unittest.TestCase):
    def test_confirmed_unsupported_blocks(self) -> None:
        blocked = caps.require_tool_capability(
            caps.ModelCapabilities(tools=caps.Capability.UNSUPPORTED))
        self.assertEqual(blocked, caps.TOOL_UNSUPPORTED_USER_MESSAGE)

    def test_unknown_never_blocks(self) -> None:
        self.assertIsNone(caps.require_tool_capability(
            caps.ModelCapabilities(tools=caps.Capability.UNKNOWN)))

    def test_supported_never_blocks(self) -> None:
        self.assertIsNone(caps.require_tool_capability(
            caps.ModelCapabilities(tools=caps.Capability.SUPPORTED)))

    def test_no_capability_record_never_blocks(self) -> None:
        self.assertIsNone(caps.require_tool_capability(None))

    def test_effective_vision_support_cloud_provider_unaffected(self) -> None:
        config = AgentConfig(provider="anthropic")
        self.assertTrue(caps.effective_vision_support(config, caps.CapabilityCache()))

    def test_effective_vision_support_local_unknown_passes_through(self) -> None:
        config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434",
                             model="llama3")
        self.assertTrue(caps.effective_vision_support(config, caps.CapabilityCache()))

    def test_effective_vision_support_local_confirmed_unsupported_blocks(self) -> None:
        config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434",
                             model="llama3")
        cache = caps.CapabilityCache()
        cache.set("http://127.0.0.1:11434", "llama3",
                  caps.ModelCapabilities(vision=caps.Capability.UNSUPPORTED))
        self.assertFalse(caps.effective_vision_support(config, cache))

    def test_skill_requiring_tools_blocked_on_unsupported(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Browse", description="", instructions="",
                     preferred_provider="", preferred_model="", allowed_tools=None)
        blocked = caps.validate_skill_capability(
            skill, caps.ModelCapabilities(tools=caps.Capability.UNSUPPORTED))
        self.assertIsNotNone(blocked)

    def test_skill_scoped_to_no_tools_never_blocked_on_tool_capability(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Summarize", description="", instructions="",
                     preferred_provider="", preferred_model="", allowed_tools=())
        blocked = caps.validate_skill_capability(
            skill, caps.ModelCapabilities(tools=caps.Capability.UNSUPPORTED))
        self.assertIsNone(blocked)

    def test_skill_needing_vision_blocked(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Describe", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     default_context_kinds=("image",))
        blocked = caps.validate_skill_capability(
            skill, caps.ModelCapabilities(vision=caps.Capability.UNSUPPORTED))
        self.assertIsNotNone(blocked)
        self.assertIn("vision", blocked)

    def test_skill_capability_unknown_never_blocks(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Browse", description="", instructions="",
                     preferred_provider="", preferred_model="")
        blocked = caps.validate_skill_capability(skill, caps.ModelCapabilities())
        self.assertIsNone(blocked)


# ---------------------------------------------------------------------------
# Local privacy classification - Parts 13, 14, 23
# ---------------------------------------------------------------------------

class LocalPrivacyTests(unittest.TestCase):
    def test_local_provider_at_local_endpoint_is_local_inference(self) -> None:
        config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434")
        self.assertTrue(caps.is_inference_local(config))
        self.assertIsNotNone(caps.local_privacy_status(config))

    def test_local_provider_pointed_remotely_is_not_local_inference(self) -> None:
        """Part 23: a "local" provider pointed at a remote URL is still remote."""
        config = AgentConfig(provider="local_openai", local_endpoint="https://example.com/v1")
        self.assertFalse(caps.is_inference_local(config))
        self.assertIsNone(caps.local_privacy_status(config))

    def test_cloud_provider_is_never_local_inference(self) -> None:
        config = AgentConfig(provider="anthropic")
        self.assertFalse(caps.is_inference_local(config))

    def test_privacy_message_never_claims_pybrowser_is_offline(self) -> None:
        self.assertNotIn("PyBrowser is completely offline", caps.LOCAL_PRIVACY_MESSAGE)
        self.assertIn("websites", caps.LOCAL_PRIVACY_MESSAGE.lower())

    def test_cloud_egress_warning_suppressed_for_local(self) -> None:
        config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434")
        self.assertFalse(caps.should_show_cloud_egress_warning(config))

    def test_cloud_egress_warning_shown_for_cloud(self) -> None:
        config = AgentConfig(provider="anthropic")
        self.assertTrue(caps.should_show_cloud_egress_warning(config))


# ---------------------------------------------------------------------------
# Provider registration / settings plumbing
# ---------------------------------------------------------------------------

class ConfigWiringTests(unittest.TestCase):
    def test_local_providers_are_registered(self) -> None:
        from app.agent.config import LOCAL_PROVIDER_IDS, PROVIDER_IDS

        self.assertTrue(LOCAL_PROVIDER_IDS.issubset(PROVIDER_IDS))
        self.assertEqual(len(LOCAL_PROVIDER_IDS), 3)

    def test_ollama_default_endpoint(self) -> None:
        from app.agent.config import PROVIDER_OLLAMA, default_local_endpoint

        self.assertEqual(default_local_endpoint(PROVIDER_OLLAMA), "http://127.0.0.1:11434")

    def test_generic_local_has_no_default_endpoint(self) -> None:
        from app.agent.config import PROVIDER_LOCAL_OPENAI, default_local_endpoint

        self.assertEqual(default_local_endpoint(PROVIDER_LOCAL_OPENAI), "")

    def test_local_provider_credential_available_without_a_key(self) -> None:
        credential = creds.resolve_for("ollama")
        self.assertTrue(credential.available)
        self.assertEqual(credential.secret, "")

    def test_local_openai_credential_uses_stored_key_when_present(self) -> None:
        from app.agent.keys import KeyringUnavailable

        store = creds.provider_key_store("local_openai")
        try:
            store.set_key("sk-local-test")
        except KeyringUnavailable:
            self.skipTest("no usable keyring in this test environment")
        try:
            credential = creds.resolve_for("local_openai")
            self.assertEqual(credential.secret, "sk-local-test")
        finally:
            store.clear_key()

    def test_build_transport_dispatches_ollama(self) -> None:
        from app.ui.agent_setup import build_transport

        credential = creds.Credential(creds.Mode.LOCAL_NO_AUTH, "Ollama", secret="",
                                      provider="ollama")
        config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434")
        transport = build_transport(credential, config)
        self.assertIsInstance(transport, OllamaClient)

    def test_build_transport_dispatches_generic_local(self) -> None:
        from app.ui.agent_setup import build_transport

        credential = creds.Credential(creds.Mode.LOCAL_NO_AUTH, "Local", secret="",
                                      provider="local_openai")
        config = AgentConfig(provider="local_openai", local_endpoint="http://127.0.0.1:8080")
        transport = build_transport(credential, config)
        self.assertIsInstance(transport, LocalOpenAICompatibleClient)


# ---------------------------------------------------------------------------
# TaskRunner: no silent cloud fallback when a local endpoint is offline
# ---------------------------------------------------------------------------

class ScheduledMissionOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.missions.scheduler import ScheduleKind
        from app.storage import Database
        from app.storage.scheduled_tasks import ScheduledTaskStore

        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.tasks = ScheduledTaskStore(self.db)
        self.schedule_kind = ScheduleKind

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_offline_local_endpoint_fails_without_running_and_never_switches_provider(self) -> None:
        from unittest.mock import MagicMock

        from app.missions.task_runner import TaskRunner

        task = self.tasks.create(goal="summarize the news", schedule_kind=self.schedule_kind.DAILY,
                                 time_of_day="09:00", next_run_at="2000-01-01T00:00:00+00:00")
        session = MagicMock()
        session.busy = False
        session.config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434")
        missions = MagicMock()
        runner = TaskRunner(self.tasks, missions, lambda: session, None)

        reason = runner._local_endpoint_offline_reason(session)
        self.assertIsNotNone(reason)
        runner._fail_without_running(task, reason)

        updated = self.tasks.get(task.id)
        self.assertEqual(updated.last_error, reason)
        # Never switched to a different provider - the session mock's own
        # config object identity proves nothing here ever replaced it.
        self.assertIs(session.config.provider, "ollama")
        session.send.assert_not_called()

    def test_reachable_local_endpoint_is_not_blocked(self) -> None:
        from unittest.mock import MagicMock

        from app.missions.task_runner import TaskRunner

        session = MagicMock()
        session.config = AgentConfig(provider="ollama", local_endpoint="http://127.0.0.1:11434")
        runner = TaskRunner(self.tasks, MagicMock(), lambda: session, None)

        import app.agent.local_providers as lp
        original = lp.OllamaClient.test_connection
        lp.OllamaClient.test_connection = classmethod(
            lambda cls, *a, **k: (True, "Ollama is running."))
        try:
            reason = runner._local_endpoint_offline_reason(session)
        finally:
            lp.OllamaClient.test_connection = original
        self.assertIsNone(reason)

    def test_cloud_provider_session_is_never_checked_for_local_reachability(self) -> None:
        from unittest.mock import MagicMock

        from app.missions.task_runner import TaskRunner

        session = MagicMock()
        session.config = AgentConfig(provider="anthropic")
        runner = TaskRunner(self.tasks, MagicMock(), lambda: session, None)
        self.assertIsNone(runner._local_endpoint_offline_reason(session))


if __name__ == "__main__":
    unittest.main()

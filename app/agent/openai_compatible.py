"""Groq and OpenRouter: one adapter, because they speak the same wire format.

Both expose an OpenAI-compatible ``/chat/completions`` endpoint. Neither of
them is where the translation work is interesting - the interesting part is
that their wire shape for a tool call is *not* Anthropic's: an assistant turn
with a tool call carries a sibling ``tool_calls`` array, not a ``tool_use``
block inside ``content``. AgentSession only ever echoes ``response.raw_content``
back as the ``content`` of an assistant message (see
``AgentSession._advance`` / ``_next_tool``), so this module's whole job is
translating in both directions at its own boundary:

* **out** - PyBrowser's Anthropic-shaped ``tools``/``messages`` -> this wire
  format.
* **in** - this wire format's response -> ``AgentResponse`` whose
  ``raw_content`` is Anthropic-shaped blocks (``text`` / ``tool_use``), so it
  round-trips through the session exactly like a real Anthropic turn would.

Nothing above this module - ``AgentSession``, ``ToolRegistry``, Missions,
``safety.py`` - needs to know any of this happened. See ``ClaudeTransport``
in ``claude_client.py`` for the contract every provider client implements,
and reuses: ``ClaudeError``, ``ToolCall`` and ``AgentResponse`` all come from
there, so error shape and safety-relevant surface are identical across
providers.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx

from app.agent.claude_client import AgentResponse, ClaudeError, ToolCall

#: How finish_reason maps onto the stop_reason vocabulary the rest of the
#: codebase already understands (see AgentResponse.wants_tools and
#: AgentSession's handling of stop_reason).
_STOP_REASONS = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}


def _block_get(block: Any, key: str, default: Any = "") -> Any:
    """A content block, whether it's a plain dict or an attribute object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def tools_param(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PyBrowser's Anthropic-shaped tool schemas -> OpenAI function-tool shape."""
    return [
        {"type": "function",
         "function": {"name": tool["name"],
                     "description": tool.get("description", ""),
                     "parameters": tool.get("input_schema", {})}}
        for tool in tools
    ]


def _assistant_turn(blocks: list[Any]) -> dict[str, Any]:
    """One Anthropic-shaped assistant content list -> one OpenAI-shaped turn."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        kind = _block_get(block, "type")
        if kind == "text":
            text = _block_get(block, "text")
            if text:
                text_parts.append(text)
        elif kind == "tool_use":
            arguments = _block_get(block, "input") or {}
            tool_calls.append({
                "id": _block_get(block, "id"),
                "type": "function",
                "function": {"name": _block_get(block, "name"),
                            "arguments": json.dumps(arguments)},
            })
    turn: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if tool_calls:
        turn["tool_calls"] = tool_calls
    return turn


def _tool_result_turns(blocks: list[Any]) -> list[dict[str, Any]]:
    """Anthropic-shaped ``tool_result`` blocks -> OpenAI ``role: tool`` turns.

    These blocks are always plain dicts - AgentSession builds them itself in
    ``_record_result`` - so no attribute-style branch is needed here, unlike
    ``_assistant_turn`` which reads blocks a model produced.
    """
    turns = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        turns.append({"role": "tool", "tool_call_id": block.get("tool_use_id"),
                      "content": content})
    return turns


def messages_param(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PyBrowser's Anthropic-shaped conversation -> an OpenAI-shaped one."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str) or content is None:
            out.append({"role": role, "content": content or ""})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            out.append(_assistant_turn(content))
        else:
            out.extend(_tool_result_turns(content))
    return out


def normalise(payload: dict[str, Any]) -> AgentResponse:
    """An OpenAI-shaped chat-completion response -> AgentResponse.

    ``raw_content`` is rebuilt as Anthropic-shaped blocks - the same ``id``
    is reused on the ``tool_use`` block and the ``ToolCall`` so a later
    ``tool_result`` correlates correctly either way.
    """
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    raw_blocks: list[dict[str, Any]] = []
    calls: list[ToolCall] = []
    if text:
        raw_blocks.append({"type": "text", "text": text})
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = call.get("id") or f"call_{index}"
        name = function.get("name", "")
        raw_blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    finish = choice.get("finish_reason") or ""
    usage = payload.get("usage") or {}
    return AgentResponse(
        text=text.strip(),
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(finish, finish or "end_turn"),
        raw_content=raw_blocks,
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
    )


def error_message(response: "httpx.Response") -> str:
    """The provider's own sentence about a failure, from the parsed body.

    Same reasoning as ``api_message_of`` in claude_client.py: never the raw
    exception or response text, which can echo request headers.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message.strip()[:400]
    if isinstance(error, str):
        return error.strip()[:400]
    return ""


#: The friendly sentence shown for a request an API rejected specifically
#: because the model does not do tool calling - as opposed to a bad key, a
#: missing model, or a rate limit, which get their own messages.
TOOL_UNSUPPORTED_MESSAGE = ("This model does not support the custom tools PyBrowser "
                            "requires. Choose another model.")


def _looks_like_tool_unsupported(api_message: str) -> bool:
    """Is this 400 the provider saying it cannot do tool/function calling?

    Matched on the words a rejection like this actually uses, not on a
    provider-specific error code - Groq and OpenRouter (and whatever they
    proxy to) do not agree on one.
    """
    lowered = api_message.lower()
    return "tool" in lowered and ("not support" in lowered or "does not support" in lowered
                                  or "unsupported" in lowered or "cannot" in lowered
                                  or "no tool" in lowered)


def pretty_label(model_id: str) -> str:
    """A human-readable label for a raw model id, with the id kept visible.

    Deliberately mechanical - splitting on ``/`` and ``-`` and title-casing
    words - rather than a hand-maintained name-to-label table, which would
    either miss every model added after this code was written or invent
    marketing names nobody confirmed. The exact id always follows, so the
    guess costs nothing if it reads oddly.
    """
    if "/" in model_id:
        _vendor, _, rest = model_id.partition("/")
    else:
        rest = model_id
    words = [w.upper() if w.isalpha() and len(w) <= 3 else w.capitalize()
            for w in rest.replace("_", "-").split("-") if w]
    friendly = " ".join(words) if words else model_id
    return f"{friendly} — {model_id}"


class OpenAICompatibleClient:
    """One HTTP client, subclassed per provider for base_url and labelling.

    Implements the same ``send()`` contract as ``ClaudeClient`` -
    ``ClaudeTransport`` in claude_client.py - so ``AgentSession`` cannot tell
    the difference. Takes the API key directly rather than a ``Credential``:
    unlike Anthropic, these providers have exactly one way in (a key), so the
    richer credential cascade would be dead weight here.
    """

    #: Overridden by each subclass.
    base_url: str = ""
    label: str = "OpenAI-compatible provider"

    #: model ids known not to support PyBrowser's custom tool interface,
    #: regardless of what a provider's own /models listing claims - e.g.
    #: Groq's "compound" models run their own internal tool-use loop server
    #: side and reject a caller-supplied tool schema outright. Checked before
    #: any other capability logic, and filtered out of both the seed list and
    #: the live list entirely - not merely disabled - because a model in this
    #: set is not a normal compatible choice under any circumstance.
    DENYLIST: frozenset[str] = frozenset()
    DENYLIST_REASON = "does not support the custom tool interface PyBrowser requires"

    @classmethod
    def is_denylisted(cls, model_id: str) -> bool:
        return (model_id or "").strip().lower() in cls.DENYLIST

    def __init__(self, api_key: str, config, *, transport: "httpx.BaseTransport | None" = None) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ClaudeError(f"No {self.label} API key is configured.")
        self._api_key = key
        self.config = config
        self._model = config.model
        self.model_choice = None
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=config.request_timeout_s,
            headers=self._headers(),
            transport=transport,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    # -- the protocol -------------------------------------------------------
    def send(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             on_text=None) -> AgentResponse:
        """One round-trip. ``on_text`` is accepted for protocol compatibility
        but unused - streaming is a fast-follow, not required for the agent
        loop to work; AgentSession already falls back to blocking calls for
        any transport that doesn't stream (see ``_accepts_streaming``)."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages_param(system, messages),
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            body["tools"] = tools_param(tools)
        try:
            response = self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise ClaudeError(f"{self.label} took too long to respond.",
                              retryable=True, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ClaudeError(f"Could not reach {self.label}. Check the network connection.",
                              retryable=True, detail=str(exc)) from exc
        return self._handle_response(response)

    def _handle_response(self, response: "httpx.Response") -> AgentResponse:
        status = response.status_code
        if status == 401:
            raise ClaudeError(
                f"{self.label} rejected the API key. Check it in "
                "Tools → Configure AI Agent.", detail=response.text[:1000])
        if status == 403:
            raise ClaudeError(
                f"This {self.label} key does not have permission for this model.",
                detail=response.text[:1000], api_message=error_message(response))
        if status == 404:
            raise ClaudeError(
                f"The model '{self._model}' is not available via {self.label}. "
                "Choose another in Tools → Configure AI Agent.",
                detail=response.text[:1000])
        if status == 429:
            api_message = error_message(response)
            quota = "quota" in api_message.lower() or "credit" in api_message.lower()
            raise ClaudeError(
                f"{self.label}'s free quota is exhausted for this key." if quota else
                f"{self.label} is rate limiting this key. Wait a moment and try again.",
                retryable=not quota, detail=response.text[:1000], api_message=api_message)
        if status >= 500:
            raise ClaudeError(f"{self.label} returned a server error.",
                              retryable=True, detail=response.text[:1000])
        if status >= 400:
            api_message = error_message(response)
            if _looks_like_tool_unsupported(api_message):
                raise ClaudeError(TOOL_UNSUPPORTED_MESSAGE,
                                  detail=response.text[:1000], api_message=api_message)
            raise ClaudeError(
                f"{self.label} rejected the request ({status}).",
                detail=response.text[:1000], api_message=api_message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClaudeError(f"{self.label} sent back something PyBrowser could not read.",
                              detail=str(exc)) from exc
        return normalise(payload)

    # -- capability discovery, for the settings dialog ----------------------
    @classmethod
    def list_models(cls, api_key: str, *, timeout: float = 10.0,
                    transport: "httpx.BaseTransport | None" = None) -> list[dict[str, Any]]:
        """The provider's own live model list. Never raises - a UI populating
        a dropdown gets an empty list on any failure and falls back to a
        small offline seed instead of blowing up the dialog."""
        try:
            with httpx.Client(base_url=cls.base_url, timeout=timeout, transport=transport,
                              headers={"Authorization": f"Bearer {(api_key or '').strip()}"}) as client:
                response = client.get("/models")
                if response.status_code != 200:
                    return []
                data = response.json().get("data", [])
                if not isinstance(data, list):
                    return []
                return [entry for entry in data
                       if not cls.is_denylisted(entry.get("id", ""))]
        except Exception:  # noqa: BLE001 - a listing failure is not fatal
            return []

    @classmethod
    def test_connection(cls, api_key: str, model: str, *, timeout: float = 20.0,
                        transport: "httpx.BaseTransport | None" = None) -> tuple[bool, str]:
        """A minimal real round trip: one short message, one no-op tool
        offered but not forced. The honest way to answer "does this key and
        model actually support what PyBrowser needs" - a model list does not
        reliably say so, but a real request does.
        """
        key = (api_key or "").strip()
        if not key:
            return False, f"No {cls.label} API key entered yet."
        if not (model or "").strip():
            return False, "Choose a model first."
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready."}],
            "max_tokens": 16,
            "tools": [{"type": "function", "function": {
                "name": "noop",
                "description": "Does nothing. Offered only to prove tool calling works.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }}],
        }
        try:
            with httpx.Client(base_url=cls.base_url, timeout=timeout, transport=transport,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"}) as client:
                response = client.post("/chat/completions", json=body)
        except Exception as exc:  # noqa: BLE001 - reported to the user, not raised
            return False, f"Could not reach {cls.label}: {exc}"
        if response.status_code == 200:
            return True, "Connected. The model accepted a tool-calling request."
        api_message = error_message(response)
        if _looks_like_tool_unsupported(api_message):
            return False, TOOL_UNSUPPORTED_MESSAGE
        message = api_message or response.text[:300]
        return False, f"{cls.label} rejected the request ({response.status_code}): {message}"

    # -- capability metadata, for the model dropdown ------------------------
    @classmethod
    def capability_of(cls, entry: dict[str, Any]) -> tuple[bool, str]:
        """(supports_tools, a short note) for one entry from list_models().

        Base implementation: optimistic and says so. Each subclass overrides
        this with whatever its own /models response actually reports -
        deliberately not guessed once and shared, because the two providers'
        listings carry different (or no) capability information.
        """
        return True, "tool support unconfirmed - use Test Connection to check"

    @classmethod
    def seed_models(cls) -> list[dict[str, str]]:
        """A tiny offline placeholder shown before the live list loads, or if
        it fails. NOT a claim that these ids are current - the live list
        from list_models() is authoritative and always preferred; this only
        keeps the dropdown non-empty for a key that has not been tested yet.
        """
        return []


class GroqClient(OpenAICompatibleClient):
    """https://console.groq.com - OpenAI-compatible, generous free tier."""

    base_url = "https://api.groq.com/openai/v1"
    label = "Groq"

    #: Groq's "compound" models run their own server-side agentic tool loop
    #: (web search, code execution) and do not accept a caller-supplied
    #: custom tool schema - PyBrowser's tools never reach them, so a request
    #: fails outright. Excluded from every list this client produces, seeded
    #: or live, never merely disabled.
    DENYLIST = frozenset({"groq/compound", "groq/compound-mini",
                          "compound", "compound-mini"})

    #: Groq's /models listing does not report tool-calling support per
    #: model (unlike OpenRouter's supported_parameters). This name-based
    #: heuristic only rules out entries that are obviously not chat models
    #: at all - everything else is left to Test Connection, which is the
    #: only way to actually know.
    _NOT_CHAT_MODELS = ("whisper", "tts", "guard", "moderation", "prompt-guard")

    #: A small, curated starting point so the dropdown is never empty before
    #: a key is entered or a live refresh completes. "Refresh model list"
    #: replaces this with Groq's own current listing, which is authoritative -
    #: this is only a seed, not a claim these ids will always exist.
    _SEED_MODEL_IDS = (
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b",
        "qwen/qwen3.8-27b",
    )

    @classmethod
    def capability_of(cls, entry: dict[str, Any]) -> tuple[bool, str]:
        model_id = (entry.get("id") or "").lower()
        if cls.is_denylisted(model_id):
            return False, cls.DENYLIST_REASON
        if any(marker in model_id for marker in cls._NOT_CHAT_MODELS):
            return False, "not a chat model (audio/moderation) - cannot run the agent loop"
        return True, "tool support unconfirmed - use Test Connection to check"

    @classmethod
    def seed_models(cls) -> list[dict[str, str]]:
        return [{"id": model_id} for model_id in cls._SEED_MODEL_IDS]


class OpenRouterClient(OpenAICompatibleClient):
    """https://openrouter.ai - a router in front of many providers, several
    with a free tier. Model availability changes often, which is exactly
    what ``list_models``/``test_connection`` exist to check live rather than
    trust a hardcoded catalogue for."""

    base_url = "https://openrouter.ai/api/v1"
    label = "OpenRouter"

    #: OpenRouter can itself route to Groq's "compound" models under the
    #: same names - excluded here for the identical reason (see GroqClient):
    #: they run their own server-side tool loop and reject a caller-supplied
    #: custom tool schema.
    DENYLIST = frozenset({"groq/compound", "groq/compound-mini"})

    @classmethod
    def capability_of(cls, entry: dict[str, Any]) -> tuple[bool, str]:
        model_id = (entry.get("id") or "").lower()
        if cls.is_denylisted(model_id):
            return False, cls.DENYLIST_REASON
        supported = entry.get("supported_parameters")
        pricing = entry.get("pricing") or {}
        free = str(pricing.get("prompt", "")) in ("0", "0.0", "0.000000") and \
            str(pricing.get("completion", "")) in ("0", "0.0", "0.000000")
        note = "free" if free else ""
        if isinstance(supported, list):
            if "tools" not in supported:
                return False, "this model does not report tool-calling support"
            return True, note or "reports tool-calling support"
        return True, (note + " - tool support unconfirmed" if note else
                      "tool support unconfirmed - use Test Connection to check")

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        # Recommended by OpenRouter for attribution; requests work without
        # them, so their absence is not treated as an error anywhere here.
        headers["HTTP-Referer"] = "https://github.com/Jeremydabuilder/Ai-Browser-Test"
        headers["X-Title"] = "PyBrowser"
        return headers

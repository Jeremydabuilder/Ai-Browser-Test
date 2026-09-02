"""Talking to Claude.

Two things live here: a small normalised view of a model response, and the
Anthropic SDK client that produces one.

The normalisation matters for two reasons that are not about tidiness:

* **Testability.** ``AgentSession`` depends on the ``ClaudeTransport`` protocol,
  not on the SDK. The agent tests inject a scripted fake transport, so they are
  deterministic, offline, and cost nothing to run.
* **Thread hygiene.** The session inspects plain dataclasses. The SDK's own
  objects are carried through untouched in ``raw_content`` and handed straight
  back to the API on the next turn, exactly as the manual-loop contract
  requires - we normalise *alongside* them, never instead of them.

This module performs blocking network I/O. It is only ever called from the
worker thread in ``session.py``; nothing here touches Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent.config import AgentConfig, describe_model


@dataclass(frozen=True)
class ToolCall:
    """One tool the model wants run."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResponse:
    """One model turn, in terms the session understands."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    #: The SDK's own content blocks, passed back verbatim on the next request.
    raw_content: Any = None
    #: Tokens billed at full price - the uncached remainder only, not the
    #: whole prompt. See app/agent/usage.py.
    input_tokens: int = 0
    output_tokens: int = 0
    #: Tokens served from the prompt cache, at about a tenth of the input price.
    cache_read_tokens: int = 0
    #: Tokens written to the cache, billed at 1.25x (5m TTL) or 2x (1h).
    cache_write_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def prompt_tokens(self) -> int:
        """Everything that went in, cached or not."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


class ClaudeError(RuntimeError):
    """A failure talking to Claude, already phrased for a person.

    ``retryable`` distinguishes "the network hiccuped, try again" from "your API
    key is wrong", which is the only distinction the UI needs to make.
    """

    def __init__(self, message: str, *, retryable: bool = False, detail: str = "",
                 api_message: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        #: The whole exception, for a developer. Never shown: an SDK exception
        #: can quote a request header, and a header can carry a credential.
        self.detail = detail
        #: Just the API's own sentence about the request, lifted out of the
        #: parsed error body - no headers, no request echo, nothing from the
        #: credential. Safe to put in front of a person, and the difference
        #: between "Claude rejected the request (400)" and knowing which
        #: parameter it objected to.
        self.api_message = api_message


def api_message_of(exc: Any) -> str:
    """The API's own explanation, from the parsed error body.

    Deliberately not ``str(exc)``: that carries whatever the SDK chose to put
    in the exception, which can include request metadata. The body's
    ``error.message`` is the server describing the request it refused, and
    nothing else.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:400]
    return ""


class ClaudeTransport(Protocol):
    """What the session needs from a model. Implemented for real below.

    ``on_text`` is called with each fragment of the answer as it arrives, from
    the worker thread. Passing None asks for a single blocking request instead,
    which is what the tests use - a scripted transport has nothing to stream.
    """

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text=None,
    ) -> AgentResponse: ...


class ClaudeClient:
    """The real transport, backed by the official Anthropic Python SDK.

    Takes a ``Credential`` rather than an API key, because a key is only one of
    several ways in - and the worst of them. See ``app/agent/credentials.py``.
    """

    def __init__(self, credential, config: AgentConfig | None = None) -> None:
        from app.agent.credentials import Credential, Mode

        # A bare string still works, so existing callers and tests are unaffected.
        if isinstance(credential, str):
            credential = Credential(Mode.ENV_KEY, "API key", secret=credential)
        if credential is None or not credential.available:
            raise ClaudeError("No way to authenticate to Claude is configured.")

        self.config = config or AgentConfig()
        self.credential = credential
        # Imported here so the browser starts fine without the SDK installed;
        # the agent panel reports it as unconfigured instead of the app dying.
        import anthropic

        self._anthropic = anthropic
        self._client = self._build_client(anthropic, credential)
        self._model = self._model_id(credential, self.config.model)
        self.model_choice = describe_model(self.config.model)
        #: Optional request parameters this platform has rejected. See _create.
        self._unsupported: set[str] = set()

    def _build_client(self, anthropic, credential):
        """Construct whichever SDK client this credential calls for."""
        from app.agent.credentials import Mode

        common = {"timeout": self.config.request_timeout_s,
                  "max_retries": self.config.max_retries}
        try:
            if credential.mode in (Mode.KEYRING, Mode.ENV_KEY):
                return anthropic.Anthropic(api_key=credential.secret, **common)
            if credential.mode == Mode.AUTH_TOKEN:
                return anthropic.Anthropic(auth_token=credential.secret, **common)
            if credential.mode == Mode.OAUTH_PROFILE:
                # No secret passed: the SDK reads the profile written by
                # `ant auth login` and refreshes the token itself.
                return anthropic.Anthropic(**common)
            if credential.mode == Mode.BEDROCK:
                return anthropic.AnthropicBedrockMantle(
                    aws_region=credential.region, **common)
            if credential.mode == Mode.VERTEX:
                return anthropic.AnthropicVertex(
                    project_id=credential.project, region=credential.region, **common)
        except Exception as exc:  # noqa: BLE001
            raise ClaudeError(
                f"Could not set up {credential.label}.", detail=str(exc)) from exc
        raise ClaudeError(f"Unsupported credential type '{credential.mode}'.")

    @staticmethod
    def _model_id(credential, model: str) -> str:
        """Bedrock namespaces its model ids; the others use the plain one."""
        from app.agent.credentials import Mode

        if credential.mode == Mode.BEDROCK and not model.startswith("anthropic."):
            return f"anthropic.{model}"
        return model

    # -- request shaping --------------------------------------------------
    #
    # Everything in this block exists to make the same conversation cost less.
    # None of it changes what the model is asked to do.

    def _system_param(self, system: str):
        """`system` as the SDK wants it, carrying the prefix cache breakpoint.

        Tools render before `system`, so one marker on the last (here, only)
        system block caches the tool schemas *and* the system prompt together -
        the entire static prefix, which is byte-identical on every request this
        browser will ever send. That prefix is re-sent on every turn of every
        task; caching it is the single largest saving available and costs
        nothing in quality.
        """
        cache = self.config.cache
        if not cache.prefix or "cache_control" in self._unsupported:
            return system
        control: dict[str, Any] = {"type": "ephemeral"}
        if cache.prefix_ttl == "1h":
            control["ttl"] = "1h"
        return [{"type": "text", "text": system, "cache_control": control}]

    def _thinking_param(self) -> dict[str, Any]:
        """``thinking``, on the models that have it.

        Adaptive thinking arrived with the 4.6 generation: it is the whole
        configuration there, and `budget_tokens` is rejected. On anything older
        the reverse holds - adaptive is rejected - so the parameter is omitted
        and the model simply answers without extended thinking.

        Sending it unconditionally is what broke every AI feature for anyone
        who selected Haiku 4.5: the request 400d, and unlike `effort` and
        `cache_control` there was nothing in the retry table to drop, so it
        failed for good.
        """
        if ("thinking" in self._unsupported
                or not self.model_choice.supports_adaptive_thinking):
            return {}
        return {"thinking": {"type": "adaptive"}}

    #: Beta flags the two context-management features need. Sent only when the
    #: features are, so an ordinary request stays on the stable endpoint.
    _CLEARING_BETA = "context-management-2025-06-27"
    _COMPACTION_BETA = "compact-2026-01-12"

    def _context_management(self) -> dict[str, Any]:
        """The server-side replacement for editing our own transcript.

        Order matters when both strategies are present, and only in one
        direction: thinking-clearing would have to come first. We do not clear
        thinking - the models we support bind it to the conversation, and the
        API drops what a model cannot read anyway - so the list is at most
        clearing followed by compaction.
        """
        context = self.config.context
        if "context_management" in self._unsupported or not context.enabled:
            return {}
        edits: list[dict[str, Any]] = []
        if context.clear_tool_results:
            edits.append({
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens",
                            "value": context.clear_after_tokens},
                "keep": {"type": "tool_uses",
                         "value": context.keep_recent_tool_uses},
                "clear_tool_inputs": context.clear_tool_inputs,
            })
        if context.compact:
            edits.append({
                "type": "compact_20260112",
                "trigger": {"type": "input_tokens",
                            "value": context.compact_after_tokens},
            })
        return {"context_management": {"edits": edits}} if edits else {}

    def _betas(self, managing: bool) -> list[str]:
        if not managing:
            return []
        context = self.config.context
        flags = []
        if context.clear_tool_results:
            flags.append(self._CLEARING_BETA)
        if context.compact:
            flags.append(self._COMPACTION_BETA)
        return flags

    @property
    def manages_context_server_side(self) -> bool:
        """True when the server is keeping the conversation small for us.

        The session asks, because the answer decides whether it may still trim
        the transcript itself. It goes False if a platform rejects the
        parameter, and the old client-side trimming resumes.
        """
        return bool(self._context_management())

    def _extra_params(self) -> dict[str, Any]:
        """Optional top-level parameters, omitted where unsupported."""
        params: dict[str, Any] = {}
        if self.config.cache.conversation and "cache_control" not in self._unsupported:
            # Automatic caching: the breakpoint is placed on the last cacheable
            # block and moves forward as the conversation grows, which is the
            # multi-turn pattern with no marker bookkeeping on our side. It sits
            # after the system prefix, so the longer-lived prefix entry still
            # precedes the shorter-lived conversation entry, as required.
            params["cache_control"] = {"type": "ephemeral"}
        effort = self.config.effort_level
        if (effort and self.model_choice.supports_effort
                and "output_config" not in self._unsupported):
            # Pinned for the life of the client on purpose: changing effort
            # between requests invalidates the message cache, which would cost
            # far more than the thinking it saves.
            params["output_config"] = {"effort": effort}
        return params

    def _create(self, *, system: str, messages: list, tools: list, on_text=None):
        """The API call, retried once without whatever the platform rejected.

        The cost parameters are not universally available - the older Amazon
        Bedrock integration rejects a top-level `cache_control` outright, and
        not every model has an `effort` control. Rather than keep a table of
        which platform supports what and be wrong about it, we ask and believe
        the answer: on a 400 naming one of these parameters, drop it, remember
        that for the rest of the session, and send the request again. The task
        then costs more and works, instead of failing.
        """
        while True:
            managing = self._context_management()
            kwargs = dict(
                model=self._model,
                max_tokens=self.config.max_tokens,
                system=self._system_param(system),
                messages=messages,
                tools=tools,
                **self._thinking_param(),
                **self._extra_params(),
                **managing,
            )
            # Context management lives on the beta endpoint. Everything else is
            # identical, so the stable path stays the default and we only move
            # across when there is something to move across for.
            betas = self._betas(bool(managing))
            api = self._client.beta.messages if betas else self._client.messages
            if betas:
                kwargs["betas"] = betas
            try:
                if on_text is None:
                    return api.create(**kwargs)
                # Streaming: hand each fragment over as it arrives, then take
                # the assembled message. The loop needs the whole message
                # regardless - tool_use blocks only make sense complete - so
                # streaming changes what the user sees, not what the loop gets.
                with api.stream(**kwargs) as stream:
                    for fragment in stream.text_stream:
                        if fragment:
                            on_text(fragment)
                    return stream.get_final_message()
            except self._anthropic.BadRequestError as exc:
                offender = self._rejected_parameter(str(exc))
                if offender is None:
                    raise
                self._unsupported.add(offender)

    def _rejected_parameter(self, detail: str) -> str | None:
        """Which optional parameter a 400 is complaining about, if any.

        Each parameter is given up at most once, so a genuinely malformed
        request still surfaces as an error rather than looping forever.
        """
        text = detail.lower()
        for name, needles in (
            ("cache_control", ("cache_control", "cache control", "prompt caching")),
            ("output_config", ("output_config", "effort")),
            # A model the catalogue does not know about - one set through the
            # environment, or added by a later release - can refuse adaptive
            # thinking too. Dropping it costs quality, not correctness.
            ("thinking", ("thinking",)),
            # A platform without the betas, or with them behind a flag this
            # account does not have. Giving up costs money, not correctness:
            # the session goes back to trimming the transcript itself.
            ("context_management", ("context_management", "context management",
                                    "compact", "clear_tool_uses",
                                    "context-management")),
        ):
            if name in self._unsupported:
                continue
            if any(needle in text for needle in needles):
                return name
        return None

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text=None,
    ) -> AgentResponse:
        """One round-trip. Raises ClaudeError on failure.

        With ``on_text`` the answer is streamed and each fragment handed over as
        it arrives; without it the call blocks until the whole message is ready.
        Either way the return value is the same, so the agent loop does not
        care which happened.
        """
        anthropic = self._anthropic
        try:
            response = self._create(system=system, messages=messages, tools=tools,
                                    on_text=on_text)
        except anthropic.AuthenticationError as exc:
            raise ClaudeError(
                f"Claude rejected the credential ({self.credential.label}). "
                "Check it in Tools \u2192 Configure AI Agent.",
                detail=str(exc),
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ClaudeError(
                "This API key does not have permission to use the Claude API.",
                detail=str(exc),
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ClaudeError(
                f"The model '{self._model}' is not available via "
                f"{self.credential.label}. Choose another in "
                "Tools \u2192 Configure AI Agent.",
                detail=str(exc),
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError(
                "Claude is rate limiting this key. Wait a moment and try again.",
                retryable=True, detail=str(exc),
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise ClaudeError(
                "Claude took too long to respond.", retryable=True, detail=str(exc),
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(
                "Could not reach the Claude API. Check the network connection.",
                retryable=True, detail=str(exc),
            ) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500
            raise ClaudeError(
                "Claude returned a server error." if retryable
                else f"Claude rejected the request ({exc.status_code}).",
                retryable=retryable, detail=str(exc),
                api_message=api_message_of(exc),
            ) from exc

        return self._normalise(response)

    @staticmethod
    def _normalise(response: Any) -> AgentResponse:
        """Flatten an SDK message into the session's view of a turn.

        Thinking blocks are skipped: the agent's internal reasoning is not
        shown to the user and not needed by the session. They stay in
        ``raw_content`` so the next request echoes them back unchanged, which
        is what the API expects when continuing on the same model.
        """
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content or []:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "tool_use":
                # Inputs arrive already parsed by the SDK; never string-match them.
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))
        meters = ClaudeClient._meters(getattr(response, "usage", None))
        return AgentResponse(
            text="\n".join(t for t in texts if t).strip(),
            tool_calls=calls,
            stop_reason=getattr(response, "stop_reason", "") or "",
            raw_content=response.content,
            **meters,
        )

    @staticmethod
    def _meters(usage: Any) -> dict[str, int]:
        """What this turn actually cost.

        A compacted turn bills for two passes - summarising the old
        conversation, then answering with the summary - and the API reports the
        first one only in ``usage.iterations``. The top-level counters exclude
        it, so reading them alone under-reports a compaction by the size of the
        whole conversation it just condensed, which is the most expensive
        request the browser ever makes.

        The cache meters stay top-level unless an iteration carries its own:
        summing zeroes over iterations that do not report them would throw away
        numbers the top level had.
        """
        def meter(source: Any, name: str) -> int:
            return getattr(source, name, 0) or 0

        top = {
            "input_tokens": meter(usage, "input_tokens"),
            "output_tokens": meter(usage, "output_tokens"),
            "cache_read_tokens": meter(usage, "cache_read_input_tokens"),
            "cache_write_tokens": meter(usage, "cache_creation_input_tokens"),
        }
        iterations = getattr(usage, "iterations", None) or []
        if not iterations:
            return top
        totals = {
            "input_tokens": sum(meter(i, "input_tokens") for i in iterations),
            "output_tokens": sum(meter(i, "output_tokens") for i in iterations),
            "cache_read_tokens": sum(
                meter(i, "cache_read_input_tokens") for i in iterations),
            "cache_write_tokens": sum(
                meter(i, "cache_creation_input_tokens") for i in iterations),
        }
        for name in ("cache_read_tokens", "cache_write_tokens"):
            if not totals[name]:
                totals[name] = top[name]
        return totals

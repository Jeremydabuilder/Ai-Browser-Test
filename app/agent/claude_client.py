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

from app.agent.config import AgentConfig


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
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ClaudeError(RuntimeError):
    """A failure talking to Claude, already phrased for a person.

    ``retryable`` distinguishes "the network hiccuped, try again" from "your API
    key is wrong", which is the only distinction the UI needs to make.
    """

    def __init__(self, message: str, *, retryable: bool = False, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.detail = detail


class ClaudeTransport(Protocol):
    """What the session needs from a model. Implemented for real below."""

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentResponse: ...


class ClaudeClient:
    """The real transport, backed by the official Anthropic Python SDK."""

    def __init__(self, api_key: str, config: AgentConfig | None = None) -> None:
        if not api_key:
            raise ClaudeError("No Anthropic API key is configured.")
        self.config = config or AgentConfig()
        # Imported here so the browser starts fine without the SDK installed;
        # the agent panel reports it as unconfigured instead of the app dying.
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=self.config.request_timeout_s,
            max_retries=self.config.max_retries,
        )

    def send(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentResponse:
        """One blocking round-trip. Raises ClaudeError on failure."""
        anthropic = self._anthropic
        try:
            response = self._client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                # Claude Opus 5 thinks by default and rejects budget_tokens;
                # adaptive is the whole configuration.
                thinking={"type": "adaptive"},
            )
        except anthropic.AuthenticationError as exc:
            raise ClaudeError(
                "Claude rejected the API key. Check the key in Settings.",
                detail=str(exc),
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ClaudeError(
                "This API key does not have permission to use the Claude API.",
                detail=str(exc),
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ClaudeError(
                f"The model '{self.config.model}' is not available to this account.",
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
        usage = getattr(response, "usage", None)
        return AgentResponse(
            text="\n".join(t for t in texts if t).strip(),
            tool_calls=calls,
            stop_reason=getattr(response, "stop_reason", "") or "",
            raw_content=response.content,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

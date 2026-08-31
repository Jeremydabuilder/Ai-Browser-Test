"""Tunable settings for the agent.

Everything a user might reasonably want to change - the model, how much of a
page is sent, how long a task may run - lives here rather than being scattered
as literals through the loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Claude Opus 5. Thinking is on by default on this model and `budget_tokens`
# is rejected, so the loop sends `thinking={"type": "adaptive"}` and nothing else.
DEFAULT_MODEL = "claude-opus-5"


@dataclass
class ContextLimits:
    """Caps on what reaches the model.

    A modern page can serialise to hundreds of kilobytes. Sending that on every
    turn is slow, expensive, and actively unhelpful - the model does better with
    a focused view. These are the knobs; the defaults are deliberately modest.

    When something is trimmed the agent is *told* it was trimmed, and how to get
    the rest (scroll, filter, ask for more). Silent truncation would leave it
    reasoning about a page it cannot see the end of.
    """

    #: Interactive elements per page snapshot.
    max_elements: int = 120
    #: Characters of readable page text per snapshot.
    max_page_text: int = 6000
    #: Characters of any single tool result handed back to the model.
    max_tool_result_chars: int = 24000
    #: Conversation turns (user+assistant pairs) kept before older ones are dropped.
    max_history_messages: int = 60
    #: Tool calls allowed in one task, as a runaway guard.
    max_tool_calls: int = 40
    #: Model round-trips allowed in one task.
    max_turns: int = 25


@dataclass
class AgentConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = 8000
    #: Wall-clock cap on a single Claude request.
    request_timeout_s: float = 120.0
    #: SDK-level retries for 429/5xx/connection errors.
    max_retries: int = 2
    limits: ContextLimits = field(default_factory=ContextLimits)

    @classmethod
    def from_environment(cls) -> "AgentConfig":
        """Allow overrides for experimentation without editing code."""
        config = cls()
        model = os.environ.get("PYBROWSER_AGENT_MODEL")
        if model:
            config.model = model
        return config

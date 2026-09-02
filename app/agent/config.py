"""Tunable settings for the agent - including the ones that decide the bill.

Everything a user might reasonably want to change - the model, how hard it
thinks, how much of a page is sent, how long a task may run - lives here rather
than being scattered as literals through the loop.

Why there is a whole section about cost
---------------------------------------
An agent loop is the most expensive shape of API use there is: every turn
resends the entire conversation so far, and a browser agent's conversation is
full of bulky page snapshots. A ten-step task can re-send the same system
prompt and tool schemas ten times.

Three levers are applied, in the order that spends the least quality per pound
saved:

1. **Prompt caching** (`CacheSettings`) - free. Identical prefixes are billed
   at about a tenth of the input price on re-read. Nothing about the answer
   changes; only the invoice does.
2. **Effort** (`AgentConfig.effort`) - nearly free. Caps how much the model
   thinks before answering. `medium` is the default here.
3. **Model** (`AgentConfig.model`) - a real trade. A cheaper model is cheaper
   because it is less capable, so this comes last and the catalogue below says
   plainly what each one gives up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    """One selectable model, described honestly.

    ``note`` is shown to the user in the settings dialog. It says what the
    model costs *and* what it gives up, because a list that only mentions price
    is an invitation to pick the cheapest and then wonder why the agent stopped
    working.
    """

    model_id: str
    label: str
    note: str
    #: False when the model has no `effort` control, so the loop omits it.
    supports_effort: bool = True
    #: False on models predating adaptive thinking. They reject
    #: ``thinking={"type": "adaptive"}`` with a 400, so the loop omits the
    #: parameter entirely rather than sending a configuration they refuse.
    supports_adaptive_thinking: bool = True
    #: True on models that check the conversation was not edited between
    #: requests ("preserved thinking"): a thinking block's signature records
    #: the prefix that produced it, and rewriting or dropping an earlier turn
    #: invalidates every block after it.
    #:
    #: ``AgentSession`` does both - see ``EDITS_HISTORY_CLIENT_SIDE`` there -
    #: so no model with this set can be offered until that changes. A test
    #: enforces the pairing, because the failure it prevents is an
    #: intermittent 400 partway through a long task, which is a miserable
    #: thing to debug from a bug report.
    checks_history_edits: bool = False


#: The models this browser offers. Ordered most-capable first, because that is
#: the order in which they are worth trying - not the order of their prices.
MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(
        "claude-opus-5", "Claude Opus 5 (default)",
        "The recommended starting point for agent work. Strongest at multi-step "
        "browsing, where a wrong step costs more than the step saved.",
    ),
    ModelChoice(
        "claude-sonnet-5", "Claude Sonnet 5",
        "Near-Opus quality on agentic work, at a lower price. The best first "
        "thing to try if Opus 5 is costing more than you want to spend.",
    ),
    ModelChoice(
        # Predates both adaptive thinking and the effort control. Sending
        # either is a 400, which is what made every AI feature fail for anyone
        # who picked the cheapest model in the list.
        "claude-haiku-4-5", "Claude Haiku 4.5 (cheapest)",
        "Roughly a tenth of Opus 5's cost per question, and markedly less "
        "accurate: 63% against 92% on Anthropic's knowledge benchmark. Suits "
        "short, checkable tasks - not long browsing sessions where an early "
        "mistake compounds. 200K context, smaller than the others' 1M.",
        supports_effort=False,
        supports_adaptive_thinking=False,
    ),
    ModelChoice(
        "claude-fable-5", "Claude Fable 5 (most capable, most expensive)",
        "The highest-capability tier, at twice Opus 5's price ($10/$50 per "
        "million tokens against $5/$25). Only worth it for tasks Opus 5 "
        "actually fails.",
    ),
)

DEFAULT_MODEL = MODELS[0].model_id

_BY_ID = {choice.model_id: choice for choice in MODELS}


def describe_model(model_id: str) -> ModelChoice:
    """The catalogue entry for an id, or a neutral one for an unknown model.

    Unknown ids are allowed on purpose: `PYBROWSER_AGENT_MODEL` should let
    someone try a model released after this code was written, and a hardcoded
    allow-list would forbid exactly that. What we lose is only the description.
    """
    known = _BY_ID.get(model_id)
    if known is not None:
        return known
    return ModelChoice(model_id, model_id, "Not in this browser's catalogue; "
                                           "settings are used as given.")


# ---------------------------------------------------------------------------
# Effort
# ---------------------------------------------------------------------------

#: `output_config.effort` values, cheapest first. "default" means: send nothing
#: and let the model use its own, which is what every earlier version did.
EFFORT_LEVELS: tuple[tuple[str, str], ...] = (
    ("low", "Low - cheapest and fastest. Gives up a little accuracy; fine for "
            "simple 'read this page' style requests."),
    ("medium", "Medium (default) - in Anthropic's measurements this matched the "
               "model's own default accuracy at 70-85% of its cost."),
    ("high", "High - more deliberation per step."),
    ("max", "Maximum - for tasks where being right matters more than the price."),
    ("default", "Model default - send no effort setting at all."),
)

DEFAULT_EFFORT = "medium"

_EFFORT_IDS = {level for level, _ in EFFORT_LEVELS}


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextManagement:
    """Letting the server keep the conversation small, instead of doing it here.

    A browsing task accumulates one page snapshot per step and resends every one
    of them on every turn. The old snapshots are dead weight - element
    references are scoped to the snapshot that produced them, so once a newer
    capture exists the older ones cannot be used for anything.

    This browser used to trim them itself: rewriting superseded tool results in
    place and dropping the oldest exchanges. Both work, and both *edit the
    transcript*, which the newest models reject - a thinking block's signature
    records the conversation prefix that produced it, so changing an earlier
    turn invalidates every block after it. There is no client-side shape that
    keeps the saving and the guarantee.

    The server-side equivalents do not count as edits, because the check
    compares the conversation as it was *sent*, not the copy the server works
    from. So the same two jobs move across the wire:

    * **clearing** removes old tool results outright - the direct replacement
      for the old snapshot pruning.
    * **compaction** summarises the conversation when it gets long - the
      replacement for dropping the oldest exchanges, and much better at it,
      because a summary keeps what a truncation throws away.

    Both are beta, and both degrade to the old client-side behaviour if a
    platform rejects them - see ClaudeClient._rejected_parameter.
    """

    #: Clear superseded tool results. The snapshots are the whole point.
    clear_tool_results: bool = True
    #: Input tokens at which clearing starts. Lower than the API's 100k default
    #: because a page snapshot is large and the browser accumulates them fast.
    clear_after_tokens: int = 60_000
    #: How many recent tool exchanges survive untouched. Three covers "look,
    #: act, verify", which is the shape the system prompt asks for.
    keep_recent_tool_uses: int = 3
    #: Do not clear the tool *inputs*, only the results. The inputs are small
    #: and they are what makes the transcript readable when something is wrong.
    clear_tool_inputs: bool = False

    #: Summarise the conversation when it gets long.
    compact: bool = True
    #: Input tokens at which compaction runs. The API's own default; its floor
    #: is 50k and it rejects anything lower.
    compact_after_tokens: int = 150_000

    @property
    def enabled(self) -> bool:
        return self.clear_tool_results or self.compact


@dataclass(frozen=True)
class CacheSettings:
    """Prompt caching, which is the single biggest lever and costs nothing.

    Two breakpoints, because the prompt has two parts that change at very
    different rates:

    * **The static prefix** - the tool schemas and system prompt. Byte-identical
      on every request the browser ever makes, so it gets one explicit
      breakpoint. Tools render before ``system``, so a marker on the last system
      block caches both together.
    * **The conversation tail** - grows by one turn each time. Top-level
      automatic caching places a breakpoint on the last block and moves it
      forward as the conversation grows, which is exactly the multi-turn
      pattern, with no marker bookkeeping on our side.

    The prefix uses the one-hour TTL rather than the default five minutes,
    because this agent stops and waits for a human whenever it wants to do
    something sensitive. A five-minute entry expires during that pause; the
    one-hour entry costs 2x to write instead of 1.25x and pays that back the
    first time a confirmation takes longer than five minutes.
    """

    #: Explicit breakpoint on the tools + system prefix.
    prefix: bool = True
    #: "1h" or "5m". See the docstring for why "1h" is the default here.
    prefix_ttl: str = "1h"
    #: Top-level automatic caching for the growing message list.
    conversation: bool = True

    @property
    def enabled(self) -> bool:
        return self.prefix or self.conversation


# ---------------------------------------------------------------------------
# Context limits
# ---------------------------------------------------------------------------


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
    #: Characters of superseded page snapshots tolerated in the history before
    #: they are collapsed to one-line summaries. See AgentSession._prune().
    #: Deliberately large: every prune rewrites the cached conversation, so a
    #: rare big prune is much cheaper than a small one every turn.
    prune_stale_after_chars: int = 40000


# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    model: str = DEFAULT_MODEL
    #: A backstop, not a tuning knob: the model never sees it, and hitting it
    #: truncates an answer mid-sentence. Generous enough never to bind on the
    #: short answers a browser agent gives.
    max_tokens: int = 8000
    #: One of EFFORT_LEVELS. Pinned for the life of a session - changing it
    #: mid-conversation invalidates the message cache.
    effort: str = DEFAULT_EFFORT
    cache: CacheSettings = field(default_factory=CacheSettings)
    context: ContextManagement = field(default_factory=ContextManagement)
    #: Wall-clock cap on a single Claude request.
    request_timeout_s: float = 120.0
    #: SDK-level retries for 429/5xx/connection errors.
    max_retries: int = 2
    limits: ContextLimits = field(default_factory=ContextLimits)

    @property
    def effort_level(self) -> str | None:
        """The value to send, or None to send nothing."""
        if self.effort in ("", "default", None) or self.effort not in _EFFORT_IDS:
            return None
        return self.effort

    @property
    def model_choice(self) -> ModelChoice:
        return describe_model(self.model)

    def with_model(self, model: str) -> "AgentConfig":
        return replace(self, model=model or DEFAULT_MODEL)

    @classmethod
    def from_environment(cls, settings=None) -> "AgentConfig":
        """Build a config from stored preferences, then environment overrides.

        The environment wins, so a shell variable can override the dialog for a
        single run without changing what the dialog remembers.

        ``settings`` is a SettingsStore or None. It is read defensively: the
        agent must still start if the settings table is unreadable.
        """
        config = cls()
        if settings is not None:
            try:
                config.model = settings.get(KEY_AGENT_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL
                stored_effort = settings.get(KEY_AGENT_EFFORT, DEFAULT_EFFORT)
                if stored_effort in _EFFORT_IDS:
                    config.effort = stored_effort
            except Exception:  # noqa: BLE001 - preferences are never load-bearing
                pass

        model = os.environ.get(ENV_MODEL)
        if model:
            config.model = model.strip()
        effort = (os.environ.get(ENV_EFFORT) or "").strip().lower()
        if effort in _EFFORT_IDS:
            config.effort = effort
        cache = (os.environ.get(ENV_CACHE) or "").strip().lower()
        if cache in ("0", "off", "false", "no"):
            # An escape hatch for debugging, not a recommendation: turning
            # caching off makes every request cost several times as much.
            config.cache = CacheSettings(prefix=False, conversation=False)
        return config


ENV_MODEL = "PYBROWSER_AGENT_MODEL"
ENV_EFFORT = "PYBROWSER_AGENT_EFFORT"
ENV_CACHE = "PYBROWSER_AGENT_CACHE"

#: Settings-table keys. Model and effort are preferences, not secrets, so they
#: belong in the ordinary settings table - unlike the API key, which never
#: touches SQLite.
KEY_AGENT_MODEL = "agent_model"
KEY_AGENT_EFFORT = "agent_effort"

"""The agent loop: user message -> Claude -> tools -> Claude -> answer.

Threading, which is the part that has to be right
-------------------------------------------------
Two constraints pull in opposite directions:

* Claude API calls are blocking network I/O and must never run on the GUI
  thread, or the whole browser freezes while the model thinks.
* Qt WebEngine - and therefore every BrowserController call - must only be
  touched from the GUI thread.

So the work is split, and the split is enforced by where the objects live:

    GUI thread                      worker thread (QThread)
    ----------                      -----------------------
    AgentSession                    _ClaudeWorker
    ToolRegistry                      └── ClaudeClient (blocking HTTP)
    BrowserController
    AgentPanel

``AgentSession`` lives on the GUI thread and owns all state. ``_ClaudeWorker``
is moved to a QThread. They talk only through signals, so Qt queues every
hand-off across the thread boundary automatically - no locks, no shared mutable
state, no direct calls in either direction.

The loop is written by hand rather than using the SDK's tool runner, for three
reasons the runner cannot accommodate: tools must execute on a *different*
thread from the request; the loop must be able to suspend mid-turn waiting for
a human to approve an action; and cancellation must be checkable between every
step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.agent.claude_client import AgentResponse, ClaudeError, ClaudeTransport, ToolCall
from app.agent.config import AgentConfig
from app.agent.prompt import SYSTEM_PROMPT
from app.agent import trace as tracing
from app.agent.tools import TOOL_SCHEMAS, ToolError, ToolRegistry
from app.agent.usage import Usage
from app.browser.controller import BrowserController


class AgentState:
    IDLE = "idle"
    THINKING = "thinking"          # waiting on Claude
    ACTING = "acting"              # running a browser tool
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CANCELLING = "cancelling"


class StepState:
    """Where one step of the task has got to."""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    WAITING = "waiting"          # paused for the user to approve
    SKIPPED = "skipped"          # the user declined, or the task was stopped


@dataclass(frozen=True)
class Step:
    """One thing the agent did, as the user sees it.

    Carries no page content and no typed text - just what was attempted and
    how it went. The panel renders these; nothing here knows about Qt.

    Frozen, and a state change emits a *new* Step rather than mutating this
    one. A mutable step would look fine in the panel, which re-renders anyway,
    and quietly betray anyone who kept a reference: every step they had stored
    would show the latest state.
    """

    index: int
    description: str
    state: str = StepState.RUNNING
    detail: str = ""


@dataclass
class ConfirmationRequest:
    """A sensitive action, paused until the user decides.

    Carries everything the user needs to answer without going and looking:
    what would happen, which site it would happen on, and - for a form - what
    would be sent. Field *names* only, never their values: a confirmation
    prompt is not a place to display a password back to someone.
    """

    tool_call_id: str
    tool_name: str
    description: str            # "click 'Buy now'"
    reasons: list[str] = field(default_factory=list)
    url: str = ""
    #: Names of the fields a form submission would send, when known.
    submits: list[str] = field(default_factory=list)
    #: True when one of those fields is a password or payment field.
    sensitive_fields: bool = False

    @property
    def site(self) -> str:
        """The host, which is the part of a URL that decides whether to trust it."""
        from urllib.parse import urlsplit

        try:
            host = urlsplit(self.url).netloc
        except ValueError:
            return ""
        return host[4:] if host.startswith("www.") else host

    @property
    def prompt(self) -> str:
        where = f" on {self.site}" if self.site else ""
        why = f" This {', '.join(self.reasons)}." if self.reasons else ""
        return f"Py AI wants to {self.description}{where}.{why}"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _accepts_streaming(transport) -> bool:
    """Does this transport take an `on_text` callback?

    Asked of the signature rather than by calling and catching TypeError: a
    TypeError raised *inside* a real streaming call would look identical, and
    we would silently fall back to non-streaming and hide the bug. The
    scripted transport in the tests has no on_text, and that is the only case
    this needs to recognise.
    """
    import inspect

    try:
        return "on_text" in inspect.signature(transport.send).parameters
    except (AttributeError, TypeError, ValueError):
        # No send at all, or a builtin/C callable with no readable signature.
        return False


class _ClaudeWorker(QObject):
    """Runs blocking Claude requests. Lives on a QThread; never touches Qt UI."""

    responded = Signal(object)   # AgentResponse
    failed = Signal(object)      # ClaudeError
    text_delta = Signal(str)     # a fragment of the answer, as it arrives

    def __init__(self, transport: ClaudeTransport) -> None:
        super().__init__()
        self._transport = transport
        self._streaming = _accepts_streaming(transport)

    @Slot(str, list, list)
    def request(self, system: str, messages: list, tools: list) -> None:
        try:
            self.responded.emit(self._send(system, messages, tools))
        except ClaudeError as exc:
            self.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - a worker crash must not kill the app
            self.failed.emit(ClaudeError(
                "Something went wrong talking to Claude.", detail=f"{type(exc).__name__}: {exc}"))

    def _send(self, system: str, messages: list, tools: list) -> AgentResponse:
        """Stream if the transport can, otherwise make one blocking call.

        Emitting from here is safe: Qt queues a signal across the thread
        boundary, so fragments arrive on the GUI thread, in order.
        """
        if self._streaming:
            return self._transport.send(
                system=system, messages=messages, tools=tools,
                on_text=self.text_delta.emit)
        return self._transport.send(system=system, messages=messages, tools=tools)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class AgentSession(QObject):
    """One conversation with the agent, bound to one browser window."""

    # -- signals the UI listens to --------------------------------------
    state_changed = Signal(str)                  # AgentState
    assistant_message = Signal(str)              # Claude's visible text
    activity = Signal(str)                       # one-line tool activity
    #: A step appeared or changed state - the panel's checklist.
    step_changed = Signal(object)                # Step
    error = Signal(str)                          # something went wrong
    finished = Signal()                          # task over (done or stopped)
    confirmation_required = Signal(object)       # ConfirmationRequest
    usage_updated = Signal(object)               # Usage for the current task
    #: A fragment of the answer as it is generated, before assistant_message.
    assistant_delta = Signal(str)
    #: The conversation was reset.
    cleared = Signal()
    #: Emitted with the outgoing request so a worker thread can pick it up.
    _dispatch = Signal(str, list, list)

    def __init__(
        self,
        browser: BrowserController,
        transport: ClaudeTransport,
        config: AgentConfig | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or AgentConfig()
        self._browser = browser
        self._tools = ToolRegistry(browser, self.config.limits)

        # -- agent state, deliberately separate from browser state -------
        self._messages: list[dict[str, Any]] = []
        self._state = AgentState.IDLE
        self._task: str = ""
        self._cancelled = False
        self._turns = 0
        self._tool_calls_made = 0
        self._pending: list[ToolCall] = []
        self._results: list[dict[str, Any]] = []
        self._assistant_content: Any = None
        self._confirmation: ConfirmationRequest | None = None
        self._confirming_call: ToolCall | None = None

        # -- what this is costing ----------------------------------------
        #: Tokens for the task in progress, reset on every `send()`.
        self.task_usage = Usage()
        #: Tokens since the browser started. Never reset.
        self.session_usage = Usage()
        #: tool_use_id -> tool name, so `_prune_snapshots` can tell which
        #: results are bulky page captures without re-parsing their JSON.
        self._result_tools: dict[str, str] = {}
        #: Results already collapsed, so a prune never runs twice on one block.
        self._pruned: set[str] = set()

        #: A structured record of the current task. See app/agent/trace.py for
        #: what is deliberately not in it.
        self.trace = tracing.Trace()
        self._steps: list[Step] = []

        # -- worker thread ------------------------------------------------
        self._thread = QThread()
        self._thread.setObjectName("claude-worker")
        self._worker = _ClaudeWorker(transport)
        self._worker.moveToThread(self._thread)
        self._dispatch.connect(self._worker.request)          # queued: GUI -> worker
        self._worker.responded.connect(self._on_response)     # queued: worker -> GUI
        self._worker.failed.connect(self._on_failure)
        self._worker.text_delta.connect(self._on_text_delta)
        self._thread.start()

    # -- public API -------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def busy(self) -> bool:
        return self._state != AgentState.IDLE

    @property
    def task(self) -> str:
        return self._task

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The conversation so far. Copied, so callers cannot corrupt it."""
        return list(self._messages)

    def send(self, message: str) -> bool:
        """Start a task. Returns False if the agent is already busy."""
        message = (message or "").strip()
        if not message or self.busy:
            return False
        self._task = message
        self._cancelled = False
        self._turns = 0
        self._tool_calls_made = 0
        self.task_usage.reset()
        self.usage_updated.emit(self.task_usage)
        self._steps = []
        self.trace.start()
        self.trace.record(tracing.TASK_STARTED, chars=len(message))
        self._messages.append({"role": "user", "content": message})
        self._trim_history()
        self._request()
        return True

    @Slot(str)
    def _on_text_delta(self, fragment: str) -> None:
        """Pass a streamed fragment on, unless the task was cancelled.

        Fragments already in Qt's queue when the user pressed Stop would
        otherwise keep arriving after the panel said the task had stopped.
        """
        if self._cancelled:
            return
        self.assistant_delta.emit(fragment)

    def clear(self) -> None:
        """Forget the conversation and start again.

        Refused while a task is running: dropping the history out from under an
        in-flight request would leave the next turn answering tool results for
        tool calls it can no longer see.
        """
        if self.busy:
            return
        self._messages.clear()
        self._result_tools.clear()
        self._pruned.clear()
        self.task_usage.reset()
        self._task = ""
        self.usage_updated.emit(self.task_usage)
        self.cleared.emit()

    def cancel(self) -> None:
        """Stop the current task.

        Sets the flag every step checks, and drops whatever the loop was about
        to do. A Claude request already in flight cannot be aborted mid-HTTP -
        the SDK call is blocking - so we let it finish on the worker thread and
        discard the result. The user sees the task stop immediately either way,
        and the browser stays usable throughout.
        """
        if not self.busy:
            return
        self._cancelled = True
        self.trace.record(tracing.TASK_CANCELLED, steps=len(self._steps))
        self._update_step(StepState.SKIPPED, "stopped")
        self._set_state(AgentState.CANCELLING)
        self._pending.clear()
        self._results.clear()
        self._confirmation = None
        self._confirming_call = None
        self.activity.emit("Stopped.")
        self._finish()

    def resolve_confirmation(self, allowed: bool) -> None:
        """Answer the outstanding confirmation. Called by the UI."""
        if self._state != AgentState.AWAITING_CONFIRMATION or self._confirming_call is None:
            return
        call, request = self._confirming_call, self._confirmation
        self._confirming_call = None
        self._confirmation = None
        if allowed:
            self.trace.record(tracing.APPROVAL_GRANTED, tool=call.name)
            self.activity.emit(f"Approved: {request.description}")
            self._set_state(AgentState.ACTING)
            self._execute(call, step=False)
            return
        self.trace.record(tracing.APPROVAL_DENIED, tool=call.name)
        self._update_step(StepState.SKIPPED, "you declined")
        self.activity.emit(f"Declined: {request.description}")
        self._record_result(call.id, (
            '{"ok": false, "error": {"code": "USER_DECLINED", '
            '"message": "The user declined this action.", "recoverable": false}, '
            '"hint": "Do not retry this action. Explain what you were about to do and stop, '
            'or ask the user how they would like to proceed."}'
        ), is_error=False)
        self._advance()

    def shutdown(self) -> None:
        """Stop the worker thread. Called when the window closes."""
        self._cancelled = True
        self._thread.quit()
        self._thread.wait(3000)

    # -- the loop ---------------------------------------------------------
    def _request(self) -> None:
        if self._cancelled:
            return
        if self._turns >= self.config.limits.max_turns:
            self.error.emit(
                f"Stopping: the task used its limit of {self.config.limits.max_turns} steps.")
            self._finish()
            return
        self._turns += 1
        self.trace.record(tracing.MODEL_REQUESTED, turn=self._turns,
                          messages=len(self._messages))
        self._set_state(AgentState.THINKING)
        self._dispatch.emit(SYSTEM_PROMPT, self._messages, TOOL_SCHEMAS)

    @Slot(object)
    def _on_response(self, response: AgentResponse) -> None:
        if self._cancelled:
            return
        self.task_usage.add(response)
        self.session_usage.add(response)
        self.usage_updated.emit(self.task_usage)
        self.trace.record(tracing.MODEL_RESPONDED,
                          tools=len(response.tool_calls),
                          stop_reason=response.stop_reason or None,
                          output_tokens=response.output_tokens or None)
        if response.text:
            self.assistant_message.emit(response.text)
        if not response.wants_tools:
            self._finish()
            return
        if self._tool_calls_made + len(response.tool_calls) > self.config.limits.max_tool_calls:
            self.error.emit(
                f"Stopping: the task reached its limit of "
                f"{self.config.limits.max_tool_calls} browser actions.")
            self._finish()
            return

        # The manual-loop contract: echo the assistant turn back verbatim,
        # then answer every tool_use with a tool_result in ONE user message.
        self._assistant_content = response.raw_content
        self._messages.append({"role": "assistant", "content": response.raw_content})
        self._pending = list(response.tool_calls)
        self._results = []
        self._set_state(AgentState.ACTING)
        self._next_tool()

    @Slot(object)
    def _on_failure(self, error: ClaudeError) -> None:
        if self._cancelled:
            return
        # The message, never the detail: an SDK exception can quote a request
        # header, and a header can carry a credential.
        self.trace.record(tracing.TASK_ERROR, kind="model",
                          retryable=error.retryable)
        self.error.emit(error.message)
        self._finish()

    def _next_tool(self) -> None:
        """Take the next pending tool call, or send the batch of results back."""
        if self._cancelled:
            return
        if not self._pending:
            self._messages.append({"role": "user", "content": self._results})
            self._results = []
            self._trim_history()
            self._prune_snapshots()
            self._request()
            return

        call = self._pending.pop(0)
        self._tool_calls_made += 1
        self.trace.record(tracing.TOOL_REQUESTED, tool=call.name,
                          **tracing.summarise_arguments(call.name, call.arguments))

        # 1. Does this tool exist? Rejected here rather than at execution time,
        #    so a hallucinated tool name never announces a step claiming the
        #    browser is doing something it is not.
        if not self._tools.knows(call.name):
            self.trace.record(tracing.TOOL_REJECTED, tool=call.name, reason="unknown_tool")
            self._record_result(call.id, self._tool_error(
                "UNKNOWN_TOOL", f"There is no tool called '{call.name}'."), is_error=True)
            self._advance()
            return

        # 2. Classify before anything else, so a malformed call becomes a normal
        #    tool error the model can correct rather than an exception.
        try:
            assessment = self._tools.assess(call.name, call.arguments)
        except ToolError as exc:
            self.trace.record(tracing.TOOL_REJECTED, tool=call.name, reason="invalid_arguments")
            self._record_result(call.id, self._tool_error("INVALID_ARGUMENTS", str(exc)),
                                is_error=True)
            self._advance()
            return
        except Exception as exc:  # noqa: BLE001
            self.trace.record(tracing.TOOL_REJECTED, tool=call.name, reason="unknown_tool")
            self._record_result(call.id, self._tool_error("TOOL_FAILED", str(exc)), is_error=True)
            self._advance()
            return

        # 3. The browser's safety layer decides - not the model.
        if assessment.get("requires_confirmation"):
            self._confirming_call = call
            submits, sensitive = self._describe_submission(call)
            self._confirmation = ConfirmationRequest(
                tool_call_id=call.id,
                tool_name=call.name,
                description=self._tools.describe_call_as_request(call.name, call.arguments),
                reasons=list(assessment.get("reasons", [])),
                url=self._browser.get_current_page().page.url,
                submits=submits,
                sensitive_fields=sensitive,
            )
            self._set_state(AgentState.AWAITING_CONFIRMATION)
            self.trace.record(tracing.APPROVAL_REQUESTED, tool=call.name,
                              reasons=len(self._confirmation.reasons))
            self._begin_step(self._tools.describe_call(call.name, call.arguments))
            self._update_step(StepState.WAITING, "waiting for your approval")
            self.confirmation_required.emit(self._confirmation)
            return

        self._execute(call)

    def _execute(self, call: ToolCall, *, step: bool = True) -> None:
        """Run one tool on the GUI thread and wait for its future.

        ``step`` is False when the step already exists - an approved action
        was announced before the user was asked, and must not appear twice.
        """
        if self._cancelled:
            return
        self._set_state(AgentState.ACTING)
        description = self._tools.describe_call(call.name, call.arguments)
        if step:
            self._begin_step(description)
        else:
            self._update_step(StepState.RUNNING)
        self.activity.emit(description)
        self.trace.record(tracing.TOOL_STARTED, tool=call.name)
        try:
            outcome = self._tools.run(call.name, call.arguments)
        except ToolError as exc:
            self.trace.record(tracing.TOOL_FAILED, tool=call.name, reason="invalid_arguments")
            self._update_step(StepState.FAILED, "invalid arguments")
            self._record_result(call.id, self._tool_error("INVALID_ARGUMENTS", str(exc)),
                                is_error=True)
            self._advance()
            return
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the session
            self.trace.record(tracing.TOOL_FAILED, tool=call.name, reason="tool_failed")
            self._update_step(StepState.FAILED, "the tool failed")
            self._record_result(call.id, self._tool_error("TOOL_FAILED", str(exc)), is_error=True)
            self._advance()
            return

        if outcome.immediate is not None:
            import json

            self.trace.record(tracing.TOOL_SUCCEEDED, tool=call.name)
            self._update_step(StepState.DONE)
            self._record_result(call.id, json.dumps(outcome.immediate, ensure_ascii=False),
                                tool_name=call.name)
            self._advance()
            return

        def on_done(result: Any) -> None:
            if self._cancelled:
                return
            if result is None:
                self.trace.record(tracing.TOOL_FAILED, tool=call.name, reason="timeout")
                self._update_step(StepState.FAILED, "timed out")
                self._record_result(call.id, self._tool_error(
                    "TIMEOUT", "The browser action did not complete in time."), is_error=True)
            else:
                payload = self._tools.encode(result)
                if result.ok:
                    self.trace.record(tracing.TOOL_SUCCEEDED, tool=call.name,
                                      result_chars=len(payload) if isinstance(payload, str) else None)
                    self._update_step(StepState.DONE)
                else:
                    code = result.error.code if result.error else "failed"
                    self.trace.record(tracing.TOOL_FAILED, tool=call.name, reason=code)
                    self._update_step(StepState.FAILED, code)
                self._record_result(call.id, self._tools.render(result, payload),
                                    is_error=not result.ok, tool_name=call.name)
            self._advance()

        outcome.future.then(on_done)

    def _advance(self) -> None:
        """Move to the next tool. Kept separate so every path funnels through it."""
        if self._cancelled:
            return
        self._next_tool()

    def _describe_submission(self, call: ToolCall) -> tuple[list[str], bool]:
        """Which fields a form submission would send. Names only, never values.

        Best-effort: if the page has moved on and the form cannot be found, the
        prompt simply says less rather than guessing.
        """
        if call.name != "browser_submit":
            return [], False
        ref = call.arguments.get("ref")
        if not isinstance(ref, str):
            return [], False
        try:
            description = self._browser.describe_action("submit", ref=ref)
            fields = description.get("fields") or []
            names, sensitive = [], False
            for item in fields:
                if not isinstance(item, dict):
                    continue
                label = (item.get("name") or item.get("field_name")
                         or item.get("placeholder") or item.get("input_type") or "")
                if label:
                    names.append(str(label)[:40])
                if item.get("secret") or item.get("input_type") == "password":
                    sensitive = True
            return names[:12], sensitive
        except Exception:  # noqa: BLE001 - a prompt must appear even if this fails
            return [], False

    # -- steps -------------------------------------------------------------
    def _begin_step(self, description: str) -> Step:
        step = Step(index=len(self._steps), description=description)
        self._steps.append(step)
        self.step_changed.emit(step)
        return step

    def _update_step(self, state: str, detail: str = "") -> None:
        if not self._steps:
            return
        from dataclasses import replace

        step = replace(self._steps[-1], state=state, detail=detail)
        self._steps[-1] = step
        self.step_changed.emit(step)

    @property
    def steps(self) -> list[Step]:
        return list(self._steps)

    # -- helpers ----------------------------------------------------------
    def _record_result(self, tool_use_id: str, content: str, *,
                       is_error: bool = False, tool_name: str = "") -> None:
        block: dict[str, Any] = {
            "type": "tool_result", "tool_use_id": tool_use_id, "content": content,
        }
        if is_error:
            block["is_error"] = True
        if tool_name:
            self._result_tools[tool_use_id] = tool_name
        self._results.append(block)

    @staticmethod
    def _tool_error(code: str, message: str) -> str:
        import json

        return json.dumps({
            "ok": False,
            "error": {"code": code, "message": message, "recoverable": code != "TOOL_FAILED"},
            "hint": "Fix the arguments and try again, or choose a different approach.",
        })

    #: Tools whose results are large and go out of date the moment the next
    #: one runs, because element references are scoped to their snapshot.
    _SNAPSHOT_TOOLS = frozenset({
        "browser_get_page", "browser_get_page_text", "browser_find_elements",
    })

    _PRUNED_NOTE = (
        '{"ok": true, "note": "This page snapshot was replaced by a newer one and '
        'has been collapsed to keep the conversation small. Its element references '
        'are no longer valid.", '
        '"hint": "Call browser_get_page again if you need to look at this page."}'
    )

    def _prune_snapshots(self) -> None:
        """Collapse superseded page snapshots once they add up to real weight.

        A browsing task accumulates one page capture per step, and every one of
        them is resent on every subsequent turn. The older ones are not merely
        bulky, they are *dead*: element references are scoped to the snapshot
        that produced them, so the moment a newer capture exists the older
        references cannot be used for anything. Replacing them with a sentence
        saying so is both cheaper and more accurate than leaving stale refs in
        front of the model.

        The threshold is high and the newest snapshot is always kept whole. A
        prune rewrites the conversation, which costs one cold cache miss on the
        next request - so this must happen rarely and in one large batch, never
        a little every turn. Below the threshold, doing nothing is cheaper than
        tidying.
        """
        limit = self.config.limits.prune_stale_after_chars
        if limit <= 0:
            return

        found: list[tuple[dict[str, Any], int]] = []
        for message in self._messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                ref = block.get("tool_use_id")
                if ref in self._pruned:
                    continue
                if self._result_tools.get(ref) not in self._SNAPSHOT_TOOLS:
                    continue
                body = block.get("content")
                if isinstance(body, str):
                    found.append((block, len(body)))

        superseded = found[:-1]          # never touch the most recent snapshot
        if sum(size for _, size in superseded) < limit:
            return
        for block, _ in superseded:
            block["content"] = self._PRUNED_NOTE
            block.pop("is_error", None)
            self._pruned.add(block.get("tool_use_id"))

    def _trim_history(self) -> None:
        """Keep the conversation bounded without destroying it.

        Drops the oldest exchanges, and never the first user message - losing
        that would leave the agent working on nothing.

        The subtlety is that a ``tool_result`` is only valid if the
        ``tool_use`` it answers is still present, so dropping an assistant turn
        means dropping the results that answer it. An earlier version skipped
        forward past *every* subsequent tool message, which collapsed a
        seven-message history to one and threw away all the context the agent
        needed to finish a multi-step task. Now only the orphaned results are
        removed, one exchange at a time.
        """
        limit = max(3, self.config.limits.max_history_messages)
        if len(self._messages) <= limit:
            return
        first, rest = self._messages[:1], self._messages[1:]
        while len(first) + len(rest) > limit and rest:
            rest.pop(0)
            # Whatever that message was, any tool_result now at the front has
            # lost its tool_use and must go with it - but stop there.
            while rest and self._holds_tool_result(rest[0]):
                rest.pop(0)
        self._messages = first + rest

    @staticmethod
    def _holds_tool_result(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
                if kind == "tool_result":
                    return True
        return False

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def _finish(self) -> None:
        if not self._cancelled:
            self.trace.record(tracing.TASK_FINISHED, steps=len(self._steps),
                              turns=self._turns, tools=self._tool_calls_made)
        self._pending.clear()
        self._results.clear()
        self._assistant_content = None
        self._set_state(AgentState.IDLE)
        self.finished.emit()

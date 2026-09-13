"""Turning "Record Workflow" into a RecordedWorkflow.

Recording is layered on the exact same hook Routines already use
(AgentSession.step_recorder / _record_step, see app/routines/service.py's
RoutineService for the precedent) rather than a brand-new JS DOM-capture
mechanism. That hook already fires only after BrowserController itself
resolved a semantic target and the action actually succeeded - so recording
gets "semantic actions, not raw coordinates" and "only real observed
actions, never text the page merely contains" for free, instead of a
second, independent path that would have to re-earn both guarantees.

A webpage's own text can never reach this recorder: only a tool the agent
itself decided to call, whose arguments it itself constructed, ever becomes
a step. This is also why prompt injection cannot create a recorded action -
injected text can influence what the model *decides* to do, exactly as it
always could, but it cannot fabricate a call that never happened, and
whatever call *did* happen still runs through this same tool pipeline
(so a malicious page still cannot, say, forge an mcp.* call with a
schema_fingerprint it does not actually have).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.automation.model import (
    MAX_STEPS,
    McpStepInfo,
    RecordedStep,
    RecordedWorkflow,
    SemanticTarget,
    credential_placeholder,
    new_workflow,
    parameter_candidates,
)
from app.browser import safety

#: A step whose tool is one of these is recordable at all. Mission
#: bookkeeping tools other than "save a finding" are not steps in a web
#: workflow (matches RoutineService.RECORDABLE_PREFIX's existing policy);
#: mission_save_finding is the one explicit "extract/save a value" action
#: the Phase 16 brief calls out by name.
_RECORDABLE_PREFIXES = ("browser_", "mcp.")
_RECORDABLE_EXACT = {"mission_save_finding"}

#: Visual tools have no ``ref`` at all - a screen point, not a page element -
#: so they can never carry a SemanticTarget. Recorded anyway (per the
#: brief's own "mark requires_visual_fallback=true" instruction) but the
#: runner treats them as always needing the user's confirmation at replay,
#: never as silently re-clickable coordinates. See runner.py.
_VISUAL_TOOLS = {"browser_visual_click", "browser_visual_focus", "browser_visual_type"}

#: Read-only lookups the agent runs constantly while resolving *other*
#: steps (browser_find_elements above all). Recording these too would turn
#: every workflow into mostly noise from the agent's own bookkeeping, not
#: the task it was demonstrating - unlike RoutineService, which teaches a
#: short, deliberate sequence and can afford to keep everything.
_NEVER_RECORD = {"browser_find_elements", "browser_wait_for_element", "browser_list_tabs"}


@dataclass
class _Draft:
    steps: list[RecordedStep] = field(default_factory=list)
    next_id: int = 0


class WorkflowRecorder:
    """One recording session's worth of state. Owned by whatever UI exposes
    "Record Workflow" (see app/ui - MainWindow wires start/pause/resume/
    finish/cancel to a toolbar action), not by AgentSession itself: a
    recorder outlives any one AgentSession turn, the same way RoutineService
    does.
    """

    def __init__(self, tools: Any = None) -> None:
        """``tools`` is a ToolRegistry (app.agent.tools) - used only to look
        up an element's semantic descriptor from a ``ref`` (element_for_ref)
        and an MCP tool's current schema_fingerprint
        (mcp_current_fingerprint). Optional so this module stays testable
        with a plain fake exposing just those two methods."""
        self._tools = tools
        self._draft: _Draft | None = None

    def set_tools(self, tools: Any) -> None:
        """Attach (or replace) the ToolRegistry used to resolve semantic
        targets - see __init__. MainWindow calls this once its one
        AgentSession/ToolRegistry actually exists, since the recorder itself
        is created before that (it outlives any one session, same as
        RoutineService)."""
        self._tools = tools

    @property
    def is_recording(self) -> bool:
        return self._draft is not None

    @property
    def step_count(self) -> int:
        return len(self._draft.steps) if self._draft else 0

    def start(self) -> bool:
        if self._draft is not None:
            return False
        self._draft = _Draft()
        return True

    def cancel(self) -> None:
        self._draft = None

    def record_step(self, tool_name: str, args: dict[str, Any], description: str = "") -> None:
        """Called after a tool call the agent made succeeds - see
        AgentSession._record_step. Silently ignored unless a recording is
        active or the tool is not one this phase considers a workflow step
        at all (RoutineService's exact same shape of guard)."""
        if self._draft is None:
            return
        if len(self._draft.steps) >= MAX_STEPS:
            return
        if not (tool_name.startswith(_RECORDABLE_PREFIXES) or tool_name in _RECORDABLE_EXACT):
            return
        if tool_name in _NEVER_RECORD:
            return
        step = self._build_step(tool_name, args, description)
        if step is not None:
            self._draft.steps.append(step)

    def finish(self, *, drop_ids: set[int] | None = None) -> RecordedWorkflow | None:
        """Stop recording and return what was captured, or None if nothing
        was. ``drop_ids`` lets the Finish-flow editor discard accidental
        steps before the workflow is ever saved - see model.RecordedStep.id."""
        draft = self._draft
        self._draft = None
        if draft is None or not draft.steps:
            return None
        drop_ids = drop_ids or set()
        steps = [s for s in draft.steps if s.id not in drop_ids]
        if not steps:
            return None
        return new_workflow(steps, parameter_candidates(steps))

    # -- internals -----------------------------------------------------
    def _build_step(self, tool_name: str, args: dict[str, Any], description: str
                    ) -> RecordedStep | None:
        step_id = self._draft.next_id
        self._draft.next_id += 1

        # A ``ref`` is a snapshot-scoped id - it means nothing outside the
        # page it was minted for, and MUST NOT be persisted (see model.py's
        # docstring): only the semantic descriptor it currently resolves to
        # is worth keeping.
        ref = args.get("ref")
        ref = ref if isinstance(ref, str) else None
        tab_id = args.get("tab_id") if isinstance(args.get("tab_id"), int) else None
        persisted_args = {key: value for key, value in args.items() if key != "ref"}

        target: SemanticTarget | None = None
        element: dict[str, Any] | None = None
        requires_visual_fallback = tool_name in _VISUAL_TOOLS

        if ref and self._tools is not None:
            element = self._tools.element_for_ref(ref, tab_id)
            if element:
                target = SemanticTarget(
                    role=str(element.get("role") or ""),
                    name=str(element.get("name") or ""),
                    field_name=str(element.get("field_name") or ""),
                    placeholder=str(element.get("placeholder") or ""),
                )
            else:
                # The tool succeeded but nothing structural explains what it
                # touched - be honest that replay will need help here too.
                requires_visual_fallback = True

        mcp_info: McpStepInfo | None = None
        if tool_name.startswith("mcp.") and self._tools is not None:
            server_id, _, inner_name = tool_name[len("mcp."):].partition(".")
            fingerprint = self._tools.mcp_current_fingerprint(tool_name)
            mcp_info = McpStepInfo(server_id=server_id, tool_name=inner_name,
                                   schema_fingerprint=fingerprint)

        if tool_name in ("browser_type", "browser_visual_type") and "text" in persisted_args:
            persisted_args = dict(persisted_args)
            persisted_args["text"] = self._secret_safe_value(
                element, str(persisted_args.get("text", "")))

        return RecordedStep(
            id=step_id, tool_name=tool_name, args=persisted_args, target=target,
            requires_visual_fallback=requires_visual_fallback,
            visual_hint=self._visual_hint(tool_name, args),
            mcp=mcp_info, label=description,
        )

    @staticmethod
    def _visual_hint(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if tool_name not in ("browser_visual_click", "browser_visual_focus"):
            return None
        x, y = args.get("x"), args.get("y")
        if isinstance(x, int) and isinstance(y, int):
            return {"x": x, "y": y}
        return None

    @staticmethod
    def _secret_safe_value(element: dict[str, Any] | None, value: str) -> str:
        """Never persist a secret into a workflow's stored JSON - see the
        module and model docstrings. Reuses the exact same classifier that
        already decides whether typing this would need the user's approval
        (app.browser.safety.classify_type), so "is this secret" is one
        judgement, not two that could quietly disagree."""
        assessment = safety.classify_type(element, value)
        if assessment.level != safety.Sensitivity.SENSITIVE:
            return value
        hint = "field"
        if element:
            hint = str(element.get("field_name") or element.get("name") or "field")
        return credential_placeholder(_slugify(hint))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "secret"

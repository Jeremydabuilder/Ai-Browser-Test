"""Replaying a RecordedWorkflow, safely.

The one rule everything here exists to uphold: **never act on a stale
target**. A recorded ``ref`` is never even stored (see model.py), so there
is nothing to blindly reuse in the first place - every step that touches
the page is re-resolved, right now, against the page as it currently is,
one step at a time (never the whole workflow up front, since resolving
step 5 before step 2 has even run would be resolving against a page state
that does not exist yet).

Approval is never bypassed either: each resolved step is handed to
AgentSession.run_routine one at a time, which is the exact same assess() /
confirmation / firewall pipeline a model-issued tool call goes through (see
app/agent/session.py's own docstring on run_routine). Recording a sensitive
action once buys it nothing here.

Every place this can legitimately stop is a *pause*, never a silent
continue and never a crash - see PauseReason. The UI layer (app/ui, not
built in this module) is what turns a pause into "PyBrowser needs your
help".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.automation.model import RecordedStep, RecordedWorkflow, render_args


class PauseReason:
    TARGET_MISSING = "target_missing"
    TARGET_AMBIGUOUS = "target_ambiguous"
    STEP_FAILED = "step_failed"
    VERIFICATION_FAILED = "verification_failed"
    MCP_UNAVAILABLE = "mcp_unavailable"
    MCP_SCHEMA_CHANGED = "mcp_schema_changed"
    VISUAL_FALLBACK = "visual_fallback"
    BUSY = "busy"
    DECLINED = "declined"


_HUMAN_REASON = {
    PauseReason.TARGET_MISSING: "PyBrowser needs your help: it could not find that element anymore.",
    PauseReason.TARGET_AMBIGUOUS: "PyBrowser needs your help: more than one matching element was found.",
    PauseReason.STEP_FAILED: "PyBrowser needs your help: a step in this workflow failed.",
    PauseReason.VERIFICATION_FAILED: "PyBrowser needs your help: the page did not look as expected.",
    PauseReason.MCP_UNAVAILABLE: "PyBrowser needs your help: the connected tool this step needs is unavailable.",
    PauseReason.MCP_SCHEMA_CHANGED: "PyBrowser needs your help: that tool's inputs changed since this was recorded.",
    PauseReason.VISUAL_FALLBACK: "PyBrowser needs your help: this step relies on visual matching and needs you to confirm it.",
    PauseReason.BUSY: "PyBrowser is busy with another task right now.",
    PauseReason.DECLINED: "This step was not approved, so the workflow stopped.",
}

#: Visual tools carry no ``ref`` and can never be re-resolved structurally -
#: see recorder.py. Replaying one unattended would mean either doing
#: nothing (safe but useless) or blindly reissuing an old (x, y) against
#: whatever is there now (exactly what the brief forbids), so this phase
#: always pauses for a human instead of guessing between those two.
_VISUAL_TOOLS = {"browser_visual_click", "browser_visual_focus", "browser_visual_type"}


@dataclass
class ReplayProgress:
    index: int
    total: int
    label: str = ""


class WorkflowRunner(QObject):
    """Replays one RecordedWorkflow through one AgentSession, a step at a
    time. One runner instance is meant to be reused across many runs (it
    connects to the session's routine_finished signal once)."""

    step_started = Signal(object)     # ReplayProgress
    step_completed = Signal(object)   # ReplayProgress
    paused = Signal(str, str)         # reason code, human message
    completed = Signal()
    failed = Signal(str)

    def __init__(self, session: Any, browser: Any, tools: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._browser = browser
        self._tools = tools
        self._workflow: RecordedWorkflow | None = None
        self._values: dict[str, str] = {}
        self._index = 0
        self._tab_id: int | None = None
        self._connected = False

    @property
    def is_running(self) -> bool:
        return self._workflow is not None

    def run(self, workflow: RecordedWorkflow, parameter_values: dict[str, str] | None = None, *,
           tab_id: int | None = None) -> bool:
        """Start replaying. Returns False (and never starts) if a replay is
        already in progress or the session is busy with something else."""
        if self._workflow is not None or getattr(self._session, "busy", False):
            return False
        if not self._connected:
            self._session.routine_finished.connect(self._on_step_finished)
            self._connected = True
        self._workflow = workflow
        self._values = dict(parameter_values or {})
        for parameter in workflow.parameters:
            self._values.setdefault(parameter.name, parameter.default)
        self._index = 0
        self._tab_id = tab_id
        self._advance()
        return True

    def cancel(self) -> None:
        self._workflow = None

    # -- internals -------------------------------------------------------
    def _pause(self, reason: str) -> None:
        self._workflow = None
        self.paused.emit(reason, _HUMAN_REASON.get(reason, reason))

    def _advance(self) -> None:
        workflow = self._workflow
        if workflow is None:
            return
        if self._index >= len(workflow.steps):
            self._workflow = None
            self.completed.emit()
            return
        step = workflow.steps[self._index]
        self.step_started.emit(ReplayProgress(self._index, len(workflow.steps), step.label))
        self._resolve_and_run(step)

    def _resolve_and_run(self, step: RecordedStep) -> None:
        if step.tool_name in _VISUAL_TOOLS:
            self._pause(PauseReason.VISUAL_FALLBACK)
            return

        if step.mcp is not None:
            namespaced = f"mcp.{step.mcp.server_id}.{step.mcp.tool_name}"
            current = self._tools.mcp_current_fingerprint(namespaced) if self._tools else ""
            if not current:
                self._pause(PauseReason.MCP_UNAVAILABLE)
                return
            if current != step.mcp.schema_fingerprint:
                self._pause(PauseReason.MCP_SCHEMA_CHANGED)
                return
            self._dispatch(namespaced, render_args(step.args, self._values))
            return

        if step.target is not None and not step.target.is_empty():
            future = self._browser.find_elements(
                step.target.queries(), role=(step.target.role or None), tab_id=self._tab_id)
            future.then(lambda result: self._on_target_resolved(step, result))
            return

        # No target to resolve at all (browser_navigate, mission_save_finding,
        # a step whose recorded element could not be described) - run as-is.
        self._dispatch(step.tool_name, render_args(step.args, self._values))

    def _on_target_resolved(self, step: RecordedStep, result: Any) -> None:
        if self._workflow is None:
            return
        if not getattr(result, "ok", False):
            self._pause(PauseReason.TARGET_MISSING)
            return
        matches = result.data.get("matches", [])
        if not matches:
            self._pause(PauseReason.TARGET_MISSING)
            return
        if len(matches) > 1:
            self._pause(PauseReason.TARGET_AMBIGUOUS)
            return
        args = render_args(step.args, self._values)
        args["ref"] = matches[0].get("ref")
        self._dispatch(step.tool_name, args)

    def _dispatch(self, tool_name: str, args: dict[str, Any]) -> None:
        if not self._session.run_routine([(tool_name, args)]):
            self._pause(PauseReason.BUSY)

    def _on_step_finished(self, results: list) -> None:
        workflow = self._workflow
        if workflow is None:
            return  # not this runner's replay, or already stopped
        step = workflow.steps[self._index]
        ok = bool(results) and not any(block.get("is_error") for block in results)
        if not ok:
            if step.optional:
                self._finish_step(step)
                return
            self._pause(PauseReason.STEP_FAILED if results else PauseReason.DECLINED)
            return
        if step.expect:
            self._verify(step)
            return
        self._finish_step(step)

    def _finish_step(self, step: RecordedStep) -> None:
        workflow = self._workflow
        if workflow is None:
            return
        self.step_completed.emit(ReplayProgress(self._index, len(workflow.steps), step.label))
        self._index += 1
        self._advance()

    # -- step verification -------------------------------------------------
    def _verify(self, step: RecordedStep) -> None:
        expect = step.expect or {}
        if "url_contains" in expect:
            needle = str(expect["url_contains"])
            tabs = self._browser.list_tabs()
            active = next((t for t in tabs if t.get("active")), None)
            url = (active or {}).get("url", "")
            self._on_verified(step, needle in url)
            return
        if "element_text_query" in expect or "element_gone_query" in expect:
            query = str(expect.get("element_text_query") or expect.get("element_gone_query"))
            expect_present = "element_text_query" in expect
            future = self._browser.find_elements([query], tab_id=self._tab_id)
            future.then(lambda result: self._on_verified(
                step, getattr(result, "ok", False)
                and bool(result.data.get("matches")) == expect_present))
            return
        if "heading_contains" in expect:
            needle = str(expect["heading_contains"])
            future = self._browser.get_page_text(tab_id=self._tab_id)
            future.then(lambda result: self._on_verified(
                step, getattr(result, "ok", False) and needle in result.data.get("text", "")))
            return
        self._on_verified(step, True)

    def _on_verified(self, step: RecordedStep, ok: bool) -> None:
        if self._workflow is None:
            return
        if not ok:
            self._pause(PauseReason.VERIFICATION_FAILED)
            return
        self._finish_step(step)

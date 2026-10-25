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

from PySide6.QtCore import QObject, QTimer, Signal

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
    TIMED_OUT = "timed_out"
    TAB_LOST = "tab_lost"
    TAB_AMBIGUOUS = "tab_ambiguous"


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
    PauseReason.TAB_LOST: "PyBrowser needs your help: the tab this workflow was working in is gone.",
    PauseReason.TAB_AMBIGUOUS: "PyBrowser needs your help: more than one new tab opened, so it cannot tell which one to continue in.",
    PauseReason.TIMED_OUT: "PyBrowser needs your help: the page did not answer in time, so the workflow stopped.",
}

#: How long finding an element or checking a step's expectation may take.
#: Deliberately not applied to the step itself: a step waiting for the
#: person's approval can take as long as the person takes.
RESOLVE_TIMEOUT_MS = 20_000

#: Tab-following. A replay works in ONE tab: the one that was active when it
#: started (or the one the caller named), never "whichever tab is active now" -
#: switching tabs while it runs must not redirect it. When a step that can open
#: a tab does open exactly one, the replay follows into it; more than one is
#: ambiguous and stops. A tab the user opens on their own after a step that
#: cannot open tabs is ignored.
_TAB_OPENING_TOOLS = {"browser_click", "browser_open_tab", "browser_type", "browser_press_key"}
#: Tools that manage tabs themselves, or have no tab argument at all.
_NO_TAB_ARG = {"browser_open_tab", "browser_select_tab", "browser_close_tab", "browser_list_tabs"}

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
    step_skipped = Signal(object)     # ReplayProgress - an *optional* step failed and was skipped
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
        #: Bumped at every start/stop/pause. A late answer from the page for
        #: a replay that has since stopped (or been replaced by a new one)
        #: carries the old number and is dropped - it must never advance or
        #: act on a different run.
        self._generation = 0
        self._dispatched = False
        self._known_tabs: set[int] = set()
        self._last_tool = ""
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)

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
        self._generation += 1
        self._workflow = workflow
        self._values = dict(parameter_values or {})
        for parameter in workflow.parameters:
            self._values.setdefault(parameter.name, parameter.default)
        self._index = 0
        self._tab_id = tab_id
        self._last_tool = ""
        tabs = self._tab_snapshot()
        self._known_tabs = {t["tab_id"] for t in tabs if isinstance(t.get("tab_id"), int)}
        if self._tab_id is None:
            active = next((t for t in tabs if t.get("active")), None)
            if active is not None and isinstance(active.get("tab_id"), int):
                self._tab_id = active["tab_id"]
        self._advance()
        return True

    def cancel(self) -> None:
        self._stop()

    # -- internals -------------------------------------------------------
    def _stop(self) -> None:
        self._dispatched = False
        self._generation += 1
        self._watchdog.stop()
        self._workflow = None

    def _arm(self) -> int:
        """Start the resolve/verify timeout; returns the token the answer
        must present to still count."""
        self._watchdog.start(RESOLVE_TIMEOUT_MS)
        return self._generation

    def _live(self, token: int) -> bool:
        return self._workflow is not None and token == self._generation

    def _on_timeout(self) -> None:
        if self._workflow is not None:
            self._pause(PauseReason.TIMED_OUT)

    def _pause(self, reason: str) -> None:
        self._stop()
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

    def _tab_snapshot(self) -> list[dict]:
        try:
            return list(self._browser.list_tabs())
        except Exception:  # noqa: BLE001 - no tab info means the old behaviour, not a crash
            return []

    def _follow_tabs(self) -> str | None:
        """Update the working tab before a step. Returns a PauseReason to stop
        with, or None to carry on."""
        if self._tab_id is None:
            return None
        tabs = self._tab_snapshot()
        ids = {t["tab_id"] for t in tabs if isinstance(t.get("tab_id"), int)}
        if not ids:
            return None
        new = ids - self._known_tabs
        self._known_tabs = ids
        if new and self._last_tool in _TAB_OPENING_TOOLS:
            if len(new) > 1:
                return PauseReason.TAB_AMBIGUOUS
            self._tab_id = next(iter(new))
            return None
        if self._tab_id not in ids:
            return PauseReason.TAB_LOST
        return None

    def _resolve_and_run(self, step: RecordedStep) -> None:
        lost = self._follow_tabs()
        if lost is not None:
            self._pause(lost)
            return
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
            token = self._arm()
            future = self._browser.find_elements(
                step.target.queries(), role=(step.target.role or None), tab_id=self._tab_id)
            future.then(lambda result: self._on_target_resolved(step, result, token))
            return

        # No target to resolve at all (browser_navigate, mission_save_finding,
        # a step whose recorded element could not be described) - run as-is.
        self._dispatch(step.tool_name, render_args(step.args, self._values))

    def _on_target_resolved(self, step: RecordedStep, result: Any, token: int) -> None:
        if not self._live(token):
            return
        self._watchdog.stop()
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
        if (self._tab_id is not None and tool_name.startswith("browser_")
                and tool_name not in _NO_TAB_ARG and "tab_id" not in args):
            args = {**args, "tab_id": self._tab_id}
        self._last_tool = tool_name
        self._dispatched = True
        if not self._session.run_routine([(tool_name, args)]):
            self._pause(PauseReason.BUSY)

    def _on_step_finished(self, results: list) -> None:
        workflow = self._workflow
        if workflow is None or not self._dispatched:
            return  # not this runner's replay, or no step of ours is in flight
        self._dispatched = False
        step = workflow.steps[self._index]
        if not results:
            # No result at all means the person declined the approval. That
            # is an answer, never something an "optional" flag may skip past.
            self._pause(PauseReason.DECLINED)
            return
        ok = not any(block.get("is_error") for block in results)
        if not ok:
            if step.optional:
                # Optional is the author's explicit choice, but it is still
                # reported, not silent.
                self.step_skipped.emit(ReplayProgress(self._index, len(workflow.steps), step.label))
                self._finish_step(step, completed=False)
                return
            self._pause(PauseReason.STEP_FAILED)
            return
        if step.expect:
            self._verify(step)
            return
        self._finish_step(step)

    def _finish_step(self, step: RecordedStep, *, completed: bool = True) -> None:
        workflow = self._workflow
        if workflow is None:
            return
        if completed:
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
            self._on_verified(step, needle in url, self._generation)
            return
        if "element_text_query" in expect or "element_gone_query" in expect:
            query = str(expect.get("element_text_query") or expect.get("element_gone_query"))
            expect_present = "element_text_query" in expect
            token = self._arm()
            future = self._browser.find_elements([query], tab_id=self._tab_id)
            future.then(lambda result: self._on_verified(
                step, getattr(result, "ok", False)
                and bool(result.data.get("matches")) == expect_present, token))
            return
        if "heading_contains" in expect:
            needle = str(expect["heading_contains"])
            token = self._arm()
            future = self._browser.get_page_text(tab_id=self._tab_id)
            future.then(lambda result: self._on_verified(
                step, getattr(result, "ok", False) and needle in result.data.get("text", ""), token))
            return
        self._on_verified(step, True, self._generation)

    def _on_verified(self, step: RecordedStep, ok: bool, token: int) -> None:
        if not self._live(token):
            return
        self._watchdog.stop()
        if not ok:
            self._pause(PauseReason.VERIFICATION_FAILED)
            return
        self._finish_step(step)

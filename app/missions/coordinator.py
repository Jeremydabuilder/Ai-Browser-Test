"""Multi-Agent Missions: one Mission, several specialised workers.

Not a second agent framework. Every worker is an ordinary AgentSession -
the exact same class, tool registry, approval flow, MCP permissions and
tool allowlist mechanism a Skill already uses (see AgentSession.
set_tool_allowlist and app/agent/skills.py) - just several instances,
each scoped to a role and a bounded task, coordinated here. A worker
"talks" to the rest of the Mission only through what it writes into the
shared MissionService (findings, sources, questions, the mission result)
and through its own WorkerTask record; there is no agent-to-agent chat
channel, and nothing here invents one.

Delegation is a choice, not a default: see should_delegate(). A goal that
does not need decomposition never touches this module - MainWindow keeps
sending it straight to the one interactive AgentSession, exactly as
before this phase.

Ordering, deliberately fixed rather than left to the Planner: research
tasks (Researcher/Browser Operator) run first, in dependency order and
partly in parallel; then Analyst; then Critic, who may ask for exactly one
bounded extra round of research; then Writer, last, once everything else
is settled. This keeps the "who runs when" question boringly predictable
even though the Planner is free to decide *how many* research tasks there
are and what each one is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.agent.tools import READ_ONLY_TOOLS, SEARCH_TOOLS


class WorkerRole:
    PLANNER = "planner"
    RESEARCHER = "researcher"
    BROWSER_OPERATOR = "browser_operator"
    ANALYST = "analyst"
    WRITER = "writer"
    CRITIC = "critic"

    ALL = (PLANNER, RESEARCHER, BROWSER_OPERATOR, ANALYST, WRITER, CRITIC)
    LABELS = {
        PLANNER: "Planner", RESEARCHER: "Researcher", BROWSER_OPERATOR: "Browser Operator",
        ANALYST: "Analyst", WRITER: "Writer", CRITIC: "Critic",
    }

    #: Roles that gather information and may run several at once - see
    #: MissionCoordinator._ready_batch. Everything else (Analyst, Critic,
    #: Writer) is a single fixed stage, never parallelised with itself.
    RESEARCH = (RESEARCHER, BROWSER_OPERATOR)


class WorkerState:
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    ALL = (QUEUED, RUNNING, WAITING_FOR_APPROVAL, DONE, FAILED, SKIPPED)
    TERMINAL = (DONE, FAILED, SKIPPED)


#: Which tools each role may use - the same allowlist mechanism a Skill
#: uses (AgentSession.set_tool_allowlist), never a separate permission
#: system. A worker inherits every other safety layer underneath this
#: (confirmation, MCP permissions, destructive-action policy) unchanged;
#: this only narrows which tools it is ever offered in the first place.
_BROWSER_READ_TOOLS = frozenset({
    "browser_get_page", "browser_get_page_text", "browser_get_pdf_text",
    "browser_find_elements", "browser_wait_for_element", "browser_scroll",
    "browser_scroll_to_element", "browser_list_tabs", "browser_navigate",
    "browser_open_tab", "browser_close_tab", "browser_back", "browser_forward",
    "browser_reload",
})
_BROWSER_WRITE_TOOLS = frozenset({
    "browser_click", "browser_type", "browser_submit", "browser_select",
    "browser_set_checked",
})

ROLE_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    WorkerRole.PLANNER: frozenset(),
    WorkerRole.RESEARCHER: _BROWSER_READ_TOOLS | {
        "mission_save_finding", "mission_note_source", "mission_save_question"},
    WorkerRole.BROWSER_OPERATOR: _BROWSER_READ_TOOLS | _BROWSER_WRITE_TOOLS | {
        "mission_save_finding"},
    WorkerRole.ANALYST: frozenset({"mission_save_finding", "mission_save_question"}),
    WorkerRole.WRITER: frozenset({"mission_save_result"}),
    WorkerRole.CRITIC: frozenset({"mission_save_question"}),
}


@dataclass
class WorkerTask:
    """One bounded piece of work, and how it went - the structured record
    workers communicate through instead of talking to each other."""

    id: int
    role: str
    title: str
    instructions: str
    depends_on: tuple[int, ...] = ()
    state: str = WorkerState.QUEUED
    result: str = ""
    error: str = ""
    findings_added: int = 0
    #: Set the moment this worker's session runs a non-read-only tool,
    #: before it runs - see TaskRunner's identical mechanism in Phase 7.
    #: A failed worker with this set is never auto-retried; see
    #: MissionCoordinator._on_worker_failed.
    write_attempted: bool = False


@dataclass
class CoordinatorLimits:
    """Hard caps - see the phase's own LIMITS section. Every one of these
    is enforced before it would matter, never discovered by running out."""

    max_workers: int = 6
    #: Independent research tasks allowed to run at the same time. A
    #: Browser Operator task is never parallelised with another Browser
    #: Operator task regardless of this cap - see _ready_batch.
    max_parallel: int = 3
    #: Applied to every worker's own AgentConfig.limits before it runs -
    #: the existing per-session guards, not a new mechanism.
    max_turns_per_worker: int = 8
    max_tool_calls_per_worker: int = 20
    #: A coordinator-level ceiling on how many workers may ever be
    #: launched for one Mission run, independent of max_workers (which
    #: bounds the *plan*) - this bounds runaway re-planning/revision
    #: rounds too.
    max_total_workers_launched: int = 12
    #: Optional token budget across every worker, if the provider reports
    #: usage - see MissionCoordinator._token_budget_exhausted. None means
    #: no budget is enforced.
    max_total_tokens: int | None = None


#: Goal phrasing that suggests real decomposition is worth it. Kept small
#: and honest: this is a heuristic, not an understanding of the goal, and
#: it is deliberately conservative - see should_delegate's docstring.
_DELEGATION_PHRASES = (
    "compare", " vs ", "versus", "pros and cons", "research and", "and then write",
    "and then summarize", "comprehensive", "multiple sources", "step by step",
    "in depth", "thorough analysis", "and write a report",
)


def should_delegate(goal: str, *, word_threshold: int = 40) -> bool:
    """Is this goal worth splitting across specialised workers?

    Deliberately conservative - "Do not delegate everything automatically"
    is an explicit requirement. A goal must either name something that
    plainly implies several distinct pieces of work (comparing things,
    citing multiple sources, "research and then write") or simply be long
    enough that it is unlikely to be a single quick lookup. Everything
    else - which is most goals - stays a single AgentSession, exactly as
    it always has.
    """
    text = (goal or "").strip().lower()
    if not text:
        return False
    if any(phrase in text for phrase in _DELEGATION_PHRASES):
        return True
    return len(text.split()) > word_threshold


#: What the Planner is asked to return - reuses the exact JSON-answer
#: mechanism a Skill's output_schema already uses (see
#: app/agent/skills.validate_output), not a new structured-output path.
PLAN_SCHEMA = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["role", "title", "instructions"],
            },
        },
    },
}

#: What the Critic is asked to return.
CRITIC_SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string"},
    },
}


def parse_plan(text: str, *, start_id: int = 1) -> list[WorkerTask]:
    """The Planner's decomposition, or [] if it could not be understood.

    An empty list is not an error the caller need report as one - see
    MissionCoordinator._run_planner, which falls back to a small default
    plan rather than failing the whole Mission over one malformed answer.
    """
    from app.agent.skills import OutputValidationError, validate_output

    try:
        parsed = validate_output(PLAN_SCHEMA, text)
    except OutputValidationError:
        return []

    tasks: list[WorkerTask] = []
    raw_tasks = parsed.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in WorkerRole.ALL or role in (WorkerRole.PLANNER,):
            continue
        depends_on = tuple(
            d for d in item.get("depends_on", []) if isinstance(d, int))
        tasks.append(WorkerTask(
            id=start_id + index, role=role,
            title=str(item.get("title", ""))[:200],
            instructions=str(item.get("instructions", ""))[:4000],
            depends_on=depends_on,
        ))
    return tasks


def parse_critic_verdict(text: str) -> dict:
    """The Critic's structured answer, or a safe default if it could not
    be parsed - an unreadable Critic answer must never silently block or
    crash the Mission; it is treated as "nothing flagged" and the failure
    is recorded on the WorkerTask itself, not hidden."""
    from app.agent.skills import OutputValidationError, validate_output

    try:
        parsed = validate_output(CRITIC_SCHEMA, text)
    except OutputValidationError:
        return {"verdict": "unsupported", "unsupported_claims": [], "needs_more_research": False,
                "parse_error": True}
    return {
        "verdict": str(parsed.get("verdict", "")),
        "unsupported_claims": [str(c) for c in parsed.get("unsupported_claims", [])
                               if isinstance(c, (str, int, float))][:10],
        "needs_more_research": bool(parsed.get("needs_more_research", False)),
        "parse_error": False,
    }


def _is_write_tool(tool_name: str) -> bool:
    return bool(tool_name) and tool_name not in READ_ONLY_TOOLS and tool_name not in SEARCH_TOOLS


class MissionCoordinator(QObject):
    """Runs one delegated Mission: plans it, executes the plan against the
    one shared MissionService, and synthesizes a result.

    ``session_factory`` returns a fresh, ready-to-use AgentSession-shaped
    object each call - the same construction MainWindow already uses for
    its own interactive session (see build_session), never a second kind
    of session. Each worker gets its own instance so independent research
    can genuinely run in parallel; nothing here maintains a private copy
    of Mission state; a worker's world starts and ends with the plain text
    this coordinator hands it and the SAME MissionService everything else
    already reads and writes.
    """

    #: A worker was created and added to the plan.
    worker_added = Signal(object)          # WorkerTask
    #: A worker's state or result changed.
    worker_changed = Signal(object)        # WorkerTask
    #: A worker is waiting on a sensitive action - carries (WorkerTask,
    #: ConfirmationRequest, session) so the UI can resolve it.
    worker_confirmation_required = Signal(object, object, object)
    #: The Mission's final synthesized result is ready.
    result_ready = Signal(str)
    #: The whole run could not produce a result - message only.
    failed = Signal(str)
    #: The run ended, one way or another - always the last signal emitted.
    finished = Signal()

    def __init__(
        self,
        missions,
        session_factory: Callable[[], Any],
        limits: CoordinatorLimits | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._missions = missions
        self._session_factory = session_factory
        self.limits = limits or CoordinatorLimits()
        self.tasks: list[WorkerTask] = []
        self._next_id = 1
        self._workers_launched = 0
        self._active_sessions: dict[int, Any] = {}   # task id -> session
        self._running_ids: set[int] = set()
        self._on_stage_done: Callable[[], None] | None = None
        self._revision_used = False
        self._total_tokens_used = 0
        self._goal = ""

    # -- entry point --------------------------------------------------------
    def run(self, goal: str) -> None:
        self._goal = goal
        if not should_delegate(goal):
            # A simple goal - the coordinator's own judgement is that
            # delegation would not help. Callers that reach this branch
            # are expected to fall back to the ordinary single-session
            # path instead (see MainWindow); this coordinator does not
            # run a lone worker over a goal it decided not to delegate.
            self.failed.emit("This goal did not need delegation - use the ordinary agent.")
            self.finished.emit()
            return
        self._run_planner(goal)

    @property
    def delegated(self) -> bool:
        return bool(self.tasks)

    # -- planning -------------------------------------------------------------
    def _run_planner(self, goal: str) -> None:
        mission = self._missions.active
        planner_task = WorkerTask(id=0, role=WorkerRole.PLANNER, title="Decompose the goal",
                                  instructions=goal, state=WorkerState.RUNNING)
        self.worker_added.emit(planner_task)

        prompt = self._planner_prompt(goal, mission)
        session = self._build_worker_session(WorkerRole.PLANNER)
        if session is None:
            self._fallback_plan(planner_task, goal, "Py is not available.")
            return
        self._workers_launched += 1

        def on_finished() -> None:
            self._disconnect_terminal(session, on_finished, on_error)
            text = getattr(session, "last_text", "") or ""
            tasks = parse_plan(text, start_id=1)
            if not tasks:
                planner_task.error = "Could not understand the plan - using a default plan."
                self._fallback_plan(planner_task, goal, planner_task.error)
                return
            planner_task.state = WorkerState.DONE
            planner_task.result = f"Created a {len(tasks)}-step plan."
            self.worker_changed.emit(planner_task)
            self._begin_execution(tasks)

        def on_error(message: str) -> None:
            pass  # on_finished always follows; nothing to do here alone.

        self._wire_text_capture(session)
        session.finished.connect(on_finished)
        session.error.connect(on_error)
        if not session.send(prompt):
            self._fallback_plan(planner_task, goal, "Py was busy.")

    def _fallback_plan(self, planner_task: WorkerTask, goal: str, reason: str) -> None:
        planner_task.state = WorkerState.FAILED
        planner_task.error = reason
        self.worker_changed.emit(planner_task)
        # A minimal, always-safe plan: look into it, then write it up. Never
        # zero tasks - a Mission the user asked to delegate still needs an
        # answer even when the Planner itself could not be reached.
        tasks = [
            WorkerTask(id=1, role=WorkerRole.RESEARCHER, title="Look into the goal",
                      instructions=goal),
            WorkerTask(id=2, role=WorkerRole.WRITER, title="Write up the findings",
                      instructions="Summarize the findings gathered so far.",
                      depends_on=(1,)),
        ]
        self._begin_execution(tasks)

    def _planner_prompt(self, goal: str, mission) -> str:
        schema_hint = (
            "Reply with a single JSON object and nothing else, shaped like: "
            '{"tasks": [{"role": "researcher", "title": "...", '
            '"instructions": "...", "depends_on": []}]}. '
            f"Valid roles: {', '.join(r for r in WorkerRole.ALL if r != WorkerRole.PLANNER)}. "
            "depends_on lists the 1-based positions of other tasks in this same list "
            f"that must finish first. Return at most {self.limits.max_workers} tasks. "
            "Do not include a planner task for yourself. Only include a writer task if "
            "the goal needs a final written answer.")
        return (f"You are the Planner for this Mission.\nGoal: {goal}\n"
               f"{self._shared_state_block(mission)}\n"
               "Decide whether this goal is best split into a small number of bounded "
               "tasks for specialised workers (Researcher: gathers information; "
               "Browser Operator: performs browser actions like filling forms; "
               "Analyst: compares/synthesizes findings; Writer: writes the final answer; "
               "Critic: checks claims). Keep the plan small and avoid unnecessary tasks.\n"
               f"{schema_hint}")

    # -- shared state, assembled fresh from MissionService every time --------
    def _shared_state_block(self, mission) -> str:
        if mission is None:
            return ""
        lines = [f"Mission title: {mission.title}"]
        if mission.constraints:
            lines.append("Constraints: " + "; ".join(mission.constraints))
        if mission.findings:
            lines.append("Findings so far:")
            for finding in mission.findings[-20:]:
                lines.append(f"  {finding.label}: {finding.text}")
        if mission.questions:
            open_questions = [q for q in mission.questions if q.status == "open"]
            if open_questions:
                lines.append("Open questions:")
                for question in open_questions[-10:]:
                    lines.append(f"  - {question.text}")
        return "\n".join(lines)

    # -- execution --------------------------------------------------------
    def _begin_execution(self, tasks: list[WorkerTask]) -> None:
        research_tasks = [t for t in tasks if t.role in WorkerRole.RESEARCH]
        analyst_tasks = [t for t in tasks if t.role == WorkerRole.ANALYST]
        critic_tasks = [t for t in tasks if t.role == WorkerRole.CRITIC]
        writer_tasks = [t for t in tasks if t.role == WorkerRole.WRITER]
        if not writer_tasks:
            writer_tasks = [WorkerTask(
                id=max((t.id for t in tasks), default=0) + 1, role=WorkerRole.WRITER,
                title="Write up the findings",
                instructions="Write the mission's final result from the findings recorded so far.")]

        # Bound the whole plan up front. The Mission is always guaranteed a
        # Writer slot (a delegated Mission that quietly produced no answer
        # would be worse than a shorter research phase), so that is
        # reserved first; the rest goes to research tasks (what the
        # Planner actually varies), then Analyst, then Critic.
        budget = max(0, self.limits.max_workers - 1)  # planner already spent one slot
        reserve_writer = 1 if writer_tasks else 0
        remaining = max(0, budget - reserve_writer)
        research_tasks = research_tasks[:remaining]
        remaining = max(0, remaining - len(research_tasks))
        analyst_tasks = analyst_tasks[:min(1, remaining)]
        remaining = max(0, remaining - len(analyst_tasks))
        critic_tasks = critic_tasks[:min(1, remaining)]
        writer_tasks = writer_tasks[:1] if reserve_writer else writer_tasks[:min(1, remaining)]

        self.tasks = research_tasks + analyst_tasks + critic_tasks + writer_tasks
        for task in self.tasks:
            self.worker_added.emit(task)

        self._run_research_stage(research_tasks, lambda: self._run_analyst_stage(
            analyst_tasks, critic_tasks, writer_tasks))

    # -- research stage: dependency + parallel bounded ------------------------
    def _run_research_stage(self, research_tasks: list[WorkerTask], on_done: Callable) -> None:
        if not research_tasks:
            on_done()
            return
        self._run_batches(research_tasks, on_done)

    def _run_batches(self, tasks: list[WorkerTask], on_done: Callable) -> None:
        pending = {t.id: t for t in tasks if t.state == WorkerState.QUEUED}
        if not pending:
            on_done()
            return

        def try_launch_more() -> None:
            if self._budget_exhausted():
                for task in pending.values():
                    if task.state == WorkerState.QUEUED:
                        task.state = WorkerState.SKIPPED
                        task.error = "Skipped - worker budget exhausted."
                        self.worker_changed.emit(task)
                pending.clear()
                on_done()
                return
            ready = self._ready_batch(pending, tasks)
            if not ready and not self._running_ids & set(pending):
                # nothing ready and nothing running - a cyclic or broken
                # dependency graph. Fail those tasks rather than hang.
                for task in pending.values():
                    task.state = WorkerState.SKIPPED
                    task.error = "Skipped - unmet dependency."
                    self.worker_changed.emit(task)
                pending.clear()
                on_done()
                return
            for task in ready:
                self._launch_worker(task, on_worker_done=lambda t=task: on_worker_terminal(t))

        def on_worker_terminal(task: WorkerTask) -> None:
            pending.pop(task.id, None)
            if pending or self._running_ids & {t.id for t in tasks}:
                try_launch_more()
            else:
                on_done()

        try_launch_more()

    def _ready_batch(self, pending: dict[int, WorkerTask], all_tasks: list[WorkerTask]) -> list[WorkerTask]:
        by_id = {t.id: t for t in all_tasks}
        slots = self.limits.max_parallel - len(self._running_ids)
        if slots <= 0:
            return []
        browser_operator_running = any(
            by_id[i].role == WorkerRole.BROWSER_OPERATOR for i in self._running_ids if i in by_id)
        ready = []
        for task in pending.values():
            if task.state != WorkerState.QUEUED:
                continue
            deps = [by_id.get(d) for d in task.depends_on]
            if any(d is not None and d.state != WorkerState.DONE for d in deps):
                continue
            if task.role == WorkerRole.BROWSER_OPERATOR:
                if browser_operator_running or any(t.role == WorkerRole.BROWSER_OPERATOR
                                                    for t in ready):
                    continue  # never parallelise write-shaped browser work
            ready.append(task)
            if len(ready) >= slots:
                break
        return ready

    # -- analyst / critic / writer stages -------------------------------------
    def _run_analyst_stage(self, analyst_tasks, critic_tasks, writer_tasks) -> None:
        if not analyst_tasks:
            self._run_critic_stage(critic_tasks, writer_tasks)
            return
        self._launch_worker(analyst_tasks[0],
                            on_worker_done=lambda: self._run_critic_stage(critic_tasks, writer_tasks))

    def _run_critic_stage(self, critic_tasks, writer_tasks) -> None:
        if not critic_tasks:
            self._run_writer_stage(writer_tasks)
            return
        critic_task = critic_tasks[0]
        critic_task.instructions += (
            "\n\nReply with a single JSON object and nothing else, shaped like: "
            '{"verdict": "supported"|"unsupported", "unsupported_claims": ["..."], '
            '"needs_more_research": true|false}.')

        def after_critic() -> None:
            verdict = parse_critic_verdict(critic_task.result)
            if verdict["needs_more_research"] and not self._revision_used \
                    and not self._budget_exhausted():
                self._revision_used = True
                follow_up = WorkerTask(
                    id=self._next_worker_id(), role=WorkerRole.RESEARCHER,
                    title="Follow-up research requested by Critic",
                    instructions="The Critic flagged these as unsupported - investigate "
                                "and either find support or note that none exists: "
                                + "; ".join(verdict["unsupported_claims"] or ["(unspecified)"]))
                self.tasks.append(follow_up)
                self.worker_added.emit(follow_up)
                self._launch_worker(follow_up, on_worker_done=lambda: self._run_writer_stage(writer_tasks))
            else:
                self._run_writer_stage(writer_tasks)

        self._launch_worker(critic_task, on_worker_done=after_critic)

    def _run_writer_stage(self, writer_tasks) -> None:
        if not writer_tasks:
            self._finish(error="No writer available to produce a result.")
            return
        # _launch_worker itself marks the task SKIPPED and records why if
        # the worker budget is already exhausted - never a silent no-op.
        self._launch_worker(writer_tasks[0], on_worker_done=self._finish)

    def _finish(self, error: str | None = None) -> None:
        mission = self._missions.active
        result = (mission.result if mission is not None else "") or ""
        if result:
            self.result_ready.emit(result)
        elif error:
            self.failed.emit(error)
        else:
            self.failed.emit("The Mission finished without producing a result.")
        self.finished.emit()

    # -- one worker's whole lifecycle -----------------------------------------
    def _launch_worker(self, task: WorkerTask, on_worker_done: Callable[[], None]) -> None:
        if self._budget_exhausted():
            task.state = WorkerState.SKIPPED
            task.error = "Skipped - worker budget exhausted."
            self.worker_changed.emit(task)
            on_worker_done()
            return
        session = self._build_worker_session(task.role)
        if session is None:
            task.state = WorkerState.FAILED
            task.error = "Py is not available."
            self.worker_changed.emit(task)
            on_worker_done()
            return

        self._workers_launched += 1
        self._running_ids.add(task.id)
        self._active_sessions[task.id] = session
        task.state = WorkerState.RUNNING
        self.worker_changed.emit(task)
        mission = self._missions.active
        prompt = self._worker_prompt(task, mission)
        findings_before = len(mission.findings) if mission is not None else 0

        self._wire_text_capture(session)

        def on_step_changed(step) -> None:
            if _is_write_tool(getattr(step, "tool", "")) and getattr(step, "state", "") == "running":
                task.write_attempted = True

        def on_confirmation_required(request) -> None:
            task.state = WorkerState.WAITING_FOR_APPROVAL
            self.worker_changed.emit(task)
            self.worker_confirmation_required.emit(task, request, session)

        def on_state_changed(state: str) -> None:
            if state in ("acting", "thinking") and task.state == WorkerState.WAITING_FOR_APPROVAL:
                task.state = WorkerState.RUNNING
                self.worker_changed.emit(task)

        pending_error: list[str] = []

        def on_error(message: str) -> None:
            pending_error.append(message)

        def on_finished() -> None:
            try:
                session.step_changed.disconnect(on_step_changed)
                session.confirmation_required.disconnect(on_confirmation_required)
                session.state_changed.disconnect(on_state_changed)
                session.error.disconnect(on_error)
                session.finished.disconnect(on_finished)
            except (TypeError, RuntimeError):
                pass
            self._running_ids.discard(task.id)
            self._active_sessions.pop(task.id, None)
            self._accumulate_usage(session)
            mission_now = self._missions.active
            task.findings_added = max(
                0, (len(mission_now.findings) if mission_now is not None else 0) - findings_before)
            if pending_error:
                self._on_worker_failed(task, pending_error[0])
            else:
                task.state = WorkerState.DONE
                task.result = getattr(session, "last_text", "") or ""
            self.worker_changed.emit(task)
            self._shutdown_session(session)
            on_worker_done()

        session.step_changed.connect(on_step_changed)
        session.confirmation_required.connect(on_confirmation_required)
        session.state_changed.connect(on_state_changed)
        session.error.connect(on_error)
        session.finished.connect(on_finished)

        if not session.send(prompt):
            task.state = WorkerState.FAILED
            task.error = "Could not start - the worker session was busy."
            self.worker_changed.emit(task)
            self._running_ids.discard(task.id)
            self._active_sessions.pop(task.id, None)
            self._shutdown_session(session)
            on_worker_done()

    def _on_worker_failed(self, task: WorkerTask, message: str) -> None:
        """A worker failed. Never auto-retried if it may have already
        attempted a write - see the module docstring and Phase 7's
        identical rule. A non-critical research failure does not stop the
        Mission; it is simply recorded and the run continues without it.
        """
        task.state = WorkerState.FAILED
        task.error = message

    def _budget_exhausted(self) -> bool:
        if self._workers_launched >= self.limits.max_total_workers_launched:
            return True
        if self.limits.max_total_tokens is not None \
                and self._total_tokens_used >= self.limits.max_total_tokens:
            return True
        return False

    def _accumulate_usage(self, session) -> None:
        usage = getattr(session, "task_usage", None)
        if usage is None:
            return
        self._total_tokens_used += (
            int(getattr(usage, "input_tokens", 0) or 0)
            + int(getattr(usage, "output_tokens", 0) or 0))

    def _next_worker_id(self) -> int:
        self._next_id = max((t.id for t in self.tasks), default=0) + 1
        return self._next_id

    # -- building a worker's session and prompt ------------------------------
    def _build_worker_session(self, role: str):
        session = self._session_factory()
        if session is None:
            return None
        allowed = ROLE_ALLOWED_TOOLS.get(role, frozenset())
        set_allowlist = getattr(session, "set_tool_allowlist", None)
        if callable(set_allowlist):
            set_allowlist(allowed)
        config = getattr(session, "config", None)
        if config is not None and hasattr(config, "limits"):
            config.limits.max_turns = self.limits.max_turns_per_worker
            config.limits.max_tool_calls = self.limits.max_tool_calls_per_worker
        return session

    def _worker_prompt(self, task: WorkerTask, mission) -> str:
        role_label = WorkerRole.LABELS.get(task.role, task.role)
        guidance = _ROLE_GUIDANCE.get(task.role, "")
        prior_results = "\n".join(
            f"- {WorkerRole.LABELS.get(t.role, t.role)} ({t.title}): {t.result}"
            for t in self.tasks
            if t.id in task.depends_on and t.result)
        parts = [
            f"You are acting as the {role_label} for this Mission - one worker among several; "
            "you do not control the Mission and cannot approve your own actions.",
            f"Mission goal: {self._goal}",
            self._shared_state_block(mission),
        ]
        if prior_results:
            parts.append("Results from tasks this depends on:\n" + prior_results)
        parts.append(f"Your assigned task: {task.title}\n{task.instructions}")
        if guidance:
            parts.append(guidance)
        return "\n\n".join(p for p in parts if p)

    def _wire_text_capture(self, session) -> None:
        """Capture the worker's final answer without adding a second
        conversation-tracking mechanism - AgentSession already emits its
        final answer via assistant_message; this just remembers the last
        one for _on_finished to read."""
        session.last_text = ""

        def capture(text: str) -> None:
            session.last_text = text
        session.assistant_message.connect(capture)

    def _shutdown_session(self, session) -> None:
        shutdown = getattr(session, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def _disconnect_terminal(self, session, on_finished, on_error) -> None:
        try:
            session.finished.disconnect(on_finished)
            session.error.disconnect(on_error)
        except (TypeError, RuntimeError):
            pass
        self._shutdown_session(session)


_ROLE_GUIDANCE = {
    WorkerRole.RESEARCHER: (
        "Gather information relevant to your task and cite where it came from. "
        "Save each real discovery with mission_save_finding, passing tab_id explicitly "
        "if you open your own tab. Do not perform any browser action beyond reading."),
    WorkerRole.BROWSER_OPERATOR: (
        "Perform the browser actions your task describes. Always pass tab_id explicitly."),
    WorkerRole.ANALYST: (
        "Compare and synthesize the findings already recorded on this Mission - do not "
        "browse. Save any new comparative insight with mission_save_finding."),
    WorkerRole.WRITER: (
        "Write the Mission's final answer from the findings already recorded, and save it "
        "with mission_save_result. This is the last step - do not leave it unsaved."),
    WorkerRole.CRITIC: (
        "Check the findings and the draft result for unsupported claims - a conclusion "
        "with no finding behind it. Do not browse."),
}

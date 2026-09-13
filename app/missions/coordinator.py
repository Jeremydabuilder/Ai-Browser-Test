"""Multi-Agent Missions: one Mission, several specialised workers, executed
as an explicit, persisted graph (Phase 10 formalizes what Phase 9 already
built - see app/missions/graph.py and app/storage/mission_graph.py; this
module is still the only thing that plans or executes anything).

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

Execution is a real dependency graph, not a scripted stage order: a node
runs the moment every node it depends on has COMPLETED, subject to the
concurrency cap and the rule that a Browser Operator node (it performs
writes) never runs alongside another one. The Planner is still free to
decide how many research tasks there are and what each depends on; this
coordinator only adds the *structural* edges that keep the rest of the
plan sane - Analyst waits for all research, Critic waits for Analyst (or
research if there is no Analyst), and Writer always runs last and always
gets a chance to produce something, even if something upstream failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.agent.tools import READ_ONLY_TOOLS, SEARCH_TOOLS
from app.missions.graph import NodeState, NodeType


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
    #: MissionCoordinator._ready_tasks. Everything else (Analyst, Critic,
    #: Writer) only ever appears once in a plan.
    RESEARCH = (RESEARCHER, BROWSER_OPERATOR)


#: The node "kind" a role most naturally performs - used to fill in a
#: node's type when the Planner did not specify one, and vice versa (see
#: parse_plan). Purely a labelling default: the role is still what decides
#: the tool allowlist.
ROLE_TO_NODE_TYPE = {
    WorkerRole.RESEARCHER: NodeType.RESEARCH,
    WorkerRole.BROWSER_OPERATOR: NodeType.BROWSE,
    WorkerRole.ANALYST: NodeType.ANALYZE,
    WorkerRole.WRITER: NodeType.WRITE,
    WorkerRole.CRITIC: NodeType.VERIFY,
}
NODE_TYPE_TO_ROLE = {
    NodeType.RESEARCH: WorkerRole.RESEARCHER,
    NodeType.BROWSE: WorkerRole.BROWSER_OPERATOR,
    NodeType.EXTRACT: WorkerRole.RESEARCHER,
    NodeType.COMPARE: WorkerRole.ANALYST,
    NodeType.ANALYZE: WorkerRole.ANALYST,
    NodeType.VERIFY: WorkerRole.CRITIC,
    NodeType.WRITE: WorkerRole.WRITER,
    NodeType.MCP_READ: WorkerRole.RESEARCHER,
    NodeType.MCP_ACTION: WorkerRole.BROWSER_OPERATOR,
}


class WorkerState:
    """A WorkerTask's own live, in-memory state for the run in progress -
    distinct from NodeState (app/missions/graph.py), which is what gets
    persisted. NEEDS_REVIEW and CANCELLED exist on the live task too, so a
    task recovered from a stranded node (see MissionCoordinator.resume)
    can show the same thing the graph already says about it."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"

    ALL = (QUEUED, RUNNING, WAITING_FOR_APPROVAL, DONE, FAILED, SKIPPED, CANCELLED, NEEDS_REVIEW)
    TERMINAL = (DONE, FAILED, SKIPPED, CANCELLED)


#: WorkerState <-> NodeState - the only place these two vocabularies meet.
_STATE_TO_NODE_STATE = {
    WorkerState.QUEUED: NodeState.PENDING,
    WorkerState.RUNNING: NodeState.RUNNING,
    WorkerState.WAITING_FOR_APPROVAL: NodeState.WAITING_FOR_APPROVAL,
    WorkerState.DONE: NodeState.COMPLETED,
    WorkerState.FAILED: NodeState.FAILED,
    WorkerState.SKIPPED: NodeState.SKIPPED,
    WorkerState.CANCELLED: NodeState.CANCELLED,
    WorkerState.NEEDS_REVIEW: NodeState.NEEDS_REVIEW,
}
_NODE_STATE_TO_STATE = {
    NodeState.PENDING: WorkerState.QUEUED,
    NodeState.READY: WorkerState.QUEUED,
    NodeState.RUNNING: WorkerState.QUEUED,   # a restart means nothing is really running
    NodeState.WAITING_FOR_APPROVAL: WorkerState.QUEUED,
    NodeState.COMPLETED: WorkerState.DONE,
    NodeState.FAILED: WorkerState.FAILED,
    NodeState.SKIPPED: WorkerState.SKIPPED,
    NodeState.CANCELLED: WorkerState.CANCELLED,
    NodeState.NEEDS_REVIEW: WorkerState.NEEDS_REVIEW,
}


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


def mcp_tools_for_role(role: str, mcp) -> frozenset[str]:
    """Which of the *currently connected* MCP tools this role may use -
    Phase 10's conservative extension of Phase 9 (which granted none).

    Never "all MCP tools because this is multi-agent": a Researcher only
    ever gets tools classified read-only (see app.mcp.types.Sensitivity);
    a Browser Operator gets the tools the Mission/tool policy already
    exposes (the same schemas() list the interactive session sees) -
    every one of them still goes through McpConnectionManager.assess_call
    at call time, so Allow/Ask/Deny, schema fingerprinting and the
    destructive-tool Always-Allow refusal all still apply exactly as they
    do for the interactive session. Analyst/Writer/Critic get none: they
    do not browse or act, so there is nothing for them to need.
    """
    if mcp is None:
        return frozenset()
    try:
        schemas = mcp.schemas()
    except Exception:  # noqa: BLE001 - a broken MCP layer must not crash planning
        return frozenset()
    if role == WorkerRole.RESEARCHER:
        from app.mcp.types import Sensitivity

        names = []
        for schema in schemas:
            name = schema.get("name", "")
            tool = _find_mcp_tool(mcp, name)
            if tool is not None and tool.sensitivity == Sensitivity.READ_ONLY:
                names.append(name)
        return frozenset(names)
    if role == WorkerRole.BROWSER_OPERATOR:
        return frozenset(schema.get("name", "") for schema in schemas)
    return frozenset()


def _find_mcp_tool(mcp, namespaced_name: str):
    try:
        from app.mcp import adapter

        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return None
        server_id, tool_name = parts
        return mcp.find_tool(server_id, tool_name)
    except Exception:  # noqa: BLE001
        return None


@dataclass
class WorkerTask:
    """One bounded piece of work, and how it went - the structured record
    workers communicate through instead of talking to each other. Backed
    by a persisted GraphNode row when the coordinator has a graph store
    (see MissionCoordinator._sync_node); in-memory only otherwise, the
    same as Phase 9."""

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
    #: What kind of work this is, for the Mission Plan UI - see NodeType.
    node_type: str = ""
    attempt_count: int = 0
    plan_round: int = 0

    def __post_init__(self) -> None:
        if not self.node_type:
            self.node_type = ROLE_TO_NODE_TYPE.get(self.role, NodeType.RESEARCH)


@dataclass
class CoordinatorLimits:
    """Hard caps - see the phase's own LIMITS section. Every one of these
    is enforced before it would matter, never discovered by running out."""

    max_workers: int = 6
    #: Independent research tasks allowed to run at the same time. A
    #: Browser Operator task is never parallelised with another Browser
    #: Operator task regardless of this cap - see _ready_tasks.
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
    #: Total persisted graph nodes one Mission run may ever create,
    #: across the initial plan and every later Critic-triggered revision -
    #: "Do not permit infinite graph expansion."
    max_graph_nodes: int = 16
    #: How many times one node may be retried (see MissionCoordinator.
    #: retry_node) before it is left FAILED for good.
    max_retries: int = 2
    #: How many times the Critic may add a follow-up research round.
    max_revision_rounds: int = 1
    #: Independent nodes allowed to run at once - the DAG-level name for
    #: max_parallel; kept as a separate field since the two ideas (worker
    #: concurrency vs. node concurrency) happen to be the same cap in this
    #: implementation but are conceptually distinct enough to want their
    #: own name in the phase's own limits list.
    @property
    def max_concurrent_nodes(self) -> int:
        return self.max_parallel


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
        node_type = item.get("type")
        if node_type not in NodeType.ALL:
            node_type = ROLE_TO_NODE_TYPE.get(role, NodeType.RESEARCH)
        depends_on = tuple(
            d for d in item.get("depends_on", []) if isinstance(d, int))
        tasks.append(WorkerTask(
            id=start_id + index, role=role,
            title=str(item.get("title", ""))[:200],
            instructions=str(item.get("instructions", ""))[:4000],
            depends_on=depends_on, node_type=node_type,
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
    """Runs one delegated Mission: plans it, executes the plan as a
    dependency graph against the one shared MissionService, and
    synthesizes a result.

    ``session_factory`` returns a fresh, ready-to-use AgentSession-shaped
    object each call - the same construction MainWindow already uses for
    its own interactive session (see build_session), never a second kind
    of session. Each worker gets its own instance so independent research
    can genuinely run in parallel; nothing here maintains a private copy
    of Mission state; a worker's world starts and ends with the plain text
    this coordinator hands it and the SAME MissionService everything else
    already reads and writes.

    ``graph_store`` is optional. Passed, every plan and every state
    transition is mirrored into SQLite as it happens (see
    app/storage/mission_graph.py) - restart-safe, inspectable from the
    Mission Plan UI, and exactly what recover_after_restart reads back.
    Omitted, the coordinator behaves exactly as Phase 9's did: an
    in-memory-only run, still fully functional, just not restart-safe -
    kept for anything that only needs a quick, disposable delegation.
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
    #: The plan is ready and (require_plan_approval=True) waiting for
    #: start_execution()/reject_plan() - "Py plans to do N steps" in the UI.
    plan_ready = Signal()

    def __init__(
        self,
        missions,
        session_factory: Callable[[], Any],
        limits: CoordinatorLimits | None = None,
        parent: QObject | None = None,
        graph_store: Any = None,
        require_plan_approval: bool = False,
    ) -> None:
        super().__init__(parent)
        self._missions = missions
        self._session_factory = session_factory
        self.limits = limits or CoordinatorLimits()
        self._graph_store = graph_store
        self._mission_id: int | None = None
        self.tasks: list[WorkerTask] = []
        self._next_id = 1
        self._workers_launched = 0
        self._active_sessions: dict[int, Any] = {}   # task id -> session
        self._running_ids: set[int] = set()
        self._revision_rounds_used = 0
        self._total_tokens_used = 0
        self._goal = ""
        self._cancelled = False
        #: When true, _materialize_plan stops after persisting the plan
        #: and waits for start_execution() - "Py plans to do N steps,
        #: [Start] [Edit plan]" in the phase's own PLAN TRANSPARENCY
        #: section. False (the default) preserves Phase 9's behaviour:
        #: execution begins the moment the plan exists.
        self._require_plan_approval = require_plan_approval
        self._plan_pending = False

    # -- entry point --------------------------------------------------------
    def run(self, goal: str) -> None:
        self._goal = goal
        mission = self._missions.active
        self._mission_id = mission.id if mission is not None else None
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
            self._materialize_plan(tasks)

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
        self._materialize_plan(tasks)

    def _planner_prompt(self, goal: str, mission) -> str:
        schema_hint = (
            "Reply with a single JSON object and nothing else, shaped like: "
            '{"tasks": [{"role": "researcher", "type": "research", "title": "...", '
            '"instructions": "...", "depends_on": []}]}. '
            f"Valid roles: {', '.join(r for r in WorkerRole.ALL if r != WorkerRole.PLANNER)}. "
            f"Valid types: {', '.join(NodeType.ALL)} (type is optional - a sensible one is "
            "filled in from the role if you omit it). "
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

    # -- turning a parsed plan into a bounded, persisted graph ---------------
    def _materialize_plan(self, tasks: list[WorkerTask]) -> None:
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
        # Unlike the worker budget above, the Planner itself is never
        # persisted as a node, so it does not need a slot reserved here.
        budget = min(budget, self.limits.max_graph_nodes)
        reserve_writer = 1 if writer_tasks else 0
        remaining = max(0, budget - reserve_writer)
        research_tasks = research_tasks[:remaining]
        remaining = max(0, remaining - len(research_tasks))
        analyst_tasks = analyst_tasks[:min(1, remaining)]
        remaining = max(0, remaining - len(analyst_tasks))
        critic_tasks = critic_tasks[:min(1, remaining)]
        writer_tasks = writer_tasks[:1] if reserve_writer else writer_tasks[:min(1, remaining)]

        ordered = research_tasks + analyst_tasks + critic_tasks + writer_tasks

        # Structural edges - see the module docstring. Added on top of
        # whatever the Planner already said, never replacing it.
        research_ids_placeholder = tuple(t.id for t in research_tasks)
        for task in analyst_tasks:
            task.depends_on = tuple(set(task.depends_on) | set(research_ids_placeholder))
        analyst_or_research = tuple(t.id for t in analyst_tasks) or research_ids_placeholder
        for task in critic_tasks:
            task.depends_on = tuple(set(task.depends_on) | set(analyst_or_research))
        critic_or_upstream = (tuple(t.id for t in critic_tasks) or analyst_or_research)
        for task in writer_tasks:
            task.depends_on = tuple(set(task.depends_on) | set(critic_or_upstream))

        self.tasks = self._persist_plan(ordered)
        for task in self.tasks:
            self.worker_added.emit(task)
        if self._require_plan_approval:
            self._plan_pending = True
            self.plan_ready.emit()
            return
        self._advance()

    # -- plan transparency: approve/edit before anything runs ---------------
    def start_execution(self) -> bool:
        """The user pressed Start on the plan preview. No-op (returns
        False) if there was never a plan waiting, or it already started."""
        if not self._plan_pending:
            return False
        self._plan_pending = False
        self._advance()
        return True

    def reject_plan(self) -> None:
        """The user declined the plan outright - equivalent to cancelling
        before anything ran."""
        if not self._plan_pending:
            return
        self._plan_pending = False
        self.cancel()

    def rename_task(self, node_id: int, title: str) -> bool:
        """Plan-preview editing: rename a step. Refused once the plan has
        started (a task already RUNNING or terminal keeps its title as a
        record of what actually happened)."""
        if not self._plan_pending:
            return False
        task = next((t for t in self.tasks if t.id == node_id), None)
        if task is None:
            return False
        title = (title or "").strip()[:200]
        if not title:
            return False
        task.title = title
        self._sync_node_title(task)
        self.worker_changed.emit(task)
        return True

    def remove_task(self, node_id: int) -> bool:
        """Plan-preview editing: drop an optional step before the plan
        starts. Refused for the Writer (a Mission always keeps one) and
        for any *non-Writer* task that depends on it, so removing a step
        can never leave a dangling requirement. A Writer depending on it
        is not itself a reason to refuse: the Writer already tolerates a
        dependency that never completed (see _ready_tasks) - that is
        exactly what "optional" means here.
        """
        if not self._plan_pending:
            return False
        task = next((t for t in self.tasks if t.id == node_id), None)
        if task is None or task.role == WorkerRole.WRITER:
            return False
        if any(node_id in t.depends_on for t in self.tasks
               if t.id != node_id and t.role != WorkerRole.WRITER):
            return False
        task.state = WorkerState.CANCELLED
        task.error = "Removed from the plan before it started."
        self._sync_node(task)
        self.worker_changed.emit(task)
        self.tasks = [t for t in self.tasks if t.id != node_id]
        return True

    def _sync_node_title(self, task: WorkerTask) -> None:
        if self._graph_store is not None:
            self._graph_store.set_title(task.id, task.title)

    def _persist_plan(self, tasks: list[WorkerTask]) -> list[WorkerTask]:
        """Give every task in ``tasks`` a real, final id - a persisted
        graph node's row id when a graph store is attached, or the
        in-memory placeholder id it already carries otherwise (Phase 9's
        behaviour, unchanged) - and remap every depends_on entry from the
        placeholder ids parse_plan assigned to those final ids.
        """
        if self._graph_store is None or self._mission_id is None:
            return tasks
        # Pass 1: persist every task in order, collecting old (placeholder)
        # id -> new (real row) id - every old id must be known before any
        # depends_on entry can be remapped, hence the two passes.
        old_to_new: dict[int, int] = {}
        for task in tasks:
            if self._graph_count_exhausted():
                continue
            node = self._graph_store.create_node(
                self._mission_id, node_type=task.node_type, role=task.role,
                title=task.title, instructions=task.instructions,
                plan_round=task.plan_round)
            if node is None:
                continue
            old_to_new[task.id] = node.id
        # Pass 2: rewrite each surviving task's own id and its depends_on.
        persisted: list[WorkerTask] = []
        for task in tasks:
            if task.id not in old_to_new:
                continue
            new_id = old_to_new[task.id]
            new_deps = tuple(old_to_new[d] for d in task.depends_on if d in old_to_new)
            task.id = new_id
            task.depends_on = new_deps
            self._graph_store.set_dependencies(new_id, list(new_deps))
            persisted.append(task)
        return persisted

    def _graph_count_exhausted(self) -> bool:
        if self._graph_store is None or self._mission_id is None:
            return False
        return self._graph_store.count_for_mission(self._mission_id) >= self.limits.max_graph_nodes

    # -- the generic scheduler ------------------------------------------------
    def _advance(self) -> None:
        if self._cancelled:
            return
        self._propagate_failures()
        if all(t.state in WorkerState.TERMINAL for t in self.tasks):
            self._finish()
            return
        ready = self._ready_tasks()
        if not ready and not self._running_ids:
            for task in self.tasks:
                if task.state == WorkerState.QUEUED:
                    task.state = WorkerState.SKIPPED
                    task.error = "Skipped - unmet dependency."
                    self._sync_node(task)
                    self.worker_changed.emit(task)
            self._finish()
            return
        for task in ready:
            self._launch_worker(task)

    def _propagate_failures(self) -> None:
        """A task that depends on one that FAILED or was SKIPPED can never
        become ready - mark it SKIPPED too, so it does not sit QUEUED
        forever. The Writer is the one exception: it always gets a chance
        to produce *something*, and is told in its own prompt which
        dependencies did not come through rather than being left to guess
        or to silently pretend they did (see _worker_prompt).
        """
        by_id = {t.id: t for t in self.tasks}
        changed = True
        while changed:
            changed = False
            for task in self.tasks:
                if task.state != WorkerState.QUEUED or task.role == WorkerRole.WRITER:
                    continue
                deps = [by_id.get(d) for d in task.depends_on]
                if any(d is not None and d.state in (WorkerState.FAILED, WorkerState.SKIPPED)
                       for d in deps):
                    task.state = WorkerState.SKIPPED
                    task.error = "Skipped - a required task did not complete."
                    self._sync_node(task)
                    self.worker_changed.emit(task)
                    changed = True

    def _ready_tasks(self) -> list[WorkerTask]:
        if self._budget_exhausted():
            return []
        slots = self.limits.max_parallel - len(self._running_ids)
        if slots <= 0:
            return []
        by_id = {t.id: t for t in self.tasks}
        browser_operator_running = any(
            by_id[i].role == WorkerRole.BROWSER_OPERATOR
            for i in self._running_ids if i in by_id)
        ready: list[WorkerTask] = []
        for task in self.tasks:
            if task.state != WorkerState.QUEUED:
                continue
            deps = [by_id.get(d) for d in task.depends_on]
            if task.role == WorkerRole.WRITER:
                # The Writer always gets a chance to produce something,
                # even if a dependency failed - it proceeds once every
                # dependency has reached *some* terminal state, and is
                # told in its own prompt which ones did not complete (see
                # _worker_prompt) rather than waiting on them forever.
                if any(d is None or d.state not in WorkerState.TERMINAL for d in deps):
                    continue
            elif any(d is None or d.state != WorkerState.DONE for d in deps):
                continue
            if task.role == WorkerRole.BROWSER_OPERATOR:
                if browser_operator_running or any(
                        t.role == WorkerRole.BROWSER_OPERATOR for t in ready):
                    continue  # never parallelise write-shaped browser work
            ready.append(task)
            if len(ready) >= slots:
                break
        return ready

    def _finish(self) -> None:
        mission = self._missions.active
        result = (mission.result if mission is not None else "") or ""
        if result:
            self.result_ready.emit(result)
        else:
            failed_titles = [t.title for t in self.tasks if t.role == WorkerRole.WRITER
                             and t.state != WorkerState.DONE]
            if failed_titles:
                self.failed.emit("The Writer could not produce a result.")
            else:
                self.failed.emit("The Mission finished without producing a result.")
        self.finished.emit()

    # -- one worker's whole lifecycle -----------------------------------------
    def _launch_worker(self, task: WorkerTask) -> None:
        if self._budget_exhausted():
            task.state = WorkerState.SKIPPED
            task.error = "Skipped - worker budget exhausted."
            self._sync_node(task)
            self.worker_changed.emit(task)
            self._advance()
            return
        session = self._build_worker_session(task.role)
        if session is None:
            task.state = WorkerState.FAILED
            task.error = "Py is not available."
            self._sync_node(task)
            self.worker_changed.emit(task)
            self._advance()
            return

        self._workers_launched += 1
        self._running_ids.add(task.id)
        self._active_sessions[task.id] = session
        task.state = WorkerState.RUNNING
        task.attempt_count += 1
        self._sync_node(task)
        self.worker_changed.emit(task)
        mission = self._missions.active
        prompt = self._worker_prompt(task, mission)
        findings_before = len(mission.findings) if mission is not None else 0

        self._wire_text_capture(session)

        def on_step_changed(step) -> None:
            if _is_write_tool(getattr(step, "tool", "")) and getattr(step, "state", "") == "running":
                task.write_attempted = True
                if self._graph_store is not None:
                    self._graph_store.set_write_attempted(task.id, True)

        def on_confirmation_required(request) -> None:
            task.state = WorkerState.WAITING_FOR_APPROVAL
            self._sync_node(task)
            self.worker_changed.emit(task)
            self.worker_confirmation_required.emit(task, request, session)

        def on_state_changed(state: str) -> None:
            if state in ("acting", "thinking") and task.state == WorkerState.WAITING_FOR_APPROVAL:
                task.state = WorkerState.RUNNING
                self._sync_node(task)
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
                if task.role == WorkerRole.CRITIC:
                    self._handle_critic_result(task)
            self._sync_node(task)
            self.worker_changed.emit(task)
            self._shutdown_session(session)
            self._advance()

        session.step_changed.connect(on_step_changed)
        session.confirmation_required.connect(on_confirmation_required)
        session.state_changed.connect(on_state_changed)
        session.error.connect(on_error)
        session.finished.connect(on_finished)

        if not session.send(prompt):
            task.state = WorkerState.FAILED
            task.error = "Could not start - the worker session was busy."
            self._sync_node(task)
            self.worker_changed.emit(task)
            self._running_ids.discard(task.id)
            self._active_sessions.pop(task.id, None)
            self._shutdown_session(session)
            self._advance()

    def _handle_critic_result(self, critic_task: WorkerTask) -> None:
        """The Critic may add exactly one bounded revision node - never an
        open-ended back-and-forth. See CoordinatorLimits.max_revision_rounds."""
        verdict = parse_critic_verdict(critic_task.result)
        if not verdict["needs_more_research"]:
            return
        if self._revision_rounds_used >= self.limits.max_revision_rounds:
            return
        if self._budget_exhausted() or self._graph_count_exhausted():
            return
        self._revision_rounds_used += 1
        follow_up = self._create_task(
            role=WorkerRole.RESEARCHER, node_type=NodeType.RESEARCH,
            title="Follow-up research requested by Critic",
            instructions="The Critic flagged these as unsupported - investigate "
                        "and either find support or note that none exists: "
                        + "; ".join(verdict["unsupported_claims"] or ["(unspecified)"]),
            depends_on=(), plan_round=self._revision_rounds_used)
        if follow_up is None:
            return
        self.tasks.append(follow_up)
        self.worker_added.emit(follow_up)
        # Every writer still queued must wait for this new node too.
        for writer in self.tasks:
            if writer.role == WorkerRole.WRITER and writer.state == WorkerState.QUEUED:
                writer.depends_on = writer.depends_on + (follow_up.id,)
                if self._graph_store is not None:
                    self._graph_store.set_dependencies(writer.id, list(writer.depends_on))

    def _create_task(
        self, *, role: str, node_type: str, title: str, instructions: str,
        depends_on: tuple[int, ...] = (), plan_round: int = 0,
    ) -> WorkerTask | None:
        """A task created after the initial plan (a Critic follow-up) -
        persisted the same way the initial plan is, or given the next
        in-memory id when there is no graph store."""
        if self._graph_store is not None and self._mission_id is not None:
            if self._graph_count_exhausted():
                return None
            node = self._graph_store.create_node(
                self._mission_id, node_type=node_type, role=role, title=title,
                instructions=instructions, dependencies=list(depends_on), plan_round=plan_round)
            if node is None:
                return None
            return WorkerTask(id=node.id, role=role, title=title, instructions=instructions,
                              depends_on=depends_on, node_type=node_type, plan_round=plan_round)
        if len(self.tasks) >= self.limits.max_graph_nodes:
            return None
        new_id = max((t.id for t in self.tasks), default=0) + 1
        return WorkerTask(id=new_id, role=role, title=title, instructions=instructions,
                          depends_on=depends_on, node_type=node_type, plan_round=plan_round)

    def _on_worker_failed(self, task: WorkerTask, message: str) -> None:
        """A worker failed. Never auto-retried if it may have already
        attempted a write - see the module docstring and Phase 7's
        identical rule. A non-critical research failure does not stop the
        Mission; it is simply recorded and the run continues without it.
        """
        task.state = WorkerState.FAILED
        task.error = message

    # -- user-facing controls: retry / skip / cancel -------------------------
    def retry_node(self, node_id: int) -> bool:
        """Explicitly retry a FAILED/NEEDS_REVIEW/SKIPPED task - never
        automatic. Refused past max_retries, or while its dependencies are
        not all satisfied, or while something with this id is already
        running."""
        task = next((t for t in self.tasks if t.id == node_id), None)
        if task is None or task.id in self._running_ids:
            return False
        if task.state not in (WorkerState.FAILED, WorkerState.NEEDS_REVIEW, WorkerState.SKIPPED):
            return False
        if task.attempt_count > self.limits.max_retries:
            return False
        by_id = {t.id: t for t in self.tasks}
        deps = [by_id.get(d) for d in task.depends_on]
        if any(d is None or d.state != WorkerState.DONE for d in deps):
            return False
        task.state = WorkerState.QUEUED
        task.error = ""
        task.write_attempted = False
        if self._graph_store is not None:
            self._graph_store.reset_for_retry(task.id)
        self.worker_changed.emit(task)
        self._advance()
        return True

    def skip_node(self, node_id: int) -> bool:
        """Explicitly give up on a task without retrying it - a user
        override, never automatic. Refused while the task is running or
        already terminal."""
        task = next((t for t in self.tasks if t.id == node_id), None)
        if task is None or task.id in self._running_ids:
            return False
        if task.state in WorkerState.TERMINAL:
            return False
        task.state = WorkerState.SKIPPED
        task.error = task.error or "Skipped by the user."
        self._sync_node(task)
        self.worker_changed.emit(task)
        self._advance()
        return True

    def cancel(self) -> None:
        """Stop the whole run. Every non-terminal task becomes CANCELLED;
        anything mid-flight is asked to shut down, exactly like closing
        the window stops the interactive session."""
        self._cancelled = True
        for task in self.tasks:
            if task.state not in WorkerState.TERMINAL:
                was_running = task.id in self._running_ids
                task.state = WorkerState.CANCELLED
                self._sync_node(task)
                self.worker_changed.emit(task)
                if was_running:
                    session = self._active_sessions.pop(task.id, None)
                    if session is not None:
                        self._shutdown_session(session)
        self._running_ids.clear()
        self.finished.emit()

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

    # -- keeping the persisted graph in step with the live task -------------
    def _sync_node(self, task: WorkerTask) -> None:
        if self._graph_store is None:
            return
        node_state = _STATE_TO_NODE_STATE.get(task.state, NodeState.PENDING)
        if task.state == WorkerState.RUNNING:
            self._graph_store.record_start(task.id)
        elif task.state in WorkerState.TERMINAL:
            self._graph_store.record_terminal(
                task.id, state=node_state, result_summary=task.result[:2000],
                error=task.error, findings_added=task.findings_added)
        else:
            self._graph_store.set_state(task.id, node_state)

    # -- building a worker's session and prompt ------------------------------
    def _build_worker_session(self, role: str):
        session = self._session_factory()
        if session is None:
            return None
        allowed = ROLE_ALLOWED_TOOLS.get(role, frozenset())
        mcp = getattr(session, "_mcp", None)
        allowed = allowed | mcp_tools_for_role(role, mcp)
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
        by_id = {t.id: t for t in self.tasks}
        dep_lines = []
        for dep_id in task.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                continue
            dep_label = WorkerRole.LABELS.get(dep.role, dep.role)
            if dep.state == WorkerState.DONE and dep.result:
                dep_lines.append(f"- {dep_label} ({dep.title}): {dep.result}")
            elif dep.state in (WorkerState.FAILED, WorkerState.SKIPPED):
                dep_lines.append(
                    f"- {dep_label} ({dep.title}) DID NOT COMPLETE ({dep.error or dep.state}) - "
                    "do not assume its information exists; say plainly that it is missing "
                    "rather than filling in a plausible-sounding answer.")
        parts = [
            f"You are acting as the {role_label} for this Mission - one worker among several; "
            "you do not control the Mission and cannot approve your own actions.",
            f"Mission goal: {self._goal}",
            self._shared_state_block(mission),
        ]
        if dep_lines:
            parts.append("Results from tasks this depends on:\n" + "\n".join(dep_lines))
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
        "with mission_save_result. This is the last step - do not leave it unsaved. If a "
        "dependency did not complete, say so plainly in the answer rather than pretending "
        "its information exists."),
    WorkerRole.CRITIC: (
        "Check the findings and the draft result for unsupported claims - a conclusion "
        "with no finding behind it. Do not browse."),
}

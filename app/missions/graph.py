"""The Mission Execution Graph: the persisted, restart-safe shape of the
plan MissionCoordinator already builds (see app/missions/coordinator.py).

This module only holds the data - node types, node states, and the
GraphNode row shape a store hands back. It knows nothing about roles,
AgentSession, or tool allowlists; the coordinator is still the only place
that decides what a node's role or tools are, and still the only thing
that executes anything. Deliberately no dependency-graph algorithms live
here either - "which nodes are ready" is a live scheduling question the
coordinator answers against its own in-flight WorkerTask list, not
something this pure data module needs to know how to compute.
"""

from __future__ import annotations

from dataclasses import dataclass


class NodeType:
    """What kind of work a node represents - for planning and the Mission
    Plan UI. Distinct from the *role* that executes it (see
    app.missions.coordinator.WorkerRole): a node's type is a label a user
    can read at a glance ("Research", "Verify"); its role is an
    implementation detail (which tool allowlist applies).
    """

    RESEARCH = "research"
    BROWSE = "browse"
    EXTRACT = "extract"
    COMPARE = "compare"
    ANALYZE = "analyze"
    VERIFY = "verify"
    WRITE = "write"
    MCP_READ = "mcp_read"
    MCP_ACTION = "mcp_action"
    USER_APPROVAL = "user_approval"

    ALL = (RESEARCH, BROWSE, EXTRACT, COMPARE, ANALYZE, VERIFY, WRITE,
           MCP_READ, MCP_ACTION, USER_APPROVAL)

    LABELS = {
        RESEARCH: "Research", BROWSE: "Browse", EXTRACT: "Extract",
        COMPARE: "Compare", ANALYZE: "Analyze", VERIFY: "Verify", WRITE: "Write",
        MCP_READ: "MCP Read", MCP_ACTION: "MCP Action", USER_APPROVAL: "User Approval",
    }


class NodeState:
    """The persisted lifecycle of one graph node - see the phase's own
    PERSISTENCE section. Distinct from WorkerState (the coordinator's
    live, in-memory runtime state): a node can be NEEDS_REVIEW only after
    a restart finds it stranded mid-write, which a live run's WorkerState
    never needs to express on its own.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    #: A restart found this node mid-write (or mid-MCP-action) with no way
    #: to know whether it finished. Never auto-retried - see
    #: app/storage/mission_graph.py's recover_after_restart.
    NEEDS_REVIEW = "needs_review"

    ALL = (PENDING, READY, RUNNING, WAITING_FOR_APPROVAL, COMPLETED, FAILED,
           SKIPPED, CANCELLED, NEEDS_REVIEW)
    TERMINAL = (COMPLETED, FAILED, SKIPPED, CANCELLED)


@dataclass(frozen=True)
class GraphNode:
    """One persisted row - what app/storage/mission_graph.py hands back.
    The Mission Plan UI and restart recovery read these; the live
    coordinator run works from its own WorkerTask list and keeps this
    store in sync as it goes (see MissionCoordinator._sync_node)."""

    id: int
    mission_id: int
    node_type: str
    role: str
    title: str
    instructions: str
    dependencies: tuple[int, ...]
    state: str
    attempt_count: int
    write_attempted: bool
    result_summary: str
    error: str
    findings_added: int
    plan_round: int
    created_at: str
    started_at: str | None
    completed_at: str | None

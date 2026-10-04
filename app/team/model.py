"""Plain data for a Team mission: tasks, artifacts, sources, events.

Everything round-trips through ``to_dict``/``from_dict`` so a whole mission
is one JSON document in the store (app/storage/team_store.py).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


class AgentId:
    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    WRITER = "writer"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"

    ALL = (COORDINATOR, RESEARCHER, WRITER, CODER, REVIEWER, TESTER)
    #: Agents a plan may assign tasks to. The Coordinator plans and assembles
    #: but is never itself a task node.
    ASSIGNABLE = (RESEARCHER, WRITER, CODER, REVIEWER, TESTER)
    LABELS = {
        COORDINATOR: "Coordinator", RESEARCHER: "Researcher", WRITER: "Writer",
        CODER: "Coder", REVIEWER: "Reviewer", TESTER: "Tester",
    }
    BLURBS = {
        COORDINATOR: "Plans the mission, assigns work, assembles the result",
        RESEARCHER: "Gathers evidence from your sources, keeps the links",
        WRITER: "Reports, comparisons, drafts and documentation",
        CODER: "Writes or edits code; proposes changes, never applies them itself",
        REVIEWER: "Checks requirements, evidence and quality; asks for revisions",
        TESTER: "Runs real checks in the sandbox and reports what happened",
    }


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    #: A dependency failed or was skipped, so this never ran.
    BLOCKED = "blocked"
    #: Deliberately not run (e.g. nothing executable to test).
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    ALL = (PENDING, RUNNING, DONE, FAILED, BLOCKED, SKIPPED, CANCELLED)
    TERMINAL = (DONE, FAILED, BLOCKED, SKIPPED, CANCELLED)
    #: States a dependent task treats as "this input will never arrive".
    UNUSABLE = (FAILED, BLOCKED, CANCELLED)


class MissionStatus:
    DRAFT = "draft"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    #: Finished and assembled, but the Reviewer's concerns were not all
    #: resolved within the revision budget, or a check failed.
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The app closed while this was running.
    INTERRUPTED = "interrupted"

    ACTIVE = (PLANNING, RUNNING)
    FINISHED = (COMPLETED, COMPLETED_WITH_ISSUES, FAILED, CANCELLED, INTERRUPTED)
    LABELS = {
        DRAFT: "Draft", PLANNING: "Planning", RUNNING: "Running", COMPLETED: "Completed",
        COMPLETED_WITH_ISSUES: "Completed with open issues", FAILED: "Failed",
        CANCELLED: "Cancelled", INTERRUPTED: "Interrupted",
    }


class ArtifactKind:
    PLAN = "plan"
    NOTES = "notes"
    REPORT = "report"
    FILE = "file"
    TEST_REPORT = "test_report"
    REVIEW = "review"
    FINAL = "final"

    LABELS = {
        PLAN: "Plan", NOTES: "Research notes", REPORT: "Draft", FILE: "File",
        TEST_REPORT: "Test report", REVIEW: "Review", FINAL: "Final result",
    }


class SourceKind:
    TAB = "tab"
    PASTE = "paste"
    FILE = "file"
    KNOWLEDGE = "knowledge"
    #: A search-engine result the Researcher found - NOT something the user attached.
    WEB = "web"

    #: Kinds the user chose to attach.
    ATTACHED = (TAB, PASTE, FILE)
    LABELS = {TAB: "attached tab", PASTE: "attached text", FILE: "attached file",
              KNOWLEDGE: "local knowledge", WEB: "web search result"}


class SourceStatus:
    INCLUDED = "included"
    TRUNCATED = "truncated"
    #: Could not be read; the reason is in ``error``. Never sent to a model.
    INACCESSIBLE = "inaccessible"


class EventKind:
    STATUS = "status"
    HANDOFF = "handoff"
    TOOL = "tool"
    REVIEW = "review"
    WARNING = "warning"
    ERROR = "error"


def now() -> float:
    return time.time()


@dataclass
class Source:
    id: str
    kind: str
    title: str
    url: str = ""
    text: str = ""
    status: str = SourceStatus.INCLUDED
    error: str = ""

    @property
    def origin_label(self) -> str:
        return SourceKind.LABELS.get(self.kind, self.kind)

    @property
    def usable(self) -> bool:
        return self.status in (SourceStatus.INCLUDED, SourceStatus.TRUNCATED) and bool(self.text.strip())

    def manifest_line(self) -> str:
        state = self.status if self.status != SourceStatus.INCLUDED else "included"
        extra = f" - {self.error}" if self.error else ""
        where = f" <{self.url}>" if self.url else ""
        return f"[{self.id}] ({self.origin_label}, {state}) {self.title}{where}{extra} - {len(self.text)} chars"


@dataclass
class Artifact:
    id: str
    kind: str
    title: str
    content: str
    agent: str
    task_id: str = ""
    version: int = 1
    #: Id of the artifact this one replaces (a revision), or "".
    replaces: str = ""
    #: Free-form: for FILE artifacts {"path", "diff", "is_new", "applied"};
    #: for notes/drafts {"cited": [...], "invalid_citations": [...]}.
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now)


@dataclass
class Task:
    id: str
    title: str
    agent: str
    instructions: str = ""
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    #: Source ids this task is explicitly given (a Researcher with none gets all).
    sources: list[str] = field(default_factory=list)
    status: str = TaskStatus.PENDING
    attempts: int = 0
    #: Artifact ids this task produced - the explicit handoff to dependents.
    outputs: list[str] = field(default_factory=list)
    #: Artifact ids this task was handed (recorded when it starts).
    inputs: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    error_kind: str = ""
    revision_of: str = ""
    round: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class Event:
    seq: int
    ts: float
    agent: str
    kind: str
    text: str


@dataclass
class Mission:
    id: int = 0
    goal: str = ""
    status: str = MissionStatus.DRAFT
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    success_criteria: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    #: Honest notes about what was unavailable or unverified.
    limitations: list[str] = field(default_factory=list)
    #: What the Coordinator is doing outside any task node.
    coordinator_note: str = ""
    workspace_path: str = ""
    #: The user allowed this run to send search queries to a web search API.
    web_search: bool = False
    final_artifact_id: str = ""
    error: str = ""
    error_kind: str = ""
    review_rounds: int = 0
    unresolved_issues: list[str] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_label: str = ""
    next_seq: int = 1

    # -- lookups ------------------------------------------------------------
    def task(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def artifact(self, artifact_id: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.id == artifact_id), None)

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mission":
        def build(kind, items):
            names = {f for f in kind.__dataclass_fields__}
            return [kind(**{k: v for k, v in item.items() if k in names}) for item in items or []]

        names = set(cls.__dataclass_fields__)
        plain = {k: v for k, v in data.items()
                 if k in names and k not in ("sources", "tasks", "artifacts", "events")}
        mission = cls(**plain)
        mission.sources = build(Source, data.get("sources"))
        mission.tasks = build(Task, data.get("tasks"))
        mission.artifacts = build(Artifact, data.get("artifacts"))
        mission.events = build(Event, data.get("events"))
        return mission

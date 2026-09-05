"""What a Mission is, in plain data.

A Mission is a goal the user is trying to accomplish, and the pages that
contributed to it. That is the whole idea: a browser normally remembers URLs
and tabs, and remembers nothing about *why* any of them are open.

Two rules this module exists to enforce:

* **Missions hold URLs, never tab ids.** A ``tab_id`` is an in-memory counter
  owned by BrowserController and means nothing after a restart. Holding one
  would make a Mission corruptible by closing a tab; holding a URL makes that
  impossible by construction.
* **Page identity lives behind :func:`page_key`.** Today it is URL equality.
  Tomorrow it may need to fold redirects, strip tracking parameters, or ignore
  fragments. Every comparison of "is this the same page?" goes through that one
  function so the change stays a one-line change.

Nothing here touches SQLite, Qt, or the agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


class MissionStatus:
    """Three states, deliberately. See the note in service.py about why this is
    not the same concept as Py's mascot state."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"

    ALL = (ACTIVE, PAUSED, COMPLETED)

    #: How each one reads in the UI.
    LABELS = {ACTIVE: "ACTIVE", PAUSED: "PAUSED", COMPLETED: "COMPLETE"}


class PageSource:
    """How a page came to be part of a Mission."""

    AGENT = "agent"        # Py opened or navigated it
    READ = "read"          # Py read a page the user already had open
    USER = "user"          # reserved: the user added it deliberately

    ALL = (AGENT, READ, USER)


#: Longest title we will store or show. Titles come from web pages, which are
#: written by strangers; a 40kB <title> is not a display problem to solve later.
MAX_TITLE = 200
MAX_URL = 2048
MAX_GOAL = 2000

#: Schemes that are never part of a Mission: internal pages, blank tabs, error
#: pages and inline data are not places the user went. Mirrors the same
#: judgement BookmarkStore makes about what is worth remembering.
IGNORED_SCHEMES = ("about:", "data:", "chrome-error:", "chrome://", "javascript:",
                   "blob:", "pybrowser:", "file:", "view-source:")


def is_associable(url: str) -> bool:
    """Is this a page a Mission should remember?"""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        return False
    return not url.lower().startswith(IGNORED_SCHEMES)


def page_key(url: str) -> str:
    """The identity of a page, for "have we already got this one?".

    V1 is URL equality, with whitespace trimmed. It is a function rather than
    an inline ``==`` so that canonicalisation - dropping ``utm_*``, folding a
    redirect chain onto its destination, ignoring the fragment - becomes an
    edit here instead of a hunt through the UI.

    Callers must never compare ``page.url`` directly; compare
    ``page_key(a) == page_key(b)``.
    """
    return (url or "").strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MissionPage:
    """One page that contributed to a Mission."""

    id: int
    mission_id: int
    url: str
    title: str = ""
    source: str = PageSource.AGENT
    #: Why this page is here. Empty in V1 - the column exists so that writing
    #: "why did we open this" later is not a migration.
    note: str = ""
    first_seen: str = ""
    last_seen: str = ""

    @property
    def key(self) -> str:
        return page_key(self.url)

    @property
    def domain(self) -> str:
        """The host, without ``www.``, for display. Never used for identity."""
        match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", self.url.strip())
        host = match.group(1) if match else self.url.strip()
        host = host.split("@")[-1].split(":")[0]
        return host[4:] if host.lower().startswith("www.") else host

    @property
    def display_title(self) -> str:
        return self.title.strip() or self.domain or self.url


#: Longest finding we will store. Not a truncation point: a finding over this
#: is refused with an error telling the model to shorten it. Silently cutting
#: "$129 until Friday" down to "$129" would store a fact with its qualifier
#: removed, which is worse than spending one more tool call.
MAX_FINDING_CHARS = 200

#: Findings one Mission may hold. A Mission is a task, not a notebook; past
#: this the panel stops being readable and the board stops being a summary.
MAX_FINDINGS_PER_MISSION = 40


def finding_key(text: str) -> str:
    """The identity of a finding, for "have we already recorded this?".

    Deliberately exact-after-normalisation: case, whitespace and trailing
    punctuation are noise, and nothing else is. No fuzzy matching - a
    similarity threshold that silently swallows a genuinely new finding is a
    worse failure than a near-duplicate the user can delete in one click, and
    it cannot be tested deterministically.

    Same reasoning as page_key: one function, so tightening it later is one
    edit rather than a hunt through the UI.
    """
    return " ".join((text or "").split()).strip(" .,;:!-\u2013\u2014").lower()


@dataclass(frozen=True)
class MissionFinding:
    """One thing Py discovered, and where it came from.

    The text is written by the model, never copied from a page: Py reads
    untrusted page content and writes a sentence about it. The *source* is not
    the model's to claim - it is resolved from the real tab at save time. See
    MissionService.save_finding.
    """

    id: int
    mission_id: int
    text: str
    key: str = ""
    #: Mission-local number. See finding_ref: this is what the user and the
    #: model see, never the row id.
    ref: int = 0
    #: The mission_pages row this came from, or None if the page has since been
    #: forgotten. Losing a source costs the attribution, never the discovery.
    page_id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    #: Filled in by the store when it reads the joined page row.
    source_url: str = ""
    source_title: str = ""

    @property
    def label(self) -> str:
        return finding_ref(self.ref) if self.ref else ""

    @property
    def age(self) -> str:
        return relative_age(self.created_at)

    @property
    def source_domain(self) -> str:
        if not self.source_url:
            return ""
        return MissionPage(id=0, mission_id=self.mission_id,
                           url=self.source_url).domain


#: Recorded activity kept per mission. Unlike findings, this is an
#: operational log, not a fact a decision might cite - so it is trimmed
#: (oldest dropped) rather than refused once full. Generous enough to cover
#: a long task without becoming the thing the page has to render.
MAX_ACTIONS_PER_MISSION = 300
MAX_ACTION_DESCRIPTION_CHARS = 200

#: A stage label, not a report - see Mission.progress. Short like a title,
#: truncated rather than refused: losing the tail of a status line costs
#: nothing a user would miss, unlike a finding or a decision.
MAX_PROGRESS_CHARS = 80

#: The exact progress label MissionService.on_agent_state_changed writes
#: while a mission is blocked on the user's approval. Shared with
#: missions_page.py so the Library can show a blocked mission distinctly
#: (a warning colour, not the ordinary progress accent) without inventing a
#: second, separately-maintained flag for what is really one fact.
BLOCKED_LABEL = "Waiting for your approval"

#: The mission's outcome, and the plain suggestions that follow it - see
#: Mission.result and Mission.follow_ups. A result can be a full structured
#: comparison, so it gets far more room than a single finding.
MAX_RESULT_CHARS = 4000
MAX_FOLLOW_UPS = 5
MAX_FOLLOW_UP_CHARS = 200


@dataclass(frozen=True)
class MissionAction:
    """One thing Py did, or tried to do, while working this Mission.

    This is the persisted twin of AgentSession's transient Step - it exists
    so "what did Py actually do" survives a restart and a resumed Mission,
    not just the findings that resulted from it. The description is the same
    short, user-facing sentence a Step already shows ("Opening Tennis
    Warehouse," "Reading 12 products") - never raw tool arguments, and never
    a page's own untrusted text.
    """

    id: int
    mission_id: int
    #: Short, user-facing sentence - see the docstring above.
    description: str
    #: The tool this came from, e.g. "browser_navigate" - for grouping/icons
    #: in the UI. Empty for a plain narrative entry with no specific tool.
    tool_name: str = ""
    #: "done" | "failed" | "waiting" - mirrors StepState loosely enough for
    #: the UI to pick a glyph, without importing agent-side state here.
    outcome: str = "done"
    #: The page this action touched, if any - lets the UI offer "open this
    #: tab" the way a live Step can. None once that page is forgotten.
    page_id: int | None = None
    created_at: str = ""

    @property
    def age(self) -> str:
        return relative_age(self.created_at)


#: Longest a decision's headline may be. Refused, not truncated - the same
#: reasoning as MAX_FINDING_CHARS.
MAX_DECISION_CHARS = 200

#: Longest the user-visible rationale may be. Room for a few sentences of
#: "why", not room for an essay or for a transcript of how it was reached.
MAX_RATIONALE_CHARS = 600

#: Alternatives and evidence carried by one decision. A decision that needs
#: more than this is a research board, not a decision.
MAX_ALTERNATIVES = 5
MAX_EVIDENCE = 8


@dataclass(frozen=True)
class DecisionAlternative:
    """Something that was considered and not chosen, and why not."""

    id: int
    decision_id: int
    name: str
    reason: str
    position: int = 0


@dataclass(frozen=True)
class DecisionEvidence:
    """One fact the decision rested on, as it read at the time.

    Both a reference and a snapshot, deliberately. A reference alone would let
    a later edit rewrite history - the decision would claim evidence that did
    not exist when it was made, which is a confident wrong answer to the exact
    question this feature exists to answer. A snapshot alone would drift from
    the live board with no way to notice.
    """

    id: int
    decision_id: int
    #: The finding this came from, or None once that finding is gone.
    finding_id: int | None
    #: What the finding said when the decision was made. Never rewritten.
    text: str
    source: str = ""
    position: int = 0
    #: What the finding says now, or None if it no longer exists. Filled by the
    #: store from the live row; not stored.
    current_text: str | None = None
    #: The finding's mission-local number, and the verdict of its live
    #: challenge if it has one. Both filled by the store.
    ref: int = 0
    verdict: str = ""

    @property
    def missing(self) -> bool:
        """The finding this cites has been deleted."""
        return self.finding_id is None or self.current_text is None

    @property
    def changed(self) -> bool:
        """The finding still exists but no longer says what it said."""
        return self.current_text is not None and self.current_text != self.text

    @property
    def label(self) -> str:
        return finding_ref(self.ref) if self.ref else ""

    @property
    def state(self) -> str:
        """What has happened to this evidence, most serious thing first."""
        candidates = []
        if self.missing:
            candidates.append(EvidenceState.MISSING)
        if self.verdict in Verdict.ALL:
            candidates.append(self.verdict)
        if self.changed:
            candidates.append(EvidenceState.CHANGED)
        return EvidenceState.worst(candidates or [EvidenceState.UNCHALLENGED])

    @property
    def glyph(self) -> str:
        return EvidenceState.GLYPHS.get(self.state, "")

    @property
    def note(self) -> str:
        return EvidenceState.LABELS.get(self.state, "")


@dataclass(frozen=True)
class MissionDecision:
    """What was decided, and the reasons a person can read.

    Deliberately contains no model reasoning. ``rationale`` is the sentence
    shown to the user; there is nowhere here to put a chain of thought, and
    that is the point.

    A saved decision is a record. It is never permission: see
    app/missions/service.py and the note in app/agent/prompt.py.
    """

    id: int
    mission_id: int
    decision: str
    rationale: str
    created_at: str = ""
    #: Empty while this is the live decision; a timestamp once a later one
    #: replaced it. Decisions are appended, never overwritten - "we changed our
    #: mind" is itself part of the record.
    superseded_at: str = ""
    alternatives: tuple[DecisionAlternative, ...] = field(default_factory=tuple)
    evidence: tuple[DecisionEvidence, ...] = field(default_factory=tuple)
    assumptions: tuple[DecisionAssumption, ...] = field(default_factory=tuple)
    #: The verdict of this decision's own live challenge, if it has one.
    #: Filled by the store; part of the status rule below.
    verdict: str = ""

    @property
    def live(self) -> bool:
        return not self.superseded_at

    @property
    def status(self) -> str:
        """How this decision is standing up, read from its evidence right now.

        Computed, never stored: a stored status goes stale the moment a
        challenge lands somewhere else, and the graph is meant to reflect the
        evidence as it is. Nothing here rewrites the decision.
        """
        return DecisionStatus.of((e.state for e in self.evidence), self.verdict)

    @property
    def status_label(self) -> str:
        return DecisionStatus.LABELS.get(self.status, self.status.upper())

    @property
    def challenge_verdict_label(self) -> str:
        return Verdict.LABELS.get(self.verdict, "") if self.verdict else ""


#: How a finding is named to the user and to the model. Mission-local and
#: never a database id: "F3" always means "this mission's third finding", so
#: there is no way to express a reference to another mission's finding at all.
FINDING_PREFIX = "F"
DECISION_REF = "D"


def finding_ref(ref: int) -> str:
    """The label for a finding's mission-local number."""
    return f"{FINDING_PREFIX}{int(ref)}"


def parse_finding_ref(text: str) -> int | None:
    """The number in a finding reference, or None if it is not one.

    Accepts "F3", "f3" and a bare "3", because a model that has been reading
    "F3" all conversation will sometimes write one and sometimes the other,
    and a citation refused on formatting is a wasted turn. What it will not do
    is guess: anything else is None, and the caller reports it.
    """
    text = (text or "").strip().upper()
    if text.startswith(FINDING_PREFIX):
        text = text[len(FINDING_PREFIX):]
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    return None


class EvidenceState:
    """What has happened to one piece of supporting evidence since.

    ``ORDER`` is the precedence, most serious first, and it is explicit for a
    reason: an item can be several of these at once - a finding that was
    reworded *and* contradicted - and which one the UI shows must never depend
    on the order rows came back from a query.
    """

    MISSING = "missing"              # the finding was deleted
    CONTRADICTED = "contradicted"    # its challenge found against it
    WEAKENED = "weakened"
    UNRESOLVED = "unresolved"
    CHANGED = "changed"              # the finding now reads differently
    UPHELD = "upheld"                # challenged, and it held
    UNCHALLENGED = "unchallenged"    # nobody has attacked it

    ORDER = (MISSING, CONTRADICTED, WEAKENED, UNRESOLVED, CHANGED,
             UPHELD, UNCHALLENGED)

    #: A glyph, so state is not carried by colour alone.
    GLYPHS = {MISSING: "\u2715", CONTRADICTED: "\u2715", WEAKENED: "!",
              UNRESOLVED: "?", CHANGED: "~", UPHELD: "\u2713",
              UNCHALLENGED: "\u2713"}

    LABELS = {MISSING: "finding removed", CONTRADICTED: "contradicted",
              WEAKENED: "weakened", UNRESOLVED: "unresolved",
              CHANGED: "text changed since", UPHELD: "upheld",
              UNCHALLENGED: ""}

    @staticmethod
    def worst(states) -> str:
        """The most serious of several states. Deterministic by ORDER."""
        present = set(states)
        for state in EvidenceState.ORDER:
            if state in present:
                return state
        return EvidenceState.UNCHALLENGED


class DecisionStatus:
    """How a decision is standing up, read from its evidence right now.

    Computed, never stored. A stored status is one that goes stale the moment
    a challenge lands somewhere else, and the whole point is that the graph
    reflects the evidence as it is. Nothing here rewrites the decision.
    """

    NEEDS_REVIEW = "needs review"
    CHECK = "check"
    SOUND = "sound"

    LABELS = {NEEDS_REVIEW: "NEEDS REVIEW", CHECK: "CHECK", SOUND: "SOUND"}

    #: Evidence states that force each status. First rule that matches wins.
    _REVIEW = (EvidenceState.CONTRADICTED, EvidenceState.MISSING)
    _CHECK = (EvidenceState.WEAKENED, EvidenceState.UNRESOLVED,
              EvidenceState.CHANGED)

    @staticmethod
    def of(evidence_states, decision_verdict: str = "") -> str:
        """The status, by the precedence agreed with the user.

        1. NEEDS REVIEW - the decision itself was contradicted, or any support
           is contradicted or gone.
        2. CHECK - the decision was weakened or left unresolved, or any support
           is weakened, unresolved, or has changed since.
        3. SOUND - otherwise.
        """
        states = set(evidence_states)
        if decision_verdict == Verdict.CONTRADICTED or (states & set(DecisionStatus._REVIEW)):
            return DecisionStatus.NEEDS_REVIEW
        if decision_verdict in (Verdict.WEAKENED, Verdict.UNRESOLVED) or (
                states & set(DecisionStatus._CHECK)):
            return DecisionStatus.CHECK
        return DecisionStatus.SOUND


#: Assumptions one decision may carry. A decision resting on more than a
#: handful of unstated things is a decision that has not been made yet.
MAX_ASSUMPTIONS = 5
MAX_ASSUMPTION_CHARS = 200


@dataclass(frozen=True)
class DecisionAssumption:
    """Something the decision takes for granted, stated out loud.

    User-visible data, like everything else here. It is what the decision
    rests on, not a record of how it was reached.
    """

    id: int
    decision_id: int
    text: str
    position: int = 0


class Confidence:
    """How sure Py is about a predicted effect. User-visible, never hidden."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ALL = (LOW, MEDIUM, HIGH)
    LABELS = {LOW: "LOW CONFIDENCE", MEDIUM: "MEDIUM CONFIDENCE", HIGH: "HIGH CONFIDENCE"}


class EffectKind:
    """How one predicted effect reads at a glance - a plus, a caution, or
    neither. Closed set, same reasoning as PointKind: the UI groups by it."""

    BENEFIT = "benefit"
    RISK = "risk"
    NEUTRAL = "neutral"
    ALL = (BENEFIT, RISK, NEUTRAL)
    GLYPHS = {BENEFIT: "+", RISK: "!", NEUTRAL: "\u00b7"}


#: Limits, refused rather than truncated - the same reasoning as everywhere
#: else data crosses from the agent into storage.
MAX_GHOST_RUN_OPTION_CHARS = 120
MAX_GHOST_RUN_EFFECTS = 8
MAX_GHOST_RUN_EFFECT_CHARS = 160


@dataclass(frozen=True)
class GhostRunEffect:
    """One predicted consequence of choosing an option, before it is chosen."""

    id: int
    ghost_run_id: int
    text: str
    kind: str = EffectKind.NEUTRAL
    position: int = 0

    @property
    def glyph(self) -> str:
        return EffectKind.GLYPHS.get(self.kind, "\u00b7")


@dataclass(frozen=True)
class GhostRun:
    """A written prediction of what choosing one option would lead to -
    written BEFORE anything is done, so options can be compared before one is
    picked. Simulate first, execute second: this table is the simulation, and
    it never performs the option it describes. Nothing here is - or can ever
    become - permission to act; see app/agent/tools.py and the trust note in
    app/agent/prompt.py.
    """

    id: int
    mission_id: int
    option: str
    confidence: str = Confidence.MEDIUM
    created_at: str = ""
    effects: tuple[GhostRunEffect, ...] = field(default_factory=tuple)

    @property
    def confidence_label(self) -> str:
        return Confidence.LABELS.get(self.confidence, self.confidence.upper())


class Verdict:
    """How a claim stood up to being attacked.

    Four, and a closed set. The point of Challenge Mode is that the answer is
    one of a small number of things a person can act on, not a paragraph they
    have to interpret.
    """

    UPHELD = "upheld"                # nothing found that undermines it
    WEAKENED = "weakened"            # still stands, but less firmly
    CONTRADICTED = "contradicted"    # evidence points the other way
    UNRESOLVED = "unresolved"        # could not be settled either way

    ALL = (UPHELD, WEAKENED, CONTRADICTED, UNRESOLVED)
    LABELS = {UPHELD: "UPHELD", WEAKENED: "WEAKENED",
              CONTRADICTED: "CONTRADICTED", UNRESOLVED: "UNRESOLVED"}


class PointKind:
    """Why a piece of counter-evidence matters.

    A `kind` here earns its place where the decision's would not have: the UI
    groups by it, and it is the difference between an adversarial check and
    another pile of notes.
    """

    CONFLICT = "conflict"       # evidence pointing the other way
    CONTEXT = "context"         # something important the claim leaves out
    OUTDATED = "outdated"       # true once, not now
    BIAS = "bias"               # who benefits from the claim being believed
    UNRESOLVED = "unresolved"   # a question the search could not answer

    ALL = (CONFLICT, CONTEXT, OUTDATED, BIAS, UNRESOLVED)
    LABELS = {CONFLICT: "CONFLICTS", CONTEXT: "MISSING CONTEXT",
              OUTDATED: "OUT OF DATE", BIAS: "INCENTIVES",
              UNRESOLVED: "UNRESOLVED"}


class TargetKind:
    """What was challenged."""

    FINDING = "finding"
    DECISION = "decision"
    ALL = (FINDING, DECISION)


#: Limits, refused rather than truncated - see MAX_FINDING_CHARS.
MAX_CHALLENGE_SUMMARY = 600
MAX_POINT_CHARS = 200
MAX_POINTS = 8


@dataclass(frozen=True)
class ChallengePoint:
    """One thing found while attacking a claim."""

    id: int
    challenge_id: int
    kind: str
    text: str
    page_id: int | None = None
    position: int = 0
    #: Where it was found, joined from the Mission page. Not stored twice.
    source_url: str = ""
    source_title: str = ""

    @property
    def label(self) -> str:
        return PointKind.LABELS.get(self.kind, self.kind.upper())

    @property
    def source_domain(self) -> str:
        if not self.source_url:
            return ""
        return MissionPage(id=0, mission_id=0, url=self.source_url).domain


@dataclass(frozen=True)
class MissionChallenge:
    """The result of trying to prove a claim wrong.

    Never replaces what it challenges. The original finding or decision is
    left exactly as it was and this sits beside it, because the user needs to
    see both to judge - which is the whole feature.

    ``claim`` is a snapshot of the challenged text. If the finding is later
    edited or deleted, the challenge still says what it was actually made
    against; the same reasoning as decision evidence.
    """

    id: int
    mission_id: int
    target_kind: str
    target_id: int
    claim: str
    verdict: str
    summary: str
    created_at: str = ""
    superseded_at: str = ""
    points: tuple[ChallengePoint, ...] = field(default_factory=tuple)

    @property
    def live(self) -> bool:
        return not self.superseded_at

    @property
    def verdict_label(self) -> str:
        return Verdict.LABELS.get(self.verdict, self.verdict.upper())

    @property
    def stands(self) -> bool:
        """Did the claim survive? Used only for display emphasis."""
        return self.verdict == Verdict.UPHELD

    def points_of(self, kind: str) -> list[ChallengePoint]:
        return [point for point in self.points if point.kind == kind]


@dataclass(frozen=True)
class Mission:
    """A goal, and the pages that served it."""

    id: int
    title: str
    goal: str
    status: str = MissionStatus.ACTIVE
    created_at: str = ""
    updated_at: str = ""
    #: Filled by the store when the caller asked for them; empty otherwise.
    pages: tuple[MissionPage, ...] = field(default_factory=tuple)
    findings: tuple[MissionFinding, ...] = field(default_factory=tuple)
    #: The current decision, if one has been made. Superseded ones are kept in
    #: the database but never carried here: the product shows one decision.
    decision: MissionDecision | None = None
    #: Live challenges on this Mission, whatever they target.
    challenges: tuple[MissionChallenge, ...] = field(default_factory=tuple)
    #: The Mission this one branched from, or None for a root Mission.
    parent_id: int | None = None
    #: What distinguishes this branch from its siblings - "Budget", "Fastest".
    #: Empty on a root Mission.
    branch_name: str = ""
    #: A short, current-stage label - "Comparing 3 options," "Waiting for
    #: approval," "Done." Deliberately a label, not a percentage: an
    #: open-ended web task has no denominator to divide by, and a fake 63%
    #: would claim a precision nobody has. Set by the agent as it works
    #: (mission_set_progress) and by the service on pause/resume/completion.
    progress: str = ""
    #: The mission's own outcome, written once real work is done - distinct
    #: from a Decision, which is "what was chosen and why." A result can
    #: exist without a decision (a pure research/comparison mission answers
    #: the goal without choosing anything), and holds the structured
    #: comparison/summary text the mission was for.
    result: str = ""
    #: What Py suggests doing next, once the mission has a result - "track
    #: prices for another week," "check availability again closer to the
    #: date." Plain suggestions, never anything that acts on their own.
    follow_ups: tuple[str, ...] = field(default_factory=tuple)
    #: Recent recorded activity, filled by the store when asked for it - see
    #: MissionAction. Empty unless requested, same convention as pages/findings.
    actions: tuple["MissionAction", ...] = field(default_factory=tuple)

    @property
    def is_branch(self) -> bool:
        return self.parent_id is not None

    @property
    def has_result(self) -> bool:
        return bool(self.result.strip())

    def challenge_of(self, kind: str, target_id: int) -> MissionChallenge | None:
        """The live challenge against one finding or decision, if any."""
        return next((c for c in self.challenges
                     if c.target_kind == kind and c.target_id == target_id), None)

    @property
    def is_active(self) -> bool:
        return self.status == MissionStatus.ACTIVE

    @property
    def is_complete(self) -> bool:
        return self.status == MissionStatus.COMPLETED

    @property
    def status_label(self) -> str:
        return MissionStatus.LABELS.get(self.status, self.status.upper())


# ---------------------------------------------------------------------------
# Deriving a title from a goal
# ---------------------------------------------------------------------------
#
# "Find me the best tennis shoes under $140 for hard courts" -> "Tennis Shoes".
#
# Done locally, with no API call, because a Mission is created the instant the
# user presses the button and waiting on a network round-trip to find out what
# your own Mission is called would be absurd. It is a heuristic and it will
# sometimes be wrong, which is why the UI offers a rename.

#: Openers people put in front of a goal. Stripped from the left, repeatedly,
#: so "i want to find me the best ..." reduces the same as "find the best ...".
_LEAD_INS = (
    "i want to", "i need to", "i would like to", "i'd like to", "i am trying to",
    "i'm trying to", "can you", "could you", "please", "help me", "help",
    "find me", "find", "search for", "search", "look for", "look up", "look",
    "show me", "show", "get me", "get", "tell me", "tell", "give me", "give",
    "research", "compare", "buy", "book", "plan", "figure out", "work out",
    "what is", "what are", "what's", "which is", "which are", "how do i",
    "how can i", "how to", "where can i", "where is", "where are", "who is",
    "the best", "best", "some", "a good", "good", "me", "the", "a", "an",
    "for", "about", "on",
)

# Where a goal stops being *what* and starts being *which*. Qualifiers do not
# belong in a two-word title, but cutting at the first one is too eager: "plan a
# trip to Japan" would become "Trip". So they come in two strengths.

#: Always ends the title, as long as there is a word already.
_STRONG_QUALIFIERS = {
    "under", "over", "below", "above", "than", "with", "without", "near",
    "around", "that", "which", "who", "before", "after", "between", "from",
    "per", "cheaper", "costing",
}

#: Ends the title only once it has two words, so short goals keep their object.
_WEAK_QUALIFIERS = {"for", "in", "on", "at", "by", "to", "up"}

_WORD = re.compile(r"[a-zA-Z0-9$£€'+&.-]+")

#: Words never worth capitalising into a title on their own.
_STOPWORDS = {"the", "a", "an", "of", "and", "or", "my", "our", "your", "is",
              "are", "was", "were", "be", "it", "this", "that", "these", "those"}

MAX_TITLE_WORDS = 4
MAX_TITLE_CHARS = 32


def title_from_goal(goal: str, *, fallback: str = "New Mission") -> str:
    """A short, human title for a goal, derived locally.

    Deliberately conservative: when the heuristic cannot find anything solid it
    returns a trimmed prefix of the goal rather than inventing something. A
    slightly dull title the user can rename beats a confident wrong one.
    """
    text = " ".join((goal or "").split()).strip()
    if not text:
        return fallback

    lowered = text.lower()
    # Strip lead-ins from the left until nothing matches. Longest first, so
    # "the best" wins over "the". Whitespace is already normalised, so the
    # offset into the lowercased copy is also the offset into the original -
    # which is how "GPU" survives as an acronym instead of becoming "Gpu".
    cut = 0
    changed = True
    while changed and cut < len(lowered):
        changed = False
        for opener in sorted(_LEAD_INS, key=len, reverse=True):
            if lowered.startswith(opener + " ", cut):
                cut += len(opener) + 1
                changed = True
                break
    lowered = lowered[cut:]
    original = _WORD.findall(text[cut:])

    # Cut at the first qualifier: "tennis shoes under $140 for hard courts"
    # becomes "tennis shoes".
    words = _WORD.findall(lowered)
    kept: list[str] = []
    for index, word in enumerate(words):
        if kept and (word in _STRONG_QUALIFIERS
                     or (word in _WEAK_QUALIFIERS and len(kept) >= 2)):
            break
        if word in _STOPWORDS and not kept:
            continue
        kept.append(original[index] if index < len(original) else word)
        if len(kept) >= MAX_TITLE_WORDS:
            break

    while kept and kept[-1].lower() in _STOPWORDS:
        kept.pop()
    if not kept:
        return _clip(text.strip(), MAX_TITLE_CHARS) or fallback

    title = " ".join(_cap(word, first=index == 0) for index, word in enumerate(kept))
    return _clip(title, MAX_TITLE_CHARS) or fallback


#: Not capitalised mid-title. "Laptops for Video Editing", not "Laptops For".
_SMALL_WORDS = _WEAK_QUALIFIERS | _STOPWORDS | {"of", "and", "or", "vs"}


def _cap(word: str, *, first: bool) -> str:
    if word.isupper():          # an acronym the user typed: GPU, USB-C
        return word
    if not first and word.lower() in _SMALL_WORDS:
        return word.lower()
    return word.capitalize()


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary where one is close."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-")


def clean_title(title: str) -> str:
    return " ".join((title or "").split())[:MAX_TITLE]


def clean_goal(goal: str) -> str:
    return " ".join((goal or "").split())[:MAX_GOAL]


def relative_age(stamp: str, *, now_at: datetime | None = None) -> str:
    """How long ago, in words. Empty when the timestamp cannot be read.

    A finding recorded last month and one recorded a minute ago look identical
    without this, which matters because Py is told to verify anything before
    acting on it and cannot judge staleness it cannot see.
    """
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = ((now_at or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'' if int(hours) == 1 else 's'} ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} day{'' if int(days) == 1 else 's'} ago"
    weeks = days / 7
    if weeks < 5:
        return f"{int(weeks)} week{'' if int(weeks) == 1 else 's'} ago"
    months = days / 30.4
    if months < 12:
        return f"{int(months)} month{'' if int(months) == 1 else 's'} ago"
    years = days / 365.25
    return f"{int(years)} year{'' if int(years) == 1 else 's'} ago"


def collapse(text: str) -> str:
    """Normalise whitespace and nothing else.

    Deliberately not clean_title(): that one truncates, and a truncating
    normaliser in front of a length check turns "refuse what is too long" into
    "silently store a shortened version", which is the failure this whole
    limit exists to prevent.
    """
    return " ".join((text or "").split())


def clean_finding(text: str) -> str:
    """Normalise a finding's whitespace. Does NOT shorten it - see the note on
    MAX_FINDING_CHARS. Length is the caller's to check and refuse."""
    return " ".join((text or "").split())


def clean_result(text: str) -> str:
    """Normalise a mission result's edges only - unlike collapse(), its
    internal line breaks survive.

    A result can be a comparison table or a bulleted list (see
    missions_page.renderResultBody), and collapsing every whitespace run to
    one space - the right rule for a single-line finding - would destroy the
    very structure this exists to carry. Only the whole text's leading and
    trailing whitespace is trimmed, plus any trailing whitespace left on an
    individual line; nothing in between is touched.
    """
    stripped = (text or "").strip()
    return "\n".join(line.rstrip() for line in stripped.splitlines())

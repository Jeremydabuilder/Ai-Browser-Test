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
    #: The mission_pages row this came from, or None if the page has since been
    #: forgotten. Losing a source costs the attribution, never the discovery.
    page_id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    #: Filled in by the store when it reads the joined page row.
    source_url: str = ""
    source_title: str = ""

    @property
    def age(self) -> str:
        return relative_age(self.created_at)

    @property
    def source_domain(self) -> str:
        if not self.source_url:
            return ""
        return MissionPage(id=0, mission_id=self.mission_id,
                           url=self.source_url).domain


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

    @property
    def missing(self) -> bool:
        """The finding this cites has been deleted."""
        return self.finding_id is None or self.current_text is None

    @property
    def changed(self) -> bool:
        """The finding still exists but no longer says what it said."""
        return self.current_text is not None and self.current_text != self.text


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

    @property
    def live(self) -> bool:
        return not self.superseded_at


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

"""What the agent is told about the Mission the user is working on.

Two kinds of text meet here and must not be confused, which is the whole
reason this is its own module rather than a few lines in the service:

* **The goal** is the user's own words. It belongs at user authority, plainly,
  outside any fence.
* **The findings** are model-authored notes *about* untrusted web pages. They
  are a record, not a request. They go inside an explicit
  ``<mission_findings>`` fence, whose meaning is defined once in the system
  prompt at developer authority - never here, where an attacker-influenced
  finding could try to redefine it.

Nothing in this module imports from ``app.agent``. The dependency runs one way:
the prompt names the marker this module owns.
"""

from __future__ import annotations

from app.missions.model import (
    Mission,
    MissionChallenge,
    MissionDecision,
    MissionFinding,
)

#: The fence. Everything board-derived goes inside it - the findings, their
#: source domains, and the line saying how many were left out. A reader that
#: trusts the fence must be able to trust that nothing from the board escaped
#: it, so "outside the fence" means "written by us or by the user", with no
#: exceptions for metadata that merely looks harmless.
FINDINGS_OPEN = "<mission_findings>"
FINDINGS_CLOSE = "</mission_findings>"

#: The decision fence. A sibling of the findings fence rather than the same
#: one: what was decided is a different kind of statement from what was
#: observed, and the model should be able to tell them apart. Same rule for
#: both - a record, never an instruction, never permission.
DECISION_OPEN = "<mission_decision>"
DECISION_CLOSE = "</mission_decision>"

#: Findings carried into a resumed conversation, most recent first. A resumed
#: Mission is picking up a thread, and the recent end of the board is the
#: thread.
MAX_BRIEFED_FINDINGS = 25

#: Characters of board-derived text, whichever binds first with the count
#: above. Bounds the worst case at roughly a thousand tokens, paid once per
#: activation rather than per turn.
MAX_BRIEFING_CHARS = 4000

#: Markers a finding must not be able to forge. The closing marker would end
#: the fence early and promote the rest to plain conversation; the untrusted
#: markers would let a finding fake the start or end of page content later on.
_FORGEABLE = (FINDINGS_OPEN, FINDINGS_CLOSE, DECISION_OPEN, DECISION_CLOSE,
              "<untrusted_web_page_content>", "</untrusted_web_page_content>")


def neutralise(text: str) -> str:
    """Defang any fence marker inside board-derived text.

    Same reasoning as ``tools.wrap_untrusted``: marking a boundary is only
    worth anything if the content cannot draw its own boundary.
    """
    for marker in _FORGEABLE:
        text = text.replace(marker, marker.replace("<", "&lt;").replace(">", "&gt;"))
    return text


def _line(finding: MissionFinding, verdict: str = "") -> str:
    """One finding, with where it came from and how old it is.

    The age is here because the prompt tells Py to verify anything before
    acting on it consequentially, and it cannot judge staleness it cannot see:
    a price recorded an hour ago and one recorded last month read identically
    without it.
    """
    text = " ".join(finding.text.split())
    # The mission-local reference goes first, because it is how the model
    # cites this finding when recording a decision - and without it a resumed
    # mission's evidence is unciteable.
    label = f"[{finding.label}] " if finding.label else ""
    # A verdict rides in the existing fence as one word rather than earning a
    # third marker. "CONTRADICTED" beside a note is the whole signal that
    # matters, and a briefing with three kinds of block in it stops being read.
    marks = [mark for mark in (finding.source_domain, finding.age, verdict) if mark]
    return (f"- {label}{text}  ({', '.join(marks)})" if marks
            else f"- {label}{text}")


def _verdicts(mission: Mission | None) -> dict[int, str]:
    """finding id -> the verdict of its live challenge, upper-cased."""
    if mission is None:
        return {}
    return {c.target_id: c.verdict_label for c in mission.challenges
            if c.target_kind == "finding"}


def selected(findings) -> tuple[list[MissionFinding], int]:
    """The findings to carry, most recent first, and how many were left out.

    Two caps rather than one: a count, so the block stays readable, and a
    character budget, so a board of unusually long findings is bounded by a
    rule instead of by luck.
    """
    ordered = list(reversed(list(findings)))
    kept: list[MissionFinding] = []
    used = 0
    for finding in ordered[:MAX_BRIEFED_FINDINGS]:
        cost = len(_line(finding)) + 1
        if kept and used + cost > MAX_BRIEFING_CHARS:
            break
        kept.append(finding)
        used += cost
    return kept, len(ordered) - len(kept)


def findings_block(findings, verdicts: dict[int, str] | None = None) -> str:
    """The fenced record, or "" when there is nothing to record.

    An empty fence would be noise in every fresh Mission's first request and
    would teach the model that the marker often means nothing.
    """
    kept, omitted = selected(findings)
    if not kept:
        return ""
    verdicts = verdicts or {}
    lines = [neutralise(_line(finding, verdicts.get(finding.id, ""))) for finding in kept]
    if omitted:
        lines.append(neutralise(f"({omitted} earlier findings not shown)"))
    body = "\n".join(lines)
    return f"{FINDINGS_OPEN}\n{body}\n{FINDINGS_CLOSE}"


def decision_block(decision: MissionDecision | None,
                   challenge: MissionChallenge | None = None) -> str:
    """The fenced record of what was decided, or "".

    Evidence snapshots are deliberately left out: they are the same sentences
    as the findings already in the briefing, and sending both pays twice for
    one set of facts.
    """
    if decision is None:
        return ""
    lines = [f"Decided: {decision.decision}", f"Because: {decision.rationale}"]
    cited = [evidence.label for evidence in decision.evidence if evidence.label]
    if cited:
        lines.append("Supported by: " + ", ".join(cited))
    for assumption in decision.assumptions:
        lines.append(f"Assuming: {assumption.text}")
    for alternative in decision.alternatives:
        lines.append(f"Not chosen: {alternative.name} - {alternative.reason}")
    if challenge is not None:
        lines.append(f"Challenged: {challenge.verdict_label} - {challenge.summary}")
    if decision.created_at:
        from app.missions.model import relative_age

        age = relative_age(decision.created_at)
        if age:
            lines.append(f"(decided {age})")
    body = "\n".join(neutralise(line) for line in lines)
    return f"{DECISION_OPEN}\n{body}\n{DECISION_CLOSE}"


def compose(mission: Mission | None) -> str:
    """The whole briefing for one Mission, or "" when none is active."""
    if mission is None:
        return ""
    parts = [f'I am working on a mission called "{mission.title}". '
             f"My goal: {mission.goal}"]
    decided = decision_block(
        mission.decision,
        mission.challenge_of("decision", mission.decision.id)
        if mission.decision is not None else None)
    if decided:
        parts.append("What was decided on this mission before:\n" + decided)
    block = findings_block(mission.findings, _verdicts(mission))
    if block:
        parts.append("Notes I recorded earlier on this mission:\n" + block)
    parts.append("Keep this goal in mind for the requests that follow. "
                 "Pages you open or read will be filed under this mission "
                 "automatically, and you can record what you learn with "
                 "mission_save_finding.")
    return "\n\n".join(parts)

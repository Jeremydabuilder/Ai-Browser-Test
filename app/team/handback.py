"""Moving work between a browsing Mission (app.missions) and an AI Team run.

The two systems keep separate stores - a Mission is a lightweight, resumable
research board, a Team run is a planned multi-agent job with its own evidence
and result. They are linked by id only, and this module is the whole of the
link: what a Mission hands to the Team (goal, requirements, notes, pages) and
the bounded summary the Team hands back. Nothing here runs a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.team.model import ArtifactKind, Mission, MissionStatus

#: A Mission's result field is short on purpose (app.missions.model.MAX_RESULT_CHARS).
SUMMARY_LIMIT = 3800

#: Headings the engine appends after the Coordinator's own answer.
_APPENDIX_HEADINGS = (
    "## Sources", "## Web pages (retrieved text)", "## Web search snippets", "## Local knowledge",
    "## Included but not cited", "## Could not be read", "## Generated files", "## Citation check (automatic)",
    "## Success criteria check", "## Optional improvements", "## Notes and limitations")


@dataclass
class Handoff:
    """What a Mission gives the Team panel to start from."""

    goal: str
    requirements: list[str] = field(default_factory=list)
    linked_mission_id: int = 0
    notes: str = ""                                 # findings saved on the Mission
    urls: list[str] = field(default_factory=list)   # pages saved on the Mission


def handoff_from_mission(mission, *, max_notes: int = 12) -> Handoff:
    """``mission`` is an app.missions.model.Mission with pages/findings loaded."""
    notes = ""
    findings = list(getattr(mission, "findings", ()) or ())[:max_notes]
    if findings:
        notes = "Notes saved on the user's browsing Mission (not independently verified):\n" + "\n".join(
            f"- {f.text}" + (f" ({f.source_url})" if getattr(f, "source_url", "") else "") for f in findings)
    urls = []
    for page in getattr(mission, "pages", ()) or ():
        if page.url and page.url not in urls:
            urls.append(page.url)
    return Handoff(goal=mission.goal, requirements=list(getattr(mission, "constraints", ()) or ()),
                   linked_mission_id=mission.id, notes=notes, urls=urls)


def answer_body(final_text: str) -> str:
    """The Coordinator's answer without the appended evidence sections."""
    cut = len(final_text)
    for heading in _APPENDIX_HEADINGS:
        at = final_text.find("\n" + heading)
        if at != -1:
            cut = min(cut, at)
    return final_text[:cut].strip()


def summary_for_mission(team: Mission, limit: int = SUMMARY_LIMIT) -> str | None:
    """A bounded summary suitable for a Mission's result, or None when the run
    has no result worth saving (not finished, or no final answer)."""
    if team.status not in (MissionStatus.COMPLETED, MissionStatus.COMPLETED_WITH_ISSUES):
        return None
    final = team.artifact(team.final_artifact_id) if team.final_artifact_id else None
    if final is None or final.kind != ArtifactKind.FINAL or not final.content.strip():
        return None
    body = answer_body(final.content)
    if not body:
        return None
    lines = [body]
    if team.requirements and team.criteria_check:
        by_text = {c.get("criterion"): c for c in team.criteria_check}
        checks = [f"- {'Met' if by_text[r].get('met') else 'NOT met'}: {r}" for r in team.requirements if r in by_text]
        if checks:
            lines += ["", "Your requirements (reviewer's check against the evidence):", *checks]
    foot = [f"Written by AI Team run #{team.id}"
            + (" - finished with open issues" if team.status == MissionStatus.COMPLETED_WITH_ISSUES else "")
            + ". The sources, citation check and unresolved questions are in that run (AI Team > History); "
              "the citation check only compares wording and figures with the saved text and is not proof."]
    text = "\n".join(lines)
    tail = "\n\n" + foot[0]
    if len(text) + len(tail) > limit:
        text = text[: max(0, limit - len(tail) - 1)].rstrip() + "…"
    return text + tail

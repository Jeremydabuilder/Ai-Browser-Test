"""What the Results workspace shows, as plain functions of a ``Mission``.

Kept free of widgets so the rules are testable: which artifacts are *current* (a replaced
version never appears as the answer, but stays in History), what the answer banner may claim
(only facts present in the mission), how a revision differs from the one it replaced, and how
follow-up conversations and exports are rendered.
"""

from __future__ import annotations

import difflib
import html
import re
import time

from app.team.model import (
    AgentId, Artifact, ArtifactKind, Mission, MissionStatus, SourceKind, TaskStatus,
)

SOURCES_KEY = "__sources__"
VIEWS = (("answer", "&Answer"), ("sources", "&Sources"), ("files", "&Files"), ("review", "&Review"),
         ("tests", "&Tests"), ("history", "&History"), ("ask", "A&sk"))
EMPTY = {
    "answer": "The final answer appears here when the team finishes.",
    "sources": "No pages, text or files were attached, and none were found.",
    "files": "No files were created for this mission.",
    "review": "The Reviewer has not reviewed anything yet.",
    "tests": "No tests were run for this mission.",
    "history": "No results yet.",
}


def replaced_ids(mission: Mission) -> set[str]:
    """Artifacts superseded by a newer version (explicitly marked, or named by a later artifact's ``replaces``)."""
    ids = {a.replaces for a in mission.artifacts if a.replaces}
    ids |= {a.id for a in mission.artifacts if a.meta.get("replaced")}
    return ids


def is_replaced(mission: Mission, artifact: Artifact) -> bool:
    return artifact.id in replaced_ids(mission)


def current(mission: Mission, kind: str) -> list[Artifact]:
    gone = replaced_ids(mission)
    return [a for a in mission.artifacts if a.kind == kind and a.id not in gone]


def predecessor(mission: Mission, artifact: Artifact) -> Artifact | None:
    return mission.artifact(artifact.replaces) if artifact.replaces else None


def answer_artifact(mission: Mission) -> tuple[Artifact | None, bool]:
    """(artifact to show as the answer, is_final). Before the final exists, the newest current
    draft is offered but flagged as not final."""
    if mission.final_artifact_id:
        final = mission.artifact(mission.final_artifact_id)
        if final is not None:
            return final, True
    for kind in (ArtifactKind.REPORT, ArtifactKind.NOTES):
        drafts = current(mission, kind)
        if drafts:
            return drafts[-1], False
    return None, False


def _label(mission: Mission, artifact: Artifact, gone: set[str]) -> str:
    who = AgentId.LABELS.get(artifact.agent, artifact.agent)
    version = f" v{artifact.version}" if artifact.version > 1 else ""
    tail = " · replaced" if artifact.id in gone else ""
    title = artifact.meta.get("path") or artifact.title
    return f"{artifact.id} · {_elide(title, 30)}{version} · {who}{tail}"


def _elide(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


def view_items(mission: Mission, view: str) -> list[tuple[str, str]]:
    """(label, key) choices inside one view."""
    gone = replaced_ids(mission)
    if view == "answer":
        artifact, final = answer_artifact(mission)
        return [("Final result" if final else f"Draft ({artifact.id}) - not final", artifact.id)] if artifact else []
    if view == "sources":
        return [("Sources and pages", SOURCES_KEY)] if mission.sources else []
    if view == "files":
        return [(_label(mission, a, gone), a.id) for a in current(mission, ArtifactKind.FILE)]
    if view == "review":
        reviews = [a for a in mission.artifacts if a.kind == ArtifactKind.REVIEW]
        return [(f"Round {i} review ({a.id})" + (" · latest" if a.id not in gone else " · replaced"), a.id)
                for i, a in reversed(list(enumerate(reviews, start=1)))]
    if view == "tests":
        return [(_label(mission, a, gone), a.id) for a in current(mission, ArtifactKind.TEST_REPORT)]
    if view == "history":
        return [(_label(mission, a, gone), a.id) for a in reversed(mission.artifacts)]
    return []


def counts(mission: Mission) -> dict[str, int]:
    return {"sources": len(mission.sources), "files": len(current(mission, ArtifactKind.FILE)),
            "review": len([a for a in mission.artifacts if a.kind == ArtifactKind.REVIEW]),
            "tests": len(current(mission, ArtifactKind.TEST_REPORT)), "history": len(mission.artifacts),
            "ask": len(mission.followups)}


def empty_text(mission: Mission, view: str) -> str:
    if view == "tests":
        why = next((x.replace("[capability] ", "") for x in mission.limitations if "Tests were not run" in x), "")
        if why:
            return why
        if not any(t.agent == AgentId.TESTER for t in mission.tasks):
            return "No tests were run: this mission had no code to check."
    if view == "files" and mission.tasks and not any(t.agent == AgentId.CODER for t in mission.tasks):
        return "No files were created: this mission did not involve code."
    if view == "review" and mission.tasks and not any(t.agent == AgentId.REVIEWER for t in mission.tasks):
        return "This mission had no review step."
    if view == "sources" and mission.status in MissionStatus.ACTIVE:
        return "Sources appear here as the team reads or finds them."
    return EMPTY.get(view, "")


def answer_banner(mission: Mission) -> str:
    """One factual line (plus caveats) about the answer on screen. Never states more than the data supports."""
    artifact, final = answer_artifact(mission)
    if artifact is None:
        return ""
    if not final:
        return "Draft - the team is still working. This is the newest draft, not the final answer."
    bits = []
    cited = artifact.meta.get("cited") or []
    bits.append(f"{len(cited)} cited source{'s' if len(cited) != 1 else ''}" if cited else "no cited sources")
    if artifact.version > 1:
        bits.append(f"version {artifact.version} (revised after a follow-up)")
    if mission.criteria_check:
        met = sum(1 for c in mission.criteria_check if c.get("met"))
        bits.append(f"Reviewer: {met} of {len(mission.criteria_check)} criteria met")
    elif any(t.agent == AgentId.REVIEWER and t.status == TaskStatus.DONE for t in mission.tasks):
        bits.append("reviewed")
    else:
        bits.append("not reviewed")
    tests = current(mission, ArtifactKind.TEST_REPORT)
    if tests:
        bits.append("tests passed" if all(t.meta.get("passed") for t in tests) else "TESTS FAILED")
    if mission.unresolved_issues:
        bits.append(f"{len(mission.unresolved_issues)} open review issue(s)")
    skipped = [t for t in mission.tasks if t.status == TaskStatus.SKIPPED and t.error == "Skipped by you."]
    if skipped:
        bits.append(f"{len(skipped)} task(s) skipped by you")
    return " · ".join(bits)


def diff_markdown(old: Artifact, new: Artifact) -> str:
    """What changed from ``old`` to ``new``, as a fenced unified diff (line based)."""
    lines = list(difflib.unified_diff(old.content.splitlines(), new.content.splitlines(),
                                      f"{old.id} (replaced)", f"{new.id} (current)", lineterm="", n=2))
    if not lines:
        return f"**No text changes** between {old.id} and {new.id}."
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return (f"**Changes from {old.id} to {new.id}:** {added} line(s) added, {removed} removed.\n\n"
            "```diff\n" + "\n".join(lines) + "\n```")


_MODE_TAG = {"answer": "Ask", "rewrite": "Rewrite", "research": "New research"}


def followups_markdown(mission: Mission) -> str:
    if not mission.followups:
        return ""
    out = []
    for item in mission.followups:
        when = time.strftime("%H:%M", time.localtime(item.get("ts", 0)))
        out.append(f"**You · {_MODE_TAG.get(item.get('mode'), 'Ask')} · {when}**\n\n{item['question']}\n")
        out.append(f"> *{item.get('basis', '')}* ({item.get('calls', 0)} model call(s))\n")
        out.append(item.get("answer", "") + "\n")
        if item.get("needs_research"):
            out.append("*Not covered by the existing evidence. Choose* **Research more** *to look it up.*\n")
        out.append("---\n")
    return "\n".join(out)


def export_name(mission: Mission, artifact: Artifact, extension: str) -> str:
    base = artifact.meta.get("path") or ("final-result" if artifact.kind == ArtifactKind.FINAL else
                                         re.sub(r"[^A-Za-z0-9._-]+", "-", artifact.title).strip("-").lower() or "result")
    base = base.rsplit(".", 1)[0] if base.endswith((".md", ".txt")) else base
    slug = re.sub(r"[^A-Za-z0-9]+", "-", mission.goal).strip("-").lower()[:30]
    return f"{slug + '-' if slug else ''}{base}.{extension}"


def html_document(title: str, body_html: str) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width, initial-scale=1'><title>" + html.escape(title) + "</title><style>"
            "body{font:16px/1.55 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1b1b1f}"
            "pre{background:#f3f3f6;padding:.75rem;overflow:auto}blockquote{color:#555;border-left:3px solid #ccc;"
            "margin-left:0;padding-left:1rem}@media (prefers-color-scheme:dark){body{background:#17171a;color:#e8e8ee}"
            "pre{background:#232329}blockquote{color:#aaa;border-color:#444}a{color:#8ab4ff}}</style></head><body>"
            + body_html + "</body></html>")

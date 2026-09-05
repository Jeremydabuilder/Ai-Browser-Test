"""The Mission Library: `pybrowser://missions/`.

A Mission is not a saved tab group, so it does not live in a 300px panel. It
gets a page - with a URL, back and forward, and room - because everything a
Mission is going to grow into (alternate futures, an evidence structure, a view
of what was believed last Tuesday) needs room, and because a page makes a
Mission a *place* rather than a widget.

The page renders; it never acts. Everything it can ask for goes through an
action URL that the window intercepts and decides on - see
app/browser/internal.py. Nothing here touches SQLite; the window supplies a
provider.

**Untrusted text.** Mission titles and goals are the user's own words, but
finding text is written by the model about web pages, and page titles come from
the web directly. This is a privileged page. So: the payload is JSON with "<"
escaped against a `</script>` break-out, and every value reaches the DOM through
textContent, never innerHTML. A finding titled `<img onerror=...>` is displayed,
not executed. That is the same discipline the new-tab page documents, for the
same reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.browser.internal import route

HOST = "missions"
LIBRARY_URL = "pybrowser://missions/"


def mission_url(mission_id: int) -> str:
    return f"{LIBRARY_URL}{int(mission_id)}"


def evidence_url(mission_id: int) -> str:
    return f"{LIBRARY_URL}{int(mission_id)}/evidence"


@dataclass
class LibraryData:
    """Everything the page shows, already flattened for JSON."""

    missions: list[dict[str, Any]] = field(default_factory=list)
    #: The Mission being looked at, or None on the list view.
    detail: dict[str, Any] | None = None
    #: The evidence view of that Mission, when that is what was asked for.
    evidence: dict[str, Any] | None = None
    query: str = ""
    total: int = 0

    def to_json(self) -> str:
        payload = json.dumps({
            "missions": self.missions,
            "detail": self.detail,
            "evidence": self.evidence,
            "query": self.query,
            "total": self.total,
        }, ensure_ascii=False)
        # A finding containing "</script>" would otherwise end the block and
        # let arbitrary markup follow. Escaping "<" makes that impossible while
        # remaining valid JSON.
        return payload.replace("<", "\\u003c")


def summarise(mission, *, with_detail: bool = False,
              findings: int | None = None, pages: int | None = None,
              routines=None, children=None, parent=None,
              ghost_runs=None) -> dict[str, Any]:
    """One Mission as the page wants it.

    The counts are passed in for the list view, where Missions are read without
    their contents: `len(mission.findings)` would quietly report zero for every
    row rather than the number the user is looking for.
    """
    row: dict[str, Any] = {
        "id": mission.id,
        "title": mission.title,
        "goal": mission.goal,
        "status": mission.status,
        "updated": mission.updated_at,
        "findings": len(mission.findings) if findings is None else findings,
        "pages": len(mission.pages) if pages is None else pages,
        "branchName": mission.branch_name,
        "progress": mission.progress,
    }
    if with_detail:
        row["result"] = mission.result
        row["followUps"] = list(mission.follow_ups)
        row["actionList"] = [_action(a) for a in mission.actions]
        row["decision"] = _decision(mission.decision)
        if row["decision"] is not None:
            row["decision"]["challenge"] = _challenge(
                mission.challenge_of("decision", mission.decision.id))
        row["findingList"] = [
            {"id": f.id, "text": f.text, "source": f.source_domain,
             "url": f.source_url, "age": f.age,
             "challenge": _challenge(mission.challenge_of("finding", f.id))}
            for f in mission.findings
        ]
        row["pageList"] = [
            {"id": p.id, "title": p.display_title, "domain": p.domain, "url": p.url}
            for p in mission.pages
        ]
        row["routineList"] = [
            {"id": routine.id, "name": routine.name, "steps": len(routine.steps)}
            for routine in (routines or [])
        ]
        row["parent"] = ({"id": parent.id, "title": parent.title}
                         if parent is not None else None)
        row["branches"] = [
            {"id": child.id, "title": child.title, "branchName": child.branch_name,
             "status": child.status}
            for child in (children or [])
        ]
        row["ghostRunList"] = [_ghost_run(g) for g in (ghost_runs or [])]
    return row


def _action(action) -> dict[str, Any]:
    """One recorded action, flattened - see MissionAction."""
    return {
        "id": action.id,
        "description": action.description,
        "toolName": action.tool_name,
        "outcome": action.outcome,
        "pageId": action.page_id,
        "age": action.age,
    }


def _ghost_run(ghost_run) -> dict[str, Any]:
    """A prediction, flattened. Never anything the page could mistake for a
    real outcome - see the note on GhostRun in app/missions/model.py."""
    return {
        "id": ghost_run.id,
        "option": ghost_run.option,
        "confidence": ghost_run.confidence,
        "confidenceLabel": ghost_run.confidence_label,
        "effects": [
            {"text": e.text, "kind": e.kind, "glyph": e.glyph}
            for e in ghost_run.effects
        ],
    }


def _decision(decision) -> dict[str, Any] | None:
    """The live decision, flattened.

    Evidence carries both what it said when the decision was made and whether
    the finding it came from has since changed or gone. Showing only one of
    those would be a confident answer to the wrong question.
    """
    if decision is None:
        return None
    from app.missions.model import relative_age

    return {
        "id": decision.id,
        "decision": decision.decision,
        "rationale": decision.rationale,
        "age": relative_age(decision.created_at),
        "evidence": [
            {"text": e.text, "source": e.source,
             "changed": e.changed, "missing": e.missing,
             "current": e.current_text or ""}
            for e in decision.evidence
        ],
        "alternatives": [
            {"name": a.name, "reason": a.reason} for a in decision.alternatives
        ],
    }


def _challenge(challenge) -> dict[str, Any] | None:
    """A challenge, flattened, grouped by the kind of problem found."""
    if challenge is None:
        return None
    from app.missions.model import PointKind, relative_age

    groups = []
    for kind in PointKind.ALL:
        points = challenge.points_of(kind)
        if points:
            groups.append({
                "label": PointKind.LABELS[kind],
                "points": [{"text": p.text, "source": p.source_domain}
                           for p in points],
            })
    return {
        "id": challenge.id,
        "verdict": challenge.verdict,
        "label": challenge.verdict_label,
        "summary": challenge.summary,
        "age": relative_age(challenge.created_at),
        "groups": groups,
    }


def evidence_map(mission) -> dict[str, Any]:
    """The Mission as an evidence structure.

    A projection, not a store: every row here already exists in
    mission_findings, decision_evidence, mission_challenges and
    challenge_points. Nothing is computed and kept - the decision's status in
    particular is read from the evidence each time, so it cannot go stale.
    """
    from app.missions.model import EvidenceState, relative_age

    def source_of(url: str, title: str) -> dict[str, str]:
        return {"url": url, "title": title}

    roots: list[dict[str, Any]] = []
    decision = mission.decision
    if decision is not None:
        challenge = mission.challenge_of("decision", decision.id)
        roots.append({
            "kind": "decision",
            "id": decision.id,
            "label": "D",
            "claim": decision.decision,
            "detail": decision.rationale,
            "status": decision.status,
            "statusLabel": decision.status_label,
            "age": relative_age(decision.created_at),
            "supported": [
                {"label": evidence.label, "text": evidence.text,
                 "state": evidence.state, "glyph": evidence.glyph,
                 "note": evidence.note, "source": evidence.source,
                 "current": evidence.current_text or ""}
                for evidence in decision.evidence
            ],
            "assumptions": [a.text for a in decision.assumptions],
            "challenge": _challenge(challenge),
        })

    for finding in mission.findings:
        challenge = mission.challenge_of("finding", finding.id)
        roots.append({
            "kind": "finding",
            "id": finding.id,
            "label": finding.label,
            "claim": finding.text,
            "detail": "",
            "status": (challenge.verdict if challenge is not None
                       else EvidenceState.UNCHALLENGED),
            "statusLabel": challenge.verdict_label if challenge is not None else "",
            "age": finding.age,
            "supported": [],
            "assumptions": [],
            "source": source_of(finding.source_url, finding.source_title),
            "challenge": _challenge(challenge),
        })

    return {
        "id": mission.id,
        "title": mission.title,
        "goal": mission.goal,
        "roots": roots,
        "sources": [{"title": p.display_title, "domain": p.domain, "url": p.url}
                    for p in mission.pages],
    }


#: Supplied by the window: ``provider(mission_id, query) -> LibraryData``.
_PROVIDER = None


def set_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


def _serve(url) -> str:
    """Render the list, or one Mission's detail.

    The path is the route: "/" is the library, "/7" is Mission 7.
    """
    from PySide6.QtCore import QUrl, QUrlQuery

    # "/" is the library, "/7" one mission, "/7/evidence" its evidence map.
    parts = [part for part in url.path().split("/") if part]
    mission_id = int(parts[0]) if parts and parts[0].isdigit() else None
    view = parts[1] if len(parts) > 1 else ""
    query = QUrlQuery(url).queryItemValue(
        "q", QUrl.ComponentFormattingOption.FullyDecoded) or ""
    if _PROVIDER is None:
        return render(LibraryData())
    try:
        data = _PROVIDER(mission_id, query, view)
    except Exception:  # noqa: BLE001 - an internal page must not 500
        data = LibraryData()
    return render(data)


route(HOST, _serve)


def render(data: LibraryData, dark: bool | None = None) -> str:
    if dark is None:
        from app.browser.newtab import _browser_is_dark

        dark = _browser_is_dark()
    return (_TEMPLATE
            .replace("__THEME__", ' data-theme="dark"' if dark else ' data-theme="light"')
            .replace("__DATA__", data.to_json()))


# The stylesheet deliberately shares the new-tab page's tokens: the two are the
# same product, and a second palette is how an application starts looking like
# two applications stapled together.
_TEMPLATE = """<!doctype html>
<html lang="en"__THEME__>
<head>
<meta charset="utf-8">
<title>Missions</title>
<style>
  :root {
    --bg: #f4f4f7; --surface: #ffffff; --surface-alt: #eaeaf0; --line: #e0e0e8;
    --text: #17171d; --muted: #65656f; --disabled: #a8a8b4;
    --accent: #3d5afe; --accent-soft: #eeedfc; --danger: #b3261e;
    --shadow: 0 1px 2px rgba(20,20,40,.04), 0 10px 30px rgba(20,20,40,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #141419; --surface: #1e1e25; --surface-alt: #262630; --line: #30303b;
      --text: #eeeef3; --muted: #9797a6; --disabled: #61616e;
      --accent: #8c9cff; --accent-soft: #282740; --danger: #f2b8b5;
      --shadow: 0 1px 2px rgba(0,0,0,.35), 0 10px 30px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --bg: #141419; --surface: #1e1e25; --surface-alt: #262630; --line: #30303b;
    --text: #eeeef3; --muted: #9797a6; --disabled: #61616e;
    --accent: #8c9cff; --accent-soft: #282740; --danger: #f2b8b5;
    --shadow: 0 1px 2px rgba(0,0,0,.35), 0 10px 30px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  main { max-width: 760px; margin: 0 auto; padding: 40px 24px 80px; }
  /* One Mission's detail view is a workspace, not a document: its own
     findings and decision on the left, everything about how it got there
     on the right - so it earns the extra width a single reading column
     does not need. Collapses to one column below the breakpoint rather
     than squeezing a sidebar onto a narrow window. */
  main.wide { max-width: 1040px; }
  .workspace {
    display: grid; grid-template-columns: minmax(0, 1fr) 300px;
    gap: 8px 40px; align-items: start; margin-top: 8px;
  }
  .side h2:first-child { margin-top: 0; }
  @media (max-width: 860px) {
    .workspace { grid-template-columns: 1fr; }
    .side { margin-top: 12px; }
  }
  header { display: flex; align-items: baseline; gap: 16px; margin-bottom: 20px; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -.01em; }
  .count { color: var(--muted); font-size: 13px; }
  #search {
    width: 100%; height: 38px; padding: 0 14px; margin-bottom: 28px;
    background: var(--surface); color: var(--text);
    border: 1px solid var(--line); border-radius: 9px; font: inherit;
  }
  #search:focus { outline: none; border-color: var(--accent); }
  h2 {
    font-size: 11px; font-weight: 600; letter-spacing: .07em; color: var(--disabled);
    margin: 26px 0 8px;
  }
  .mission {
    display: block; width: 100%; text-align: left; cursor: pointer;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px 16px; margin-bottom: 8px; color: inherit;
    font: inherit; transition: border-color .12s, box-shadow .12s;
  }
  .mission:hover { border-color: var(--accent); box-shadow: var(--shadow); }
  .mission .row { display: flex; align-items: baseline; gap: 12px; }
  .mission .name { font-weight: 600; font-size: 15px; }
  .mission .meta { margin-left: auto; color: var(--muted); font-size: 12px;
                   white-space: nowrap; }
  .mission .goal {
    color: var(--muted); margin-top: 3px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .empty { color: var(--muted); padding: 32px 0; }
  .back {
    display: block; background: none; border: none; color: var(--muted);
    cursor: pointer; font: inherit; padding: 0; margin-bottom: 8px;
  }
  .back:last-of-type { margin-bottom: 18px; }
  .back:hover { color: var(--accent); }
  .detail-goal { color: var(--muted); margin: 4px 0 22px; font-size: 15px; }
  /* A stage label, not a progress bar - see Mission.progress. Quiet, and
     never claims a precision an open-ended web task does not have. */
  .progress-pill {
    display: inline-block; font-size: 12px; color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border-radius: 999px; padding: 3px 12px; margin: 0 0 18px;
  }
  .result-block {
    font-size: 15px; line-height: 1.5; white-space: pre-wrap;
    border-left: 3px solid var(--success); padding: 2px 0 2px 18px; margin: 4px 0 12px;
  }
  ul.follow-ups { padding-left: 12px; margin: 0 0 26px; }
  ul.follow-ups li { font-size: 13px; color: var(--muted); padding: 2px 0; }
  ul.activity { padding-left: 0; margin: 0 0 20px; list-style: none; }
  ul.activity li {
    display: flex; justify-content: space-between; gap: 12px;
    font-size: 13px; color: var(--muted); padding: 4px 0;
    border-bottom: 1px solid var(--line);
  }
  ul.activity li .t { color: var(--text); }
  ul.activity li .d { color: var(--disabled); font-size: 11px; white-space: nowrap; }
  ul.activity li.activity-failed .t { color: var(--danger); }
  .actions { display: flex; gap: 8px; margin: 0 0 26px; }
  button.act {
    height: 32px; padding: 0 14px; border-radius: 8px; cursor: pointer;
    font: inherit; background: var(--surface); color: var(--text);
    border: 1px solid var(--line);
  }
  button.act:hover { border-color: var(--accent); color: var(--accent); }
  button.act.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.act.primary:hover { color: #fff; opacity: .9; }
  button.act.danger:hover { border-color: var(--danger); color: var(--danger); }
  /* The decision leads the page and does not look like another card in a row
     of cards: it is the answer the mission was for. */
  .decision {
    border-left: 3px solid var(--accent); padding: 2px 0 2px 18px; margin: 4px 0 30px;
  }
  .decision .label {
    font-size: 11px; font-weight: 600; letter-spacing: .07em; color: var(--accent);
  }
  .decision .what { font-size: 21px; font-weight: 600; margin: 4px 0 2px;
                    letter-spacing: -.01em; }
  .decision .why { color: var(--muted); font-size: 15px; margin-bottom: 16px; }
  .decision .when { color: var(--disabled); font-size: 12px; float: right; }
  .decision .cols { display: flex; gap: 40px; flex-wrap: wrap; }
  .decision .cols > div { flex: 1 1 240px; min-width: 0; }
  .decision h3 {
    font-size: 11px; font-weight: 600; letter-spacing: .07em; color: var(--disabled);
    margin: 0 0 6px;
  }
  .decision li { padding: 3px 0; }
  .decision .src, .decision .why-not { color: var(--muted); font-size: 12px; }
  .decision .flag { color: var(--muted); font-size: 11px; font-style: italic; }
  .decision .acts { margin-top: 18px; }
  /* A challenge sits beneath what it challenges and is unmistakably a second
     opinion rather than a correction of the first. */
  .challenge { margin: 6px 0 14px 12px; padding-left: 14px;
               border-left: 2px solid var(--line); }
  .challenge .verdict { font-size: 10px; font-weight: 700; letter-spacing: .08em; }
  .challenge .verdict.upheld { color: var(--success); }
  .challenge .verdict.weakened { color: var(--warning); }
  .challenge .verdict.contradicted { color: var(--danger); }
  .challenge .verdict.unresolved { color: var(--muted); }
  .challenge .when { color: var(--disabled); font-size: 11px; margin-left: 8px; }
  .challenge .sum { color: var(--text); font-size: 13px; margin: 3px 0 8px; }
  .challenge .grp { display: flex; gap: 12px; margin-bottom: 5px; }
  .challenge .grp .k {
    flex: 0 0 108px; font-size: 10px; font-weight: 600; letter-spacing: .06em;
    color: var(--disabled); padding-top: 2px;
  }
  .challenge .grp .v { flex: 1; min-width: 0; font-size: 13px; }
  .challenge .grp .v .src { color: var(--muted); font-size: 11px; }
  button.link {
    background: none; border: none; padding: 0; cursor: pointer; font: inherit;
    color: var(--muted); font-size: 12px;
  }
  button.link:hover { color: var(--accent); }
  /* The evidence map. A structured list, not a node canvas: what matters is
     that every claim traces to a source and that the state of each piece of
     support is readable at a glance. Draggable circles would cost that. */
  .evsum {
    display: flex; align-items: baseline; gap: 12px; margin: 2px 0 22px;
    color: var(--muted); font-size: 12px;
  }
  .root { margin: 0 0 30px; padding-left: 18px; border-left: 3px solid var(--line); }
  .root.decision { border-left-color: var(--accent); }
  .root .ref {
    font-size: 11px; font-weight: 700; letter-spacing: .06em; color: var(--muted);
  }
  .root .claim { font-size: 17px; font-weight: 600; margin: 2px 0 2px; }
  .root.decision .claim { font-size: 21px; }
  .root .detail { color: var(--muted); margin-bottom: 10px; }
  .status {
    font-size: 10px; font-weight: 700; letter-spacing: .08em; margin-left: 10px;
  }
  .status.sound, .status.upheld { color: var(--success); }
  .status.check, .status.weakened { color: var(--warning); }
  .status\.needs, .status.contradicted { color: var(--danger); }
  .status.needs { color: var(--danger); }
  .status.unresolved { color: var(--muted); }
  .sect {
    font-size: 10px; font-weight: 600; letter-spacing: .07em; color: var(--disabled);
    margin: 12px 0 5px;
  }
  .ev { display: flex; gap: 10px; padding: 3px 0; }
  .ev .g { flex: 0 0 14px; text-align: center; color: var(--muted); }
  .ev .g.contradicted, .ev .g.missing { color: var(--danger); }
  .ev .g.weakened { color: var(--warning); }
  .ev .g.upheld, .ev .g.unchallenged { color: var(--success); }
  .ev .b { flex: 1; min-width: 0; }
  .ev .b .m { color: var(--muted); font-size: 12px; }
  .ev .b .m .flag { font-style: italic; }
  .ev .r { color: var(--muted); font-size: 11px; font-weight: 700; }
  ul { list-style: none; margin: 0; padding: 0; }
  li.finding { padding: 9px 0 9px 12px; border-left: 2px solid var(--accent);
               margin-bottom: 6px; }
  li.finding .src { color: var(--muted); font-size: 12px; }
  li.page a { display: flex; gap: 12px; padding: 7px 0; color: inherit;
              text-decoration: none; align-items: baseline; }
  .root-row { display: flex; gap: 12px; padding: 7px 0; align-items: baseline; }
  .root-row .t { font-weight: 500; }
  .root-row .d { margin-left: auto; color: var(--muted); font-size: 12px; }
  li.page a:hover .t { color: var(--accent); }
  li.page .d { margin-left: auto; color: var(--muted); font-size: 12px; }
  .tag { font-size: 10px; font-weight: 700; letter-spacing: .08em;
         color: var(--accent); }
  ul.ghost-effects { padding-left: 12px; margin-bottom: 4px; }
  ul.ghost-effects li { font-size: 12px; padding: 2px 0; color: var(--muted); }
  ul.ghost-effects li.ghost-benefit { color: var(--success); }
  ul.ghost-effects li.ghost-risk { color: var(--danger); }
</style>
</head>
<body>
<main>
  <header>
    <h1>Missions</h1>
    <span class="count" id="count"></span>
  </header>
  <input id="search" type="search" placeholder="Search missions, findings and pages…"
         autocomplete="off" spellcheck="false">
  <div id="body"></div>
</main>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var data;
  try {
    data = JSON.parse(document.getElementById("data").textContent);
  } catch (e) {
    data = { missions: [], detail: null, query: "", total: 0 };
  }

  function act(name, params) {
    var url = "pybrowser://missions/action/" + name;
    var parts = [];
    for (var key in params) {
      if (params[key] !== undefined && params[key] !== null && params[key] !== "") {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
      }
    }
    // Navigating is how the page reaches Python: the browser intercepts this
    // URL, refuses the navigation, and acts on it.
    if (parts.length) { url += "?" + parts.join("&"); }
    window.location.href = url;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    // textContent, never innerHTML: findings are written about web pages and
    // page titles come from them, and this is a privileged page.
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  var search = document.getElementById("search");
  search.value = data.query || "";
  search.addEventListener("keydown", function (event) {
    if (event.key === "Enter") { act("search", { q: search.value }); }
  });

  var body = document.getElementById("body");

  function countLabel(mission) {
    var bits = [];
    if (mission.findings) { bits.push(mission.findings + " finding" + (mission.findings === 1 ? "" : "s")); }
    if (mission.pages) { bits.push(mission.pages + " page" + (mission.pages === 1 ? "" : "s")); }
    return bits.join(" \\u00b7 ");
  }

  function missionButton(mission) {
    var card = el("button", "mission");
    var row = el("div", "row");
    row.appendChild(el("span", "name", mission.title));
    if (mission.status === "active") { row.appendChild(el("span", "tag", "ACTIVE")); }
    row.appendChild(el("span", "meta", countLabel(mission)));
    card.appendChild(row);
    card.appendChild(el("div", "goal", mission.goal));
    if (mission.status === "active" && mission.progress) {
      card.appendChild(el("div", "progress-pill", mission.progress));
    }
    card.addEventListener("click", function () { act("open", { id: mission.id }); });
    return card;
  }

  function renderList() {
    var groups = [["active", "ACTIVE"], ["paused", "PAUSED"], ["completed", "COMPLETE"]];
    document.getElementById("count").textContent =
      data.query ? data.missions.length + " of " + data.total : String(data.total);
    if (!data.missions.length) {
      body.appendChild(el("p", "empty", data.query
        ? "Nothing matches \\u201c" + data.query + "\\u201d."
        : "Missions you start with Py will collect here."));
      return;
    }
    groups.forEach(function (group) {
      var rows = data.missions.filter(function (m) { return m.status === group[0]; });
      if (!rows.length) { return; }
      body.appendChild(el("h2", null, group[1]));
      rows.forEach(function (mission) { body.appendChild(missionButton(mission)); });
    });
  }

  function challengeBlock(challenge) {
    if (!challenge) { return null; }
    var box = el("div", "challenge");
    var head = el("div");
    head.appendChild(el("span", "verdict " + challenge.verdict, challenge.label));
    head.appendChild(el("span", "when", challenge.age));
    box.appendChild(head);
    box.appendChild(el("div", "sum", challenge.summary));
    challenge.groups.forEach(function (group) {
      var row = el("div", "grp");
      row.appendChild(el("div", "k", group.label));
      var values = el("div", "v");
      group.points.forEach(function (point) {
        values.appendChild(el("div", null, point.text));
        if (point.source) { values.appendChild(el("div", "src", point.source)); }
      });
      row.appendChild(values);
      box.appendChild(row);
    });
    return box;
  }

  function challengeButton(kind, id, existing) {
    var button = el("button", "link", existing ? "Challenge again" : "Challenge");
    button.title = "Ask Py to try to prove this wrong";
    button.addEventListener("click", function () {
      act("challenge", { kind: kind, target: id });
    });
    return button;
  }

  function renderEvidenceSummary(mission) {
    var counts = { findings: mission.findingList.length, sources: mission.pageList.length,
                   challenged: 0 };
    mission.findingList.forEach(function (f) { if (f.challenge) { counts.challenged += 1; } });
    if (mission.decision && mission.decision.challenge) { counts.challenged += 1; }
    if (!counts.findings && !mission.decision) { return; }

    var row = el("div", "evsum");
    var bits = [counts.findings + " claim" + (counts.findings === 1 ? "" : "s"),
                counts.sources + " source" + (counts.sources === 1 ? "" : "s")];
    if (counts.challenged) { bits.push(counts.challenged + " challenged"); }
    row.appendChild(el("span", "c", bits.join(" \u00b7 ")));
    var open = el("button", "link", "View evidence \u2192");
    open.addEventListener("click", function () { act("evidence", { id: mission.id }); });
    row.appendChild(open);
    body.appendChild(row);
  }

  function renderDecision(mission, target) {
    // Nothing at all when there is none. A mission still being worked on
    // should look like one, not like a form with an empty field.
    var decision = mission.decision;
    if (!decision) { return; }

    var box = el("div", "decision");
    var when = el("span", "when", decision.age);
    box.appendChild(when);
    box.appendChild(el("div", "label", "DECISION"));
    box.appendChild(el("div", "what", decision.decision));
    box.appendChild(el("div", "why", decision.rationale));

    var cols = el("div", "cols");
    if (decision.evidence.length) {
      var left = el("div");
      left.appendChild(el("h3", null, "BECAUSE"));
      var evidence = el("ul");
      decision.evidence.forEach(function (item) {
        var li = el("li");
        li.appendChild(el("div", null, item.text));
        var marks = [];
        if (item.source) { marks.push(item.source); }
        if (item.missing) { marks.push("finding since removed"); }
        else if (item.changed) { marks.push("finding has changed since"); }
        if (marks.length) {
          // The snapshot is what the decision was made on; the flag says the
          // board has moved. Both are true and both are shown.
          li.appendChild(el("div", item.missing || item.changed ? "flag" : "src",
                            marks.join(" \u00b7 ")));
        }
        evidence.appendChild(li);
      });
      left.appendChild(evidence);
      cols.appendChild(left);
    }
    if (decision.alternatives.length) {
      var right = el("div");
      right.appendChild(el("h3", null, "INSTEAD OF"));
      var alts = el("ul");
      decision.alternatives.forEach(function (item) {
        var li = el("li");
        li.appendChild(el("div", null, item.name));
        if (item.reason) { li.appendChild(el("div", "why-not", item.reason)); }
        alts.appendChild(li);
      });
      right.appendChild(alts);
      cols.appendChild(right);
    }
    if (cols.childNodes.length) { box.appendChild(cols); }

    var acts = el("div", "acts");
    var edit = el("button", "act", "Edit");
    edit.addEventListener("click", function () {
      act("edit-decision", { id: mission.id });
    });
    acts.appendChild(edit);
    var clear = el("button", "act danger", "Clear");
    clear.addEventListener("click", function () {
      act("clear-decision", { id: mission.id });
    });
    acts.appendChild(clear);
    var challenge = el("button", "act", decision.challenge ? "Challenge again" : "Challenge");
    challenge.title = "Ask Py to try to prove this wrong";
    challenge.addEventListener("click", function () {
      act("challenge", { kind: "decision", target: decision.id });
    });
    acts.appendChild(challenge);
    box.appendChild(acts);
    target.appendChild(box);

    var verdict = challengeBlock(decision.challenge);
    if (verdict) { target.appendChild(verdict); }
  }

  function renderDetail(mission) {
    document.querySelector("main").classList.add("wide");

    var back = el("button", "back", "\u2190 All missions");
    back.addEventListener("click", function () { act("library", {}); });
    body.appendChild(back);

    if (mission.parent) {
      var lineage = el("button", "link", "Branched from " + mission.parent.title);
      lineage.addEventListener("click", function () {
        act("open", { id: mission.parent.id });
      });
      body.appendChild(lineage);
    }

    var head = el("div", "row");
    head.appendChild(el("h1", null, mission.title));
    if (mission.branchName) { head.appendChild(el("span", "tag", mission.branchName)); }
    body.appendChild(head);
    body.appendChild(el("div", "detail-goal", mission.goal));
    if (mission.progress) {
      body.appendChild(el("div", "progress-pill", mission.progress));
    }

    var actions = el("div", "actions");
    var resume = el("button", "act primary",
                    mission.status === "active" ? "Go to mission" : "Resume");
    resume.addEventListener("click", function () { act("resume", { id: mission.id }); });
    actions.appendChild(resume);
    var rename = el("button", "act", "Rename");
    rename.addEventListener("click", function () { act("rename", { id: mission.id }); });
    actions.appendChild(rename);
    var remove = el("button", "act danger", "Delete");
    remove.addEventListener("click", function () { act("delete", { id: mission.id }); });
    actions.appendChild(remove);
    var branch = el("button", "act", "Branch this mission");
    branch.title = "Fork an independent copy - its own findings, its own decision";
    branch.addEventListener("click", function () { act("branch", { id: mission.id }); });
    actions.appendChild(branch);
    body.appendChild(actions);

    // Two columns from here: the mission's own substance on the left - what
    // it found and decided - and everything about *how it got there* on the
    // right, the same split as a document and its margin notes. Collapses
    // to one column below the workspace breakpoint (see the media query).
    var workspace = el("div", "workspace");
    var main = el("div", "main-col");
    var side = el("div", "side");
    workspace.appendChild(main);
    workspace.appendChild(side);
    body.appendChild(workspace);

    if (mission.result) {
      main.appendChild(el("h2", null, "RESULT"));
      main.appendChild(el("div", "result-block", mission.result));
      if (mission.followUps && mission.followUps.length) {
        var followUps = el("ul", "follow-ups");
        mission.followUps.forEach(function (item) {
          followUps.appendChild(el("li", null, item));
        });
        main.appendChild(followUps);
      }
    }

    renderDecision(mission, main);

    main.appendChild(el("h2", null, "FINDINGS \u00b7 " + mission.findingList.length));
    if (!mission.findingList.length) {
      main.appendChild(el("p", "empty", "Nothing recorded for this mission yet."));
    } else {
      var found = el("ul");
      mission.findingList.forEach(function (finding) {
        var li = el("li", "finding");
        li.appendChild(el("div", null, finding.text));
        var marks = [finding.source, finding.age].filter(Boolean);
        var meta = el("div", "src", marks.join(" \u00b7 "));
        if (marks.length) { meta.appendChild(document.createTextNode(" \u00b7 ")); }
        meta.appendChild(challengeButton("finding", finding.id, finding.challenge));
        li.appendChild(meta);
        var verdict = challengeBlock(finding.challenge);
        if (verdict) { li.appendChild(verdict); }
        found.appendChild(li);
      });
      main.appendChild(found);
    }

    if (mission.actionList && mission.actionList.length) {
      side.appendChild(el("h2", null, "ACTIVITY \u00b7 " + mission.actionList.length));
      var activity = el("ul", "activity");
      mission.actionList.forEach(function (item) {
        var li = el("li", "activity-" + item.outcome);
        li.appendChild(el("span", "t", item.description));
        if (item.age) { li.appendChild(el("span", "d", item.age)); }
        activity.appendChild(li);
      });
      side.appendChild(activity);
    }

    side.appendChild(el("h2", null, "PAGES \u00b7 " + mission.pageList.length));
    var pages = el("ul");
    mission.pageList.forEach(function (page) {
      var li = el("li", "page");
      var link = el("a");
      link.href = "pybrowser://missions/action/page?url=" + encodeURIComponent(page.url);
      link.appendChild(el("span", "t", page.title));
      link.appendChild(el("span", "d", page.domain));
      link.title = page.url;
      li.appendChild(link);
      pages.appendChild(li);
    });
    side.appendChild(pages);

    if (mission.branches && mission.branches.length) {
      side.appendChild(el("h2", null, "BRANCHES \u00b7 " + mission.branches.length));
      var branches = el("ul");
      mission.branches.forEach(function (child) {
        var li = el("li", "page");
        var row = el("div", "root-row");
        row.appendChild(el("span", "t", child.title));
        if (child.status === "active") { row.appendChild(el("span", "tag", "ACTIVE")); }
        var open = el("button", "link", "Open");
        open.addEventListener("click", function () { act("open", { id: child.id }); });
        row.appendChild(open);
        li.appendChild(row);
        branches.appendChild(li);
      });
      side.appendChild(branches);
    }

    if (mission.routineList && mission.routineList.length) {
      side.appendChild(el("h2", null, "ROUTINES \u00b7 " + mission.routineList.length));
      var routines = el("ul");
      mission.routineList.forEach(function (routine) {
        var li = el("li", "page");
        var row = el("div", "root-row");
        row.appendChild(el("span", "t", routine.name));
        row.appendChild(el("span", "d", routine.steps + " step" +
                           (routine.steps === 1 ? "" : "s")));
        var run = el("button", "link", "Run");
        run.addEventListener("click", function () {
          act("routine-run", { id: routine.id });
        });
        row.appendChild(run);
        li.appendChild(row);
        routines.appendChild(li);
      });
      side.appendChild(routines);
    }

    if (mission.ghostRunList && mission.ghostRunList.length) {
      side.appendChild(el("h2", null, "GHOST RUNS \u00b7 " + mission.ghostRunList.length));
      var ghostRuns = el("ul");
      mission.ghostRunList.forEach(function (ghost) {
        var li = el("li", "page");
        var row = el("div", "root-row");
        row.appendChild(el("span", "t", ghost.option));
        row.appendChild(el("span", "tag", ghost.confidenceLabel));
        var clear = el("button", "link", "Clear");
        clear.addEventListener("click", function () {
          act("ghost-run-clear", { id: ghost.id });
        });
        row.appendChild(clear);
        li.appendChild(row);
        if (ghost.effects.length) {
          var effects = el("ul", "ghost-effects");
          ghost.effects.forEach(function (effect) {
            var el2 = el("li", "ghost-" + effect.kind,
                        effect.glyph + " " + effect.text);
            effects.appendChild(el2);
          });
          li.appendChild(effects);
        }
        ghostRuns.appendChild(li);
      });
      side.appendChild(ghostRuns);
    }
    document.getElementById("count").textContent = "";
  }

  function renderEvidence(map) {
    search.style.display = "none";
    var back = el("button", "back", "\u2190 " + map.title);
    back.addEventListener("click", function () { act("open", { id: map.id }); });
    body.appendChild(back);
    body.appendChild(el("h1", null, "Evidence"));
    body.appendChild(el("div", "detail-goal", map.goal));

    if (!map.roots.length) {
      body.appendChild(el("p", "empty",
        "Nothing has been recorded for this mission yet."));
      return;
    }

    map.roots.forEach(function (root) {
      var box = el("div", "root" + (root.kind === "decision" ? " decision" : ""));
      var head = el("div");
      head.appendChild(el("span", "ref", root.label));
      if (root.statusLabel) {
        head.appendChild(el("span", "status " + root.status.split(" ")[0],
                            root.statusLabel));
      }
      box.appendChild(head);
      box.appendChild(el("div", "claim", root.claim));
      if (root.detail) { box.appendChild(el("div", "detail", root.detail)); }

      if (root.supported.length) {
        box.appendChild(el("div", "sect", "SUPPORTED BY"));
        root.supported.forEach(function (item) {
          var row = el("div", "ev");
          row.appendChild(el("div", "g " + item.state, item.glyph));
          var b = el("div", "b");
          var line = el("div");
          if (item.label) { line.appendChild(el("span", "r", item.label + "  ")); }
          line.appendChild(document.createTextNode(item.text));
          b.appendChild(line);
          var marks = [];
          if (item.source) { marks.push(item.source); }
          var meta = el("div", "m", marks.join(" \u00b7 "));
          if (item.note) {
            if (marks.length) { meta.appendChild(document.createTextNode(" \u00b7 ")); }
            meta.appendChild(el("span", "flag", item.note));
          }
          b.appendChild(meta);
          row.appendChild(b);
          box.appendChild(row);
        });
      }

      if (root.challenge) {
        box.appendChild(el("div", "sect", "CHALLENGED BY"));
        box.appendChild(challengeBlock(root.challenge));
      }

      if (root.assumptions.length) {
        box.appendChild(el("div", "sect", "ASSUMPTIONS"));
        root.assumptions.forEach(function (text) {
          var row = el("div", "ev");
          row.appendChild(el("div", "g", "\u00b7"));
          row.appendChild(el("div", "b", text));
          box.appendChild(row);
        });
      }

      if (root.kind === "finding" && root.source && root.source.url) {
        var row = el("div", "ev");
        row.appendChild(el("div", "g", ""));
        var b = el("div", "b");
        var link = el("button", "link", root.source.title || root.source.url);
        link.addEventListener("click", function () {
          act("page", { url: root.source.url });
        });
        b.appendChild(link);
        row.appendChild(b);
        box.appendChild(row);
      }
      body.appendChild(box);
    });

    body.appendChild(el("h2", null, "SOURCES \u00b7 " + map.sources.length));
    var pages = el("ul");
    map.sources.forEach(function (page) {
      var li = el("li", "page");
      var link = el("a");
      link.href = "pybrowser://missions/action/page?url=" + encodeURIComponent(page.url);
      link.appendChild(el("span", "t", page.title));
      link.appendChild(el("span", "d", page.domain));
      li.appendChild(link);
      pages.appendChild(li);
    });
    body.appendChild(pages);
  }

  if (data.evidence) {
    renderEvidence(data.evidence);
  } else if (data.detail) {
    // Searching from inside one mission would mean two different scopes on
    // one screen. The way back to the list is the "All missions" link.
    search.style.display = "none";
    renderDetail(data.detail);
  } else {
    renderList();
  }
}());
</script>
</body>
</html>
"""

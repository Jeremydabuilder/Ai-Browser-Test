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


@dataclass
class LibraryData:
    """Everything the page shows, already flattened for JSON."""

    missions: list[dict[str, Any]] = field(default_factory=list)
    #: The Mission being looked at, or None on the list view.
    detail: dict[str, Any] | None = None
    query: str = ""
    total: int = 0

    def to_json(self) -> str:
        payload = json.dumps({
            "missions": self.missions,
            "detail": self.detail,
            "query": self.query,
            "total": self.total,
        }, ensure_ascii=False)
        # A finding containing "</script>" would otherwise end the block and
        # let arbitrary markup follow. Escaping "<" makes that impossible while
        # remaining valid JSON.
        return payload.replace("<", "\\u003c")


def summarise(mission, *, with_detail: bool = False,
              findings: int | None = None, pages: int | None = None) -> dict[str, Any]:
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
    }
    if with_detail:
        row["decision"] = _decision(mission.decision)
        row["findingList"] = [
            {"id": f.id, "text": f.text, "source": f.source_domain,
             "url": f.source_url, "age": f.age}
            for f in mission.findings
        ]
        row["pageList"] = [
            {"id": p.id, "title": p.display_title, "domain": p.domain, "url": p.url}
            for p in mission.pages
        ]
    return row


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

    identifier = url.path().strip("/")
    mission_id = int(identifier) if identifier.isdigit() else None
    query = QUrlQuery(url).queryItemValue(
        "q", QUrl.ComponentFormattingOption.FullyDecoded) or ""
    if _PROVIDER is None:
        return render(LibraryData())
    try:
        data = _PROVIDER(mission_id, query)
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
    background: none; border: none; color: var(--muted); cursor: pointer;
    font: inherit; padding: 0; margin-bottom: 18px;
  }
  .back:hover { color: var(--accent); }
  .detail-goal { color: var(--muted); margin: 4px 0 22px; font-size: 15px; }
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
  ul { list-style: none; margin: 0; padding: 0; }
  li.finding { padding: 9px 0 9px 12px; border-left: 2px solid var(--accent);
               margin-bottom: 6px; }
  li.finding .src { color: var(--muted); font-size: 12px; }
  li.page a { display: flex; gap: 12px; padding: 7px 0; color: inherit;
              text-decoration: none; align-items: baseline; }
  li.page a:hover .t { color: var(--accent); }
  li.page .d { margin-left: auto; color: var(--muted); font-size: 12px; }
  .tag { font-size: 10px; font-weight: 700; letter-spacing: .08em;
         color: var(--accent); }
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

  function renderDecision(mission) {
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
    box.appendChild(acts);
    body.appendChild(box);
  }

  function renderDetail(mission) {
    var back = el("button", "back", "\\u2190 All missions");
    back.addEventListener("click", function () { act("library", {}); });
    body.appendChild(back);

    var head = el("div", "row");
    head.appendChild(el("h1", null, mission.title));
    body.appendChild(head);
    body.appendChild(el("div", "detail-goal", mission.goal));

    renderDecision(mission);

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
    body.appendChild(actions);

    body.appendChild(el("h2", null, "FINDINGS \\u00b7 " + mission.findingList.length));
    if (!mission.findingList.length) {
      body.appendChild(el("p", "empty", "Nothing recorded for this mission yet."));
    } else {
      var found = el("ul");
      mission.findingList.forEach(function (finding) {
        var li = el("li", "finding");
        li.appendChild(el("div", null, finding.text));
        var marks = [finding.source, finding.age].filter(Boolean);
        if (marks.length) {
          li.appendChild(el("div", "src", marks.join(" \u00b7 ")));
        }
        found.appendChild(li);
      });
      body.appendChild(found);
    }

    body.appendChild(el("h2", null, "PAGES \\u00b7 " + mission.pageList.length));
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
    body.appendChild(pages);
    document.getElementById("count").textContent = "";
  }

  if (data.detail) {
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

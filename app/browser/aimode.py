"""AI Mode's page, served from ``pybrowser://aimode/``.

A conversation UI: ask, watch real progress ("Searching the web...", "Reading example.com..."), get a
cited answer with its sources, follow up. Like the new-tab page it is a real page (a URL, a tab,
back/forward) and it cannot call Python: it navigates to ``pybrowser://aimode/action/<name>`` URLs that
the browser intercepts (``aimode:ask`` ...). Python pushes state back with
``window.PB.setState(<json>)``; the initial state is embedded so a reload restores the conversation.

Untrusted content: the answer HTML is produced by ``app.aimode.markup`` (escape-first, own tags only);
every other string (questions, titles, hosts, snippets, status) is written with ``textContent``.
"""

from __future__ import annotations

import json

from app.browser.internal import is_internal, route

HOST = "aimode"
AIMODE_URL = f"pybrowser://{HOST}/"

#: Callable returning the current page state (a dict); installed by the window.
_PROVIDER = lambda: {"turns": [], "busy": False, "status": "", "providerReady": False,  # noqa: E731
                     "searchReady": False, "searchLabel": ""}


def set_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


def is_ai_mode(url) -> bool:
    from PySide6.QtCore import QUrl

    target = QUrl(url) if isinstance(url, str) else url
    return is_internal(target) and target.host() == HOST


def _state_json() -> str:
    try:
        state = _PROVIDER()
    except Exception:  # noqa: BLE001 - the page must always open
        state = {"turns": [], "busy": False, "status": "", "providerReady": False, "searchReady": False}
    # "<" escaped so a "</script>" in any string cannot end the data block.
    return json.dumps(state, ensure_ascii=False).replace("<", "\\u003c")


def _dark() -> bool:
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui import theme

        return theme.palette_for(QApplication.instance()) is theme.DARK
    except Exception:  # noqa: BLE001
        return False


def render(state_json: str | None = None, dark: bool | None = None) -> str:
    dark = _dark() if dark is None else dark
    return (_TEMPLATE.replace("__THEME__", ' data-theme="dark"' if dark else ' data-theme="light"')
            .replace("__STATE__", state_json if state_json is not None else _state_json()))


def _serve(_url) -> str:
    return render()


route(HOST, _serve)

_TEMPLATE = """<!doctype html>
<html lang="en"__THEME__>
<head>
<meta charset="utf-8">
<title>AI Mode</title>
<style>
  :root {
    --bg: #fafafc; --surface: #ffffff; --surface-alt: #f1f1f6; --line: #e4e4ec; --text: #1a1a22; --muted: #6b6b78;
    --accent: #3d5afe; --accent2: #7b3ff2; --accent-soft: #eef0ff; --danger: #c62828; --danger-soft: #fdecea;
    --warn: #8a5a00; --warn-soft: #fff6e0; --shadow: 0 1px 2px rgba(20,20,40,.05), 0 8px 24px rgba(20,20,40,.06);
    --gradient: linear-gradient(135deg, var(--accent), var(--accent2));
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #16161b; --surface: #1f1f27; --surface-alt: #262630; --line: #30303b; --text: #eeeef3; --muted: #9a9aa8;
      --accent: #8c9cff; --accent2: #b98cff; --accent-soft: #272a45; --danger: #ff8a80; --danger-soft: #3a2224;
      --warn: #ffcc66; --warn-soft: #36301f; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --bg: #16161b; --surface: #1f1f27; --surface-alt: #262630; --line: #30303b; --text: #eeeef3; --muted: #9a9aa8;
    --accent: #8c9cff; --accent2: #b98cff; --accent-soft: #272a45; --danger: #ff8a80; --danger-soft: #3a2224;
    --warn: #ffcc66; --warn-soft: #36301f; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
    font: 15px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 14px 24px; border-bottom: 1px solid var(--line);
    background: var(--bg);
  }
  header .logo { width: 22px; height: 22px; color: var(--accent); }
  header h1 { margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -.01em; }
  header .grow { flex: 1; }
  button { font: inherit; }
  .ghost {
    height: 32px; padding: 0 12px; border-radius: 16px; border: 1px solid var(--line); background: var(--surface);
    color: var(--muted); cursor: pointer; font-size: 13px;
  }
  .ghost:hover { color: var(--accent); border-color: var(--accent); }
  button:focus-visible, a:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #scroll { flex: 1; overflow-y: auto; }
  #thread { max-width: 760px; margin: 0 auto; padding: 24px 24px 12px; }
  .empty { text-align: center; padding: 9vh 0 0; }
  .empty .spark { width: 44px; height: 44px; color: var(--accent); }
  .empty h2 { margin: 12px 0 6px; font-size: 26px; font-weight: 650; letter-spacing: -.02em; }
  .empty p { margin: 0 auto; max-width: 460px; color: var(--muted); }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 22px; }
  .chip {
    padding: 8px 14px; border-radius: 18px; border: 1px solid var(--line); background: var(--surface); color: var(--text);
    cursor: pointer; font-size: 13.5px; text-align: left;
  }
  .chip:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  .banner { margin: 18px 0 0; padding: 10px 14px; border-radius: 12px; font-size: 13.5px; }
  .banner.setup { background: var(--accent-soft); }
  .banner.warn { background: var(--warn-soft); color: var(--warn); }
  .banner button { margin-left: 8px; }
  .turn { margin: 0 0 30px; }
  .q { font-size: 20px; font-weight: 600; letter-spacing: -.015em; margin: 0 0 12px; overflow-wrap: anywhere; }
  .basis { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); margin: 0 0 10px; }
  .basis.knowledge { color: var(--warn); }
  .answer { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 4px 22px 8px;
    box-shadow: var(--shadow); overflow-wrap: anywhere; }
  .answer h2, .answer h3, .answer h4 { margin: 16px 0 4px; font-size: 16px; }
  .answer p { margin: 10px 0; } .answer ul, .answer ol { margin: 8px 0; padding-left: 22px; }
  .answer code { background: var(--surface-alt); padding: 1px 5px; border-radius: 5px; font-size: 13px; }
  .answer pre { background: var(--surface-alt); padding: 10px 12px; border-radius: 10px; overflow-x: auto; }
  .answer a { color: var(--accent); }
  .answer .table { overflow-x: auto; margin: 10px 0; }
  .answer table { border-collapse: collapse; font-size: 14px; }
  .answer th, .answer td { border-bottom: 1px solid var(--line); padding: 6px 12px 6px 0; text-align: left; vertical-align: top; }
  sup.cite a {
    text-decoration: none; font-size: 11px; font-weight: 600; padding: 0 5px; border-radius: 8px; margin: 0 1px;
    background: var(--accent-soft); color: var(--accent);
  }
  .err { background: var(--danger-soft); color: var(--danger); border-radius: 12px; padding: 12px 16px; }
  .sources { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-top: 14px; }
  .src {
    display: block; padding: 10px 12px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface);
    color: var(--text); text-decoration: none; min-width: 0;
  }
  .src:hover, .src:target { border-color: var(--accent); background: var(--accent-soft); }
  .src .top { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
  .src .num { background: var(--accent-soft); color: var(--accent); font-weight: 600; border-radius: 8px; padding: 0 6px; }
  .src .title { font-size: 13.5px; font-weight: 600; margin-top: 4px; display: -webkit-box; -webkit-line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden; }
  .src .kind { margin-left: auto; font-size: 11px; }
  .src.uncited { opacity: .72; }
  .related { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
  .status { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 14px; padding: 6px 2px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--gradient); animation: pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: .35; transform: scale(.85); } 50% { opacity: 1; transform: scale(1); } }
  @media (prefers-reduced-motion: reduce) { .dot { animation: none; } }
  footer { border-top: 1px solid var(--line); background: var(--bg); padding: 12px 24px 14px; }
  .composer { max-width: 760px; margin: 0 auto; display: flex; gap: 10px; align-items: flex-end; background: var(--surface);
    border: 1px solid var(--line); border-radius: 26px; padding: 8px 8px 8px 20px; box-shadow: var(--shadow); }
  .composer:focus-within { border-color: var(--accent); }
  textarea { flex: 1; resize: none; border: 0; outline: 0; background: transparent; color: var(--text); font: inherit;
    max-height: 140px; padding: 8px 0; }
  textarea::placeholder { color: var(--muted); }
  #send { width: 40px; height: 40px; border-radius: 50%; border: 0; background: var(--gradient); color: #fff; cursor: pointer;
    display: grid; place-items: center; flex: none; }
  #send[disabled] { opacity: .4; cursor: default; }
  #send svg { width: 18px; height: 18px; }
  .note { max-width: 760px; margin: 8px auto 0; text-align: center; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <svg class="logo" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1.5l1.7 4.6 4.6 1.7-4.6 1.7L10
    14.1 8.3 9.5 3.7 7.8l4.6-1.7zM16 12.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>
  <h1>AI Mode</h1><span class="grow"></span>
  <button class="ghost" id="new" type="button" title="Start a new conversation">New chat</button>
</header>
<div id="scroll"><main id="thread" aria-live="polite"></main></div>
<footer>
  <form class="composer" id="form" autocomplete="off">
    <textarea id="q" rows="1" placeholder="Ask anything" aria-label="Ask a question" autofocus></textarea>
    <button id="send" type="submit" title="Send (Enter)" aria-label="Send"><svg viewBox="0 0 20 20" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 16V4M5 9l5-5 5 5"/></svg></button>
  </form>
  <p class="note" id="note"></p>
</footer>
<script id="state" type="application/json">__STATE__</script>
<script>
(function () {
  "use strict";
  var state;
  try { state = JSON.parse(document.getElementById("state").textContent); } catch (e) { state = { turns: [] }; }
  var thread = document.getElementById("thread"), box = document.getElementById("q"), send = document.getElementById("send");
  var SUGGESTIONS = ["Explain how vaccines train the immune system", "What are the pros and cons of electric cars?",
                     "Summarize the latest news on renewable energy", "How do I choose a good laptop for programming?"];

  function act(name, params) {
    var parts = [];
    for (var key in params) { if (params[key]) { parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key])); } }
    window.location.href = "pybrowser://aimode/action/" + name + (parts.length ? "?" + parts.join("&") : "");
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }   // never innerHTML for anything but the sanitized answer
    return node;
  }
  function ask(text) {
    text = (text || "").trim();
    if (!text || state.busy) { return; }
    act("ask", { q: text });
    box.value = ""; resize();
    state.busy = true; state.status = "Starting\\u2026";
    state.turns = (state.turns || []).concat([{ id: -1, question: text, html: "", sources: [], related: [], basis: "", error: "", pending: true }]);
    draw();
  }
  function banner(cls, message, label, action) {
    var b = el("div", "banner " + cls, message);
    if (label) { var btn = el("button", "ghost", label); btn.type = "button"; btn.addEventListener("click", function () { act(action, {}); }); b.appendChild(btn); }
    return b;
  }
  function drawTurn(turn, last) {
    var t = el("section", "turn");
    t.appendChild(el("h2", "q", turn.question));
    if (turn.pending) {
      var s = el("div", "status"); s.appendChild(el("span", "dot")); s.appendChild(el("span", "", state.status || "Working\\u2026"));
      t.appendChild(s); return t;
    }
    if (turn.error) { t.appendChild(el("div", "err", turn.error)); return t; }
    if (turn.basis) { t.appendChild(el("p", "basis " + (turn.basisKind || ""), turn.basis)); }
    var answer = el("div", "answer");
    answer.innerHTML = turn.html;                         // produced by app.aimode.markup (escape-first)
    t.appendChild(answer);
    if (turn.sources && turn.sources.length) {
      var grid = el("div", "sources");
      turn.sources.forEach(function (src) {
        var a = el("a", "src" + (src.cited ? "" : " uncited")); a.id = "src-" + src.n;
        a.href = src.url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.title = src.snippet || src.url;
        var top = el("div", "top"); top.appendChild(el("span", "num", String(src.n))); top.appendChild(el("span", "", src.host));
        top.appendChild(el("span", "kind", src.kind === "page" ? "page" : "snippet"));
        a.appendChild(top); a.appendChild(el("div", "title", src.title));
        grid.appendChild(a);
      });
      t.appendChild(grid);
    }
    if (last && turn.related && turn.related.length) {
      var rel = el("div", "related");
      turn.related.forEach(function (r) { var c = el("button", "chip", r); c.type = "button"; c.addEventListener("click", function () { ask(r); }); rel.appendChild(c); });
      t.appendChild(rel);
    }
    return t;
  }
  function draw() {
    thread.textContent = "";
    var turns = state.turns || [];
    if (state.disabled) {
      var off = el("div", "empty");
      off.appendChild(el("h2", "", "AI features are off"));
      off.appendChild(el("p", "", "AI Mode is part of the optional AI features. Turn them on in Settings to use it."));
      var go = el("button", "chip", "Open Settings"); go.type = "button"; go.style.marginTop = "18px";
      go.addEventListener("click", function () { act("settings", {}); });
      off.appendChild(go); thread.appendChild(off);
      send.disabled = true; box.disabled = true; return;
    }
    if (!state.providerReady) {
      thread.appendChild(banner("setup", "Set up an AI provider to use AI Mode. Your key stays in your system keyring. Gemini, Groq and OpenRouter have free tiers; a model on your own computer (Ollama, LM Studio) needs no key.", "Set up\\u2026", "setup"));
      var sample = el("button", "chip", "See a sample result (offline demonstration)"); sample.type = "button";
      sample.addEventListener("click", function () { act("demo", {}); });
      thread.appendChild(sample);
    } else if (!state.searchReady && !turns.length) {
      thread.appendChild(banner("warn", "Web search isn\\u2019t set up, so answers come from the model\\u2019s general knowledge and aren\\u2019t checked against sources.", "Set up web search\\u2026", "search-setup"));
    }
    if (!turns.length) {
      var e = el("div", "empty");
      e.innerHTML = '<svg class="spark" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1.5l1.7 4.6 4.6 1.7-4.6 1.7L10 14.1 8.3 9.5 3.7 7.8l4.6-1.7zM16 12.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>';
      e.appendChild(el("h2", "", "Ask anything"));
      e.appendChild(el("p", "", state.searchReady ? "Get an answer built from pages found on the web, with the sources next to it." : "Get a quick answer. Add web search in settings for answers built from real pages."));
      var chips = el("div", "chips");
      SUGGESTIONS.forEach(function (s) { var c = el("button", "chip", s); c.type = "button"; c.addEventListener("click", function () { ask(s); }); chips.appendChild(c); });
      e.appendChild(chips); thread.appendChild(e);
    }
    turns.forEach(function (turn, i) { thread.appendChild(drawTurn(turn, i === turns.length - 1 && !state.busy)); });
    send.disabled = !!state.busy || !box.value.trim();
    document.getElementById("note").textContent = turns.length ? "AI can make mistakes. Check the sources." : "";
    // Keep the newest question at the top of the view (and the answer under it), not the page bottom.
    var turnsEls = thread.querySelectorAll(".turn");
    if (turnsEls.length) { turnsEls[turnsEls.length - 1].scrollIntoView({ block: "start" }); }
  }
  function resize() { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 140) + "px"; send.disabled = !!state.busy || !box.value.trim(); }

  window.PB = { setState: function (next) { state = next || { turns: [] }; draw(); }, focus: function () { box.focus(); } };
  document.getElementById("form").addEventListener("submit", function (ev) { ev.preventDefault(); ask(box.value); });
  box.addEventListener("keydown", function (ev) { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); ask(box.value); } });
  box.addEventListener("input", resize);
  document.getElementById("new").addEventListener("click", function () { act("new", {}); state = { turns: [], providerReady: state.providerReady, searchReady: state.searchReady }; draw(); box.focus(); });
  draw(); resize();
})();
</script>
</body>
</html>
"""

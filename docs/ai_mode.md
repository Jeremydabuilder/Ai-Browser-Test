# AI is optional; AI Mode

## The master switch

PyBrowser is an ordinary browser until you turn AI on: **Settings -> AI features -> Turn on AI features** (off by default,
like Firefox's optional features). First launch asks once - *Keep AI off* is the default answer.

| | AI off (default) | AI on |
|---|---|---|
| New tab | wordmark, search box, your sites | + an **AI Mode** button |
| Menus | no AI entries (their shortcuts are disabled too) | Tools -> **AI** submenu, Libraries, Automation... |
| Toolbar / address bar | no AI button, no "Ask" hint | AI Mode button; **Alt+Enter** sends typed text to AI Mode |
| Right-click | the browser's normal menu | + "Ask Py about..." |
| Side panel, Team, missions, scheduled tasks | not created, not started | available |
| Network / keys | no AI service is contacted, no AI key is read | only when you ask |

Switching applies immediately (no restart); turning it off closes an open AI panel. An AI action reached some other way (a
stale link, an old shortcut) explains that AI is off and offers the switch instead of silently doing nothing. Settings'
AI-only tabs (Connected Tools, External AI Access, Privacy & Security) appear only while AI is on.

## AI Mode (`pybrowser://aimode/`, Ctrl+Shift+Space)

Ask a question, get a short answer with the sources next to it, follow up. It reuses what already exists - your Groq key
(Tools -> AI -> Configure AI Agent), the Team's web search (Tavily or Brave, bring your own key) and its SSRF-safe page
reader - and adds no key storage of its own.

Per question: (1) follow-ups are first rewritten into a standalone search query (one model call; the first question is
searched as typed); (2) web search (queries are redacted by the app's firewall first); (3) the top three result pages are read,
the rest stay snippets; (4) one answer call with the sources fenced as untrusted data; every `[n]` marker is checked against
the real sources and removed if it points at nothing.

Honest labelling, enforced in code and shown on the page:

* **Based on N web pages and M search snippets - searched with Tavily** - sources are listed as cards (cited ones first
  emphasised); snippet-only sources say so.
* **From the model's general knowledge - not checked against sources** - when web search is not set up or failed; no citations
  are allowed in that case, and the empty page says search is missing.
* Progress is the real step ("Searching the web...", "Reading example.com..."); there are no invented percentages.
* A conversation is capped at 40 model calls; **Stop** and **New chat** are always available.

The answer HTML is built by an escape-first converter (`app/aimode/markup.py`); the page writes everything else with
`textContent`. Model output cannot add a tag, an attribute or a `javascript:` link.

## Cleaner window

The menu bar is hidden; every menu lives in the toolbar's main menu (the three dots), with **Show menu bar** (F10) to bring
it back. The workspace switcher only appears once you have more than one workspace. The new tab page is one search box and
one row of sites.

## Verified / not verified

Verified (tests, offscreen Qt/WebEngine): the switch hides and disables every AI entry and is live-reversible; the page,
its actions and state restore; the pipeline with a scripted model and fake search/fetch; the renderer against hostile output.
**Not verified:** a live Groq answer, live search, real-model answer quality, Windows/macOS rendering.

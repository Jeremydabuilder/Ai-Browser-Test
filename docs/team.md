# AI Team: a real multi-agent crew in the side panel

Six specialised agents - **Coordinator, Researcher, Writer, Coder, Reviewer,
Tester** - plan a mission, hand each other explicit artifacts, review each
other's work and assemble one final result. Open it with **Tools -> AI Team...**
(or the **Team** tab at the top of the AI panel).

This is separate from the older *Missions* system (Planner / Operator / Critic
driving `AgentSession` tool loops). A Team run is a graph of plain model calls
with artifact handoffs; its agents have **no browser-driving, messaging,
publishing or deleting tools at all**.

## Quick start on Windows

1. **Install** Python 3.11+ and Git, then in PowerShell:
   ```powershell
   git clone https://github.com/Jeremydabuilder/Ai-Browser-Test.git
   cd Ai-Browser-Test
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python main.py
   ```
2. **Add your Groq key** (once). Open **Tools -> AI Team...**, press **Set up Groq...**
   - this opens the existing *Configure AI Agent* dialog already on Groq. Paste the
   key, **Save**. It goes to the Windows Credential Manager (the same entry the AI
   panel uses, so if Groq already works in the AI panel there is nothing to do).
   Back in the Team tab press **Test** - one tiny real request proves the key.
   Alternative: `setx GROQ_API_KEY "gsk_..."` and restart the browser.
3. **(Optional) Web search.** Team tab -> **Settings** -> *Web search*: pick Tavily or
   Brave Search, paste its key, **Save key**, **Test**. See "Web search" below.
4. **(Optional) Run tests safely.** Windows can only run generated code in a container:
   install **Docker Desktop** (WSL 2 backend, Linux containers - the default), start it,
   then `docker pull python:3.12-slim`. Team tab -> **Settings** -> *Code sandbox* ->
   **Check sandbox**. Without it everything else works; the Tester is simply dropped and
   the result says tests were not run.
5. **Try it.** Open two product pages in tabs, Team tab -> **Compare tabs** example ->
   **Add tabs...** (tick both) -> **Start team**. Watch Agents / Tasks / Activity; the
   **Results** tab opens when it finishes. **Save to Downloads** puts the result in your
   Downloads folder and in the browser's Downloads window (Ctrl+J).
6. **Live check from a terminal** (no UI; proves Coordinator -> Researcher -> Writer ->
   Reviewer against the real provider and tells you exactly why if it cannot):
   ```powershell
   python scripts\team_live_check.py          # add --web to include web search
   ```
   Exit code 0 = passed, 1 = ran but failed, 2 = blocked (no/invalid key or provider
   unreachable).

Linux / macOS: the same `pip install` / `python main.py`; the built-in sandbox needs
`bubblewrap` on Linux (`sudo apt install bubblewrap`), `sandbox-exec` ships with macOS.

## Credentials

* **Model (Groq by default):** the browser's existing lookup - OS keyring entry saved by
  *Configure AI Agent*, else `GROQ_API_KEY`. Nothing is bundled or shared. The key lives
  only inside the provider client; it is scrubbed from every event, error, stored mission
  and `repr`. Other providers: `PYBROWSER_TEAM_PROVIDER` / Settings (`openai`,
  `openrouter`, `gemini`); model: `PYBROWSER_TEAM_MODEL` / Settings (default
  `llama-3.3-70b-versatile`, or the AI agent's remembered Groq model).
* **Web search (separate service, separate key):** see below.
* **Container runtime:** no credential; only Docker running.

No key found -> the Team tab says exactly what is missing, **Start team** shows that
message instead of starting, and an engine run that loses its key fails with the same
message. It never produces placeholder output.

## Web search

The Researcher can search the web through a supported API - **bring your own key**:

| Provider | Key | Where | Notes |
|---|---|---|---|
| Tavily | `TAVILY_API_KEY` | app.tavily.com | Built for LLM agents; returns a relevant passage per hit. Limited free tier - check their current pricing. |
| Brave Search API | `BRAVE_SEARCH_API_KEY` | api-dashboard.search.brave.com | Subscription token; check their current plans and free allowance. |

Pick a provider and save the key in **Settings -> Web search** (stored in the OS keyring,
never the settings database) or set the environment variable and
`PYBROWSER_SEARCH_PROVIDER`. Per mission there is a **Search the web** checkbox (on by
default once configured).

* **What leaves your machine:** only 1-2 short search queries per research task, written by
  the model from the mission wording (never page contents), with likely secrets removed by
  the app's firewall first. Page text, files and answers are never sent to the search service.
* **What comes back:** title, URL and a snippet per hit. Pages are *not* opened.
* **Kept distinct:** web hits are `web search result` sources, shown and cited separately
  from what you attached (`attached tab / text / file`) in the prompt to the agents, the
  Sources view and the final "Sources" / "Web search results" sections, and the result says
  they are snippets and should be treated as leads.
* A failing search (rate limit, quota, bad key) degrades gracefully: the mission continues
  on attached sources and records the failure as a limitation.

## What it does

| Agent | Does | Produces |
|---|---|---|
| Coordinator | Understands the mission, writes success criteria, builds the task graph, activates only the agents needed, assembles the final answer | plan, final result |
| Researcher | Reads attached sources, local knowledge (when enabled) and web results; keeps `[S#]` citations | research notes |
| Writer | Reports, comparisons, drafts, documentation from the notes | draft |
| Coder | Creates/edits code; returns complete files | file artifacts (+ diffs against your workspace) |
| Reviewer | Checks requirements, evidence, completeness, test results; asks for *specific* revisions | review verdict |
| Tester | Picks real checks and runs them in the sandbox | test report from actual output |

Flow: mission -> Coordinator plan (validated; one repair attempt, then a labelled fallback)
-> tasks run when dependencies are done, up to the concurrency limit -> Reviewer -> if it
asks for changes, a *revision task* for the producing agent (previous artifact, original
inputs and the exact issues) and a re-review, up to the revision limit -> Coordinator
assembles the result.

Every task has an id, dependencies, assigned agent, status, inputs (artifact ids handed in),
outputs (artifact ids produced) and acceptance criteria. The panel shows agent cards with
their real current assignment, the task board with dependencies, a live activity feed
(progress and tool actions, never private reasoning), the artifacts, sources, generated
files and final result, plus Cancel / Retry and saved history. The whole panel scrolls, so
nothing overlaps in a short window or a 300px column.

## Inputs

Open tabs (read through the browser's own page/PDF extraction), pasted text and files
(txt, md, json, csv, docx, pdf). A page that cannot be read (internal page, still loading,
sign-in wall, no text) is shown as **not included with the reason**, never sent to a model,
and listed again in the final result.

## Generated files and Downloads

**Save to Downloads** writes through the browser's `DownloadManager`: the file lands in your
Downloads folder (never overwriting an existing file - `name (1).ext`), appears in the
Downloads window with *Show in folder*, and raises the usual notice. **Save all files**
puts every generated file in one `AI Team - <mission> (#id)` subfolder. **Save as...** lets
you pick the location (you confirm any overwrite in the dialog) and is still listed in
Downloads. Names are reduced to safe relative paths and can never escape the folder.
Workspace edits are separate: proposals until you press **Apply to workspace...** and
confirm the listed diffs.

## Code sandbox (Tester)

Generated code runs **only** inside an isolated environment that has **proved itself at
probe time**, or not at all. There is no setting and no fallback that runs it on the host.
The probe runs a self-test inside the backend and requires: a canary file planted in your
home folder is unreadable, a listener opened on host loopback is unreachable, and only the
scratch directory is writable. A backend that fails is rejected with the reason.

| Platform | Backend | Isolation |
|---|---|---|
| Linux | bubblewrap | user/pid/ipc/net/uts namespaces; only `/usr`, `/bin`, `/lib*` and the Python install are visible (read-only); empty `/tmp`; your home folder, the project and `/etc` do not exist inside; no network |
| macOS | `sandbox-exec` | profile denying network and reads of `/Users`, `/Volumes`; writes only to the scratch dir |
| Windows (or any OS) | Docker / Podman container | `--network none`, read-only root, all capabilities dropped, `no-new-privileges`, non-root user, pid/memory/CPU/file limits, one bind mount (the scratch dir), `--pull never` |

All backends also add CPU/memory/file-size/wall-clock limits, a scrubbed environment (no
keys) and an allow-list of commands (`python -m unittest|py_compile|pytest`, staged `.py`
scripts). If nothing qualifies, the Tester is dropped from the plan, the Team tab says
"Sandbox: unavailable (how to enable)", and the result states that tests were not run.
The container image is never pulled implicitly: run `docker pull python:3.12-slim` (or use
**Download image** in Settings).

## Safety model

* **Untrusted content.** Page and file text is fenced as data (the AI agent's
  `wrap_untrusted`, which also redacts likely secrets); every prompt says fenced text and
  teammates' artifacts are data, never instructions. Citations to sources that do not exist
  are removed.
* **No outward actions.** Agents cannot send, publish, delete or touch accounts. A mission
  that asks for one yields a draft you act on yourself.
* **Files.** The Coder reads an authorized workspace only if you pick one, and only
  *proposes* changes; paths are confined to it and symlinks out of it ignored.

## Limits (all configurable)

Defaults suit Groq's free tier: 2 agents at once, 10 tasks, 60 model calls per run, 2
revision rounds, 3 retries per call with Retry-After-aware exponential backoff (capped),
240s per task, 60s per sandbox check. Cancel stops scheduling immediately and interrupts
waits. Retry continues from the finished work.

## What has and has not been verified

See the test files. In summary: engine behaviour, the Groq HTTP request/retry/error paths
(against a mock transport and a local OpenAI-compatible server), both search providers'
request/response handling (mock transports), Linux bubblewrap isolation (real), the
container backend (against a stub `docker` - **not** a real Docker daemon), and the whole
panel in the real window with the real theme are verified. **Not verified anywhere in CI:**
a live Groq call, live Tavily/Brave calls, a real Docker/Windows run, macOS `sandbox-exec`.
`scripts/team_live_check.py` and **Test** buttons exist so you can verify those yourself.

## Known limitations

* Web search returns snippets, not full pages; the model may over-trust a snippet.
* The Tester only runs **Python** checks. The Linux sandbox shows read-only `/usr`; it is
  namespace isolation, not a hypervisor.
* A model request already in flight cannot be aborted mid-HTTP; its result is discarded.
* Attached page text is saved with the mission (for Retry and history). Delete the mission
  to remove it.
* Citation checking verifies that a cited source *exists*, not that it supports the claim -
  that is the Reviewer's job, and models can miss things.

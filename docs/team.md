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

A shorter step-by-step first-run list with expected results: [team_windows_checklist.md](team_windows_checklist.md).

1. **Install** Python 3.11+ and Git, then in PowerShell:
   ```powershell
   git clone https://github.com/Jeremydabuilder/Ai-Browser-Test.git
   cd Ai-Browser-Test
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python main.py
   ```
   Calling `.venv\Scripts\python` directly avoids PowerShell's *"running scripts is disabled"*
   error that `Activate.ps1` triggers under the default execution policy (or run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `cmd`).
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
   If Windows reports a path error from Docker, check that your `%TEMP%` is on a local drive
   that Docker Desktop shares; folders containing commas are handled.
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
* **What comes back:** title, URL and a snippet per hit. The top few hits are then **opened and read** (see
  "Reading pages" below); a hit that cannot be opened stays a snippet.
* **Kept distinct:** web sources are labelled `web page text (retrieved)`, `web page text (shortened)` or
  `web search snippet (page not opened)`, separate from what you attached (`attached tab / text / file`), in the prompt to
  the agents, the Sources view and the final "Sources" / "Web pages (retrieved text)" / "Web search snippets"
  sections (retrieved pages show their retrieval date and whether they were shortened). The result says which are snippets to treat as leads.
* A failing search (rate limit, quota, bad key) degrades gracefully: the mission continues
  on attached sources and records the failure as a limitation.

## Reading pages (SSRF-safe)

Search results are attacker-influenced, so the page reader treats every URL as hostile
(`app/team/webfetch.py`):

* Only `http`/`https` on ports 80/443; no credentials in the URL; numeric/obfuscated hosts
  (`2130706433`, `0x7f.1`) and local names (`localhost`, `*.local`, `*.internal`...) are refused.
* The host is resolved first and **every** address must be globally routable - loopback, private
  (RFC 1918/ULA), link-local (incl. `169.254.169.254` cloud metadata), CGNAT, multicast, reserved,
  and IPv4 hidden in IPv6 (mapped/6to4/Teredo/NAT64) are refused. The connection is then made to
  the validated address (Host header, SNI and certificate verification use the original hostname -
  a certificate that is only valid for the IP, expired, untrusted or for another name is refused), so DNS cannot change its
  answer between check and connect. Every redirect is re-validated (max 4).
* **Proxies are ignored on purpose.** `HTTP(S)_PROXY` variables are not used: a proxy would make the
  connection go somewhere other than the address that was checked. On a network that forces a
  proxy, page reading simply fails (the result stays a snippet) rather than bypassing the check.
* Content is inflated by the app itself with a hard output cap, so a gzip/deflate "bomb" cannot
  exhaust memory; encodings other than gzip/deflate are refused.
* Limits: 10s per request (20s total), 1.5 MB decoded (compression bombs are cut off), 20,000
  characters kept, HTML/plain text only, no cookies, no credentials, no referrer.
* Text extraction drops scripts, styles, navigation, footers, forms and visually hidden text.
  What comes back is still **untrusted data**: fenced and cited like any attached page.
* Settings: *Web pages to read per research task* (0 = snippets only), timeout, characters.
  A mission reads at most three times the per-task number. On **Retry** the pages already read
  are reused - nothing is searched or downloaded twice.

## Handoffs, review feedback and recovery

* Every agent ends with a short **handoff note** (what it finished, what is uncertain, what it
  asks of the next agent). Notes are shown on the task card and passed to downstream agents.
* The **Reviewer** returns structured feedback: a per-criterion check, *blocking* issues (id
  `B1`..., task, where, problem, evidence, exact change) and optional *minor* suggestions
  (`M1`...). Only blocking issues trigger a revision, and the revising agent gets them as a
  checklist. On the next round the Reviewer must mark each earlier issue fixed / not fixed.
  Suggestions and the criteria check appear in the final result.
* **No stale combinations:** every task records which versions of upstream work it was built on. If
  that work changes afterwards (a revision of the research, a retried task), anything built on the old
  version is redone - e.g. a revised research task re-queues the draft and the review - and the old
  artifacts stay visible but marked replaced. Reviews count as feedback, not inputs.
* **Spending limits:** each run (start, Retry, Resume, Revise again) has its own budget of model calls,
  but one mission can never use more than **three runs' worth** in total. The panel shows what is left;
  Retry is disabled when nothing is left. Cancel stops new work at once but cannot undo calls already
  made or tokens already consumed, and a reply that arrives after Cancel is discarded.
* **Rate limits:** after a call exhausts its retries, the task waits (a cool-down that grows,
  honouring Retry-After) and is re-queued up to twice; the team drops to one agent at a time and
  says so. Only then does the task fail.
* **Retry** continues from finished work. Failed tasks show **Retry this task** / **Skip it**;
  after the revision limit **Revise again** grants one more review round; an interrupted mission
  (closed app, crash) is marked and **Resume**d without repeating completed tasks.

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

See the test files. `tests/test_team_e2e_missions.py` runs realistic missions (compare two products from
fetched pages, hostile page, reviewer-driven revision, provider failure + retry without re-fetching, rate
limit, skip) with the real engine and page fetcher over real local sockets and a prompt-reading stand-in
model - it checks that facts reach the final result with correct citations, not just that tasks ran.
`tests/test_team_webfetch_network.py` proves which address the fetcher actually dials. In summary: engine behaviour, the Groq HTTP request/retry/error paths
(against a mock transport and a local OpenAI-compatible server), both search providers'
request/response handling (mock transports), Linux bubblewrap isolation (real), the
container backend (against a stub `docker` - **not** a real Docker daemon), and the whole
panel in the real window with the real theme are verified. Page fetching is verified against a fake network (address policy, pinning, redirects, size and time
limits) - not against the live internet. **Not verified anywhere in CI:**
a live Groq call, live Tavily/Brave calls, a real Docker/Windows run, macOS `sandbox-exec`.
`scripts/team_live_check.py` and **Test** buttons exist so you can verify those yourself.

## Known limitations

* Page reading is text-only: JavaScript-rendered pages, PDFs, images and sign-in walls are not seen
  (such a result stays a snippet). The model may still over-trust a snippet.
* The Tester only runs **Python** checks. The Linux sandbox shows read-only `/usr`; it is
  namespace isolation, not a hypervisor.
* A model request already in flight cannot be aborted mid-HTTP; its result is discarded.
* Attached page text is saved with the mission (for Retry and history). Delete the mission
  to remove it.
* Citation checking verifies that a cited source *exists*, not that it supports the claim -
  that is the Reviewer's job, and models can miss things.

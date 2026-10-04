# AI Team: a real multi-agent crew in the side panel

Six specialised agents - **Coordinator, Researcher, Writer, Coder, Reviewer,
Tester** - plan a mission, hand each other explicit artifacts, review each
other's work and assemble one final result. Open it with **Tools -> AI Team...**
(or the **Team** tab at the top of the AI panel).

This is separate from the older *Missions* system (Planner / Operator / Critic
driving `AgentSession` tool loops). A Team run is a graph of plain model calls
with artifact handoffs; its agents have **no browser-driving, messaging,
publishing or deleting tools at all**.

## Start it

```bash
pip install -r requirements.txt
python main.py
```

Credentials use the browser's existing mechanism - nothing new to configure if
Groq already works in the AI panel:

1. **Tools -> Configure AI Agent...**, choose Groq, paste your key (stored in the
   OS keyring), **or** start the browser with `GROQ_API_KEY=... python main.py`.
2. **Tools -> AI Team...**, tick/attach sources, type a mission, **Start team**.

No key is bundled, shared or guessed. If none is found the Team tab says exactly
what is missing, **Start team** shows that message instead of starting, and an
engine run that somehow loses its key fails with the same message - the Team
never produces placeholder output. Other overrides: `PYBROWSER_TEAM_PROVIDER`
(`groq`, `openai`, `openrouter`, `gemini`), `PYBROWSER_TEAM_MODEL`, or
**Limits and model...** in the Team tab. The default model is
`llama-3.3-70b-versatile` unless the AI agent already has a Groq model chosen.

## What it does

| Agent | Does | Produces |
|---|---|---|
| Coordinator | Understands the mission, writes success criteria, builds the task graph, activates only the agents needed, assembles the final answer | plan, final result |
| Researcher | Reads the attached sources (and the local knowledge index when enabled), keeps `[S#]` citations | research notes |
| Writer | Reports, comparisons, drafts, documentation from the notes | draft |
| Coder | Creates/edits code; returns complete files | file artifacts (+ diffs against your workspace) |
| Reviewer | Checks requirements, evidence, completeness, test results; asks for *specific* revisions | review verdict |
| Tester | Picks real checks and runs them in a sandbox | test report from actual output |

**Flow:** mission -> Coordinator plan (validated; one repair attempt, then a
labelled fallback) -> tasks run when their dependencies are done, up to the
concurrency limit -> Reviewer -> if it asks for changes, a *revision task* for
the producing agent (it receives its previous artifact, the original inputs and
the exact issues) and a re-review, up to the revision limit -> Coordinator
assembles the result, with a Sources section built from what was actually cited.

Every task has an id, dependencies, assigned agent, status, inputs (artifact ids
it was handed), outputs (artifact ids it produced) and acceptance criteria. The
panel shows agent cards with their real current assignment, the task board with
dependencies, a live activity feed (progress and tool actions, never private
reasoning), the artifacts, sources, generated files and final result, plus
Cancel / Retry and saved history.

## Inputs

Open tabs (read through the browser's own page/PDF extraction), pasted text and
files (txt, md, json, csv, docx, pdf). The list shows exactly which pages are
included; a page that cannot be read (internal page, still loading, sign-in wall,
no text) is shown as **not included with the reason**, is never sent to a model,
and is listed again in the final result under "Could not be read".

## Safety model

* **Untrusted content.** Page and file text is fenced as data (the same
  `wrap_untrusted` boundary the AI agent uses, which also redacts likely
  secrets); every agent prompt says fenced text and teammates' artifacts are
  data, never instructions. Citations to sources that do not exist are removed.
* **No outward actions.** Agents cannot send, publish, delete or touch accounts.
  A mission that asks for one yields a draft you act on yourself.
* **Files.** The Coder reads an authorized workspace folder only if you pick one,
  and only *proposes* changes. **Nothing is written until you press Apply and
  confirm** a dialog listing every diff. Paths are resolved and confined to the
  folder; symlinks pointing outside it are ignored. Without a workspace, files
  are downloadable artifacts.
* **Running code.** Only in the sandbox (`app/team/sandbox.py`): throw-away
  directory, scrubbed environment (no keys), CPU / memory / file-size / open-file /
  wall-clock limits, **no network** (Linux network namespace; macOS
  `sandbox-exec`). Commands are allow-listed to `python -m unittest|py_compile|pytest`
  and staged `.py` scripts. Windows, and systems without network isolation, report
  the sandbox **unavailable**; Tester tasks are then removed from the plan and the
  limitation is stated in the result. (You can opt in to running without network
  isolation in Limits and model.) It is *not* a filesystem jail.
* **Credentials** stay in this process: resolved via the existing keyring/env
  lookup, held only by the provider client, scrubbed from every event, error and
  stored mission. There is no extension or frontend involved.

## Limits (all configurable)

Defaults suit Groq's free tier: 2 agents at once, 10 tasks, 60 model calls per
run, 2 revision rounds, 3 retries per call with Retry-After-aware exponential
backoff (capped), 240s per task, 60s per sandbox check. Cancel stops scheduling
immediately and interrupts waits. Retry continues from the finished work.

## Known limitations

* **No web search.** The browser has no search tool the team can call. The
  Researcher uses attached sources plus - only when *Semantic history* is on -
  your local knowledge index. With neither, research is labelled unverified.
* The Tester only runs **Python** checks.
* Rate limits: bounded retries then a clear failure; the task is retryable.
* A model request already in flight cannot be aborted mid-HTTP; its result is
  discarded on cancel/timeout.
* Attached page text is saved with the mission (for Retry and history). Delete
  the mission to remove it.
* Citation checking verifies that a cited source *exists*, not that it supports
  the claim - that is the Reviewer's job, and models can miss things.

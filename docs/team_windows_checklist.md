# AI Team: Windows first-run checklist

About 10 minutes for the required part. Items marked *optional* can be skipped; the Team works without them.
Nothing here has been run on Windows or against live services by the developers - that is what this
checklist is for. If a step does not behave as written, note the step number and what you saw.

## 0. Start the app (required)
```powershell
git pull
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```
Expect: the browser opens. (Calling `.venv\Scripts\python` directly avoids the PowerShell
"running scripts is disabled" error that `Activate.ps1` can cause.)

## 1. Groq key (required)
1. **Tools -> AI Team...**, then **Set up Groq...** (or Tools -> Configure AI Agent -> Groq).
2. Paste your key, **Save**. Back in the Team tab press **Test**.
   Expect: a green "works" message after a second or two. A red message names the problem
   (invalid key, rate limit, offline).
3. Optional, no UI: `.venv\Scripts\python scripts\team_live_check.py` -> `PASS`, exit code 0 (2 = no/invalid key).

## 2. One attached-tab mission (required)
1. Open two product or article pages in tabs.
2. Team tab -> **Compare tabs** example -> **Add tabs...** -> tick both -> **Start team**.
3. Watch **Agents / Tasks / Activity**. Expect: Coordinator plans, Researcher/Writer/Reviewer work, a progress
   bar fills, the **Results** tab opens by itself.
4. Check the **Final result**: every claim has an `[S1]`-style citation, the **Sources** list names your two tabs,
   and there is a **Notes and limitations** section.
5. Look at the status line: it shows the model calls used and how many are left (one mission is capped at
   three runs' worth). **Save to Downloads** -> the file appears in your Downloads folder and in Ctrl+J.
6. **Ask** tab: type "Which one is cheaper per year?" -> the answer says it came from existing evidence and the status line
   shows one more model call used. Try **Rewrite the result** -> "Make it shorter"; the old version stays under **History**.
7. Try **Cancel** on a second run: it stops new work at once; calls already made are not refunded.

## 3. Web search (optional)
1. Team -> **Settings** -> *Web search*: choose Tavily or Brave, paste its key, **Save key**, **Test**.
2. Run a mission with **Search the web** ticked and *no* tabs, e.g. "What are the main differences between X and Y?".
3. Expect: Activity shows searches and "Read <site>"; in Sources, pages are labelled *Web pages (retrieved text)*
   with a retrieval date, and anything not opened is under *Web search snippets (page not opened)*.
   Sites that need JavaScript or a sign-in stay snippets, with a reason.
4. Behind a company proxy, page reading may fail on purpose (proxies are ignored for safety); results then stay snippets.

## 4. Safe code testing (optional)
1. Install **Docker Desktop** (Linux containers), start it, then `docker pull python:3.12-slim`.
2. Team -> **Settings** -> *Code sandbox* -> **Check sandbox**. Expect: "Sandbox: docker container".
3. Mission: "Write a Python function that parses ISO dates, with unit tests." Expect a Tester task with a real
   test report in Results. Without Docker the Tester is simply left out and the result says tests were not run.

## What is still unverified
Live Groq/Tavily/Brave calls, reading real public websites and real certificates, real model output quality,
Windows (Credential Manager, Docker Desktop, paths), macOS `sandbox-exec`.

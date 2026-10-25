# Windows clean-machine tester checklist

Purpose: find out what actually happens when a person who is not the developer installs the unsigned preview on a
machine that has never seen PyBrowser, Python, or this repository. Nothing below has been run on a clean Windows
machine by the developers; every box is unchecked until a person ticks it from a real run.

Use a fresh Windows 10/11 user profile, or a virtual machine (the free Windows 11 development VM, or Windows
Sandbox, works; Sandbox forgets everything on close, which is useful for the uninstall step but not for restart).
You do **not** need to disable any Windows protection, and you should not: the SmartScreen prompt below is
per-file and expected for an unsigned build.

Record: Windows version/build `_______`, machine or VM `_______`, build file name and its SHA-256 `_______`, date `_______`.

## 1. Installation
- [ ] Download `PyBrowser-Setup-*.exe` from the Releases page (a pre-release, labelled unsigned).
- [ ] Compare the file's hash with `SHA256.txt`: `Get-FileHash .\PyBrowser-Setup-*.exe -Algorithm SHA256`.
- [ ] Double-click it. Windows shows *"Windows protected your PC"* (expected: the build is **not code-signed**).
      Use **More info → Run anyway** for this one file only. Note anything else the OS or antivirus says.
- [ ] The installer runs **without an administrator prompt** and installs for your user only.
- [ ] Install finishes; "Launch PyBrowser" works. Start-menu entry exists; desktop shortcut only if you ticked it.

## 2. First launch
- [ ] A window opens within ~15 seconds and is usable (no console window, no crash dialog).
- [ ] The first-run question appears once: AI is **off by default**; "Keep AI off" is the default button.
- [ ] Choose **Keep AI off**. The toolbar, menus and new-tab page show no AI entries.
- [ ] `%LOCALAPPDATA%\PyBrowser` exists (settings, history, `logs\pybrowser.log`).

## 3. Normal browsing (AI still off)
- [ ] Load 3 ordinary sites, including one video or image-heavy page. Open, switch and close tabs; back/forward.
- [ ] Bookmark a page (Ctrl+D); find in page (Ctrl+F); download a file and see it in Downloads (Ctrl+J).
- [ ] Close and reopen: history, bookmarks and open tabs behave as you expect.
- [ ] Dark/light follows the system theme; text is readable; keyboard Tab/Shift+Tab reaches the toolbar and address bar.

## 4. AI setup (needs your own provider key, or a local model)
Free options to try, in order of least effort: Google Gemini or Groq (both have free tiers; limits and terms are the
provider's), or a model running on your own PC (Ollama or LM Studio, no key).
- [ ] Settings → AI features → **Turn on AI features**. The AI menu and AI Mode button appear without restarting.
- [ ] Before configuring anything, open **AI Mode**: it says a provider is missing and offers
      *"See a sample result (offline demonstration)"*. The sample says clearly that no model produced it.
- [ ] **Set up…** opens the provider dialog, starting on Gemini. Paste a key and press **Test Connection**.
      A wrong key gives a readable error; a right key reports success. (Write down which provider you used.)
- [ ] Your key is not visible anywhere afterwards (Settings, logs, screenshots).

## 5. One successful Mission
- [ ] Open 2–3 product pages (e.g. headphones or laptops you actually might buy) in tabs.
- [ ] **AI → AI Team…** → *Compare products* → **Add tabs…** → type 2–3 **requirements** (e.g. "Under $200") → **Start team**.
- [ ] Progress is understandable without reading logs. The run finishes with a result that has: a recommendation
      labelled as judgement, each of your requirements marked met / not met / not found, figures with `[S#]` citations,
      sources listed with page vs snippet and a retrieval date, and a *Citation check (automatic)* section.
- [ ] Open at least two cited sources and compare the claim to the page yourself. Note any claim the sources do not support.
- [ ] Any page the team could not read is listed under *Could not be read*; nothing is invented about it.
- [ ] Start a browsing Mission instead (AI panel → goal) and use **AI → Continue Mission in AI Team…**: the goal and
      constraints are pre-filled; **Save summary to Mission** writes a short summary back (only when you press it).

## 6. Restart and resume
- [ ] Start another run and **close PyBrowser while it is working** (not during an approval prompt for a real action).
- [ ] Reopen. The run appears under Team → History as interrupted, finished steps are kept, and **Resume** continues
      without repeating completed work (the "model calls used" figure should not double).
- [ ] A completed run still opens from History with its sources and result after a restart.
- [ ] Browsing Missions you created are still listed, and none re-ran a form submission or purchase by itself.

## 7. Failure behaviour (do what comes naturally; do not try to cause damage)
- [ ] Disconnect the network mid-run (or enter a wrong key): the error is plain language and the browser stays usable.
- [ ] If you hit a free-tier rate limit: the wait is shown and the run continues or can be resumed.
- [ ] Press **Cancel** during a run: it stops new work promptly; finished work stays.

## 8. Uninstall
- [ ] Settings → Apps → PyBrowser → Uninstall. The program folder is removed.
- [ ] `%LOCALAPPDATA%\PyBrowser` is **still there** (by design: your history and Missions are not deleted).
- [ ] API keys saved by PyBrowser may remain in **Windows Credential Manager**. Before uninstalling, use
      *Remove stored key* in the provider dialog if you want them gone. Note whether any entry remained.
- [ ] Reinstall: your data is picked up again.

## Report
For each failed box: the step number, what you did, what you expected, what happened, a screenshot, and
`%LOCALAPPDATA%\PyBrowser\logs\pybrowser.log` (it contains no keys; skim it before sharing anyway).
Open an issue on the repository or send it to the person who gave you the build.

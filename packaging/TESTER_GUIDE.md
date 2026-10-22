# PyBrowser - tester guide (Windows)

You have an **unsigned preview build**. It is real software that has not been through a wide audience yet.

## Install
* **Installer** (`PyBrowser-Setup-*.exe`): double-click. No admin rights needed; installs for your user only.
* **Portable** (`PyBrowser-Portable-*.zip`): unzip anywhere, run `PyBrowser.exe`. Nothing is installed.

Windows will say **"Windows protected your PC"** because the build is not code-signed yet. Click **More info -> Run anyway**.
(The checksums in `SHA256.txt` let you confirm the file is the one you were sent:
`Get-FileHash .\PyBrowser-Setup-*.exe -Algorithm SHA256`.)

## What to expect
* It starts as an **ordinary browser**. AI features are **off** until you switch them on in **Settings -> AI features**.
* Everything lives under `%LOCALAPPDATA%\PyBrowser` (settings, history, logs in `logs\pybrowser.log`). Uninstalling the app does
  not delete it; delete that folder to reset.
* Nothing is sent to any AI service unless you turn AI on and add your own key (Groq by default).

## Please try
1. Browse a few normal sites; open/close tabs; downloads (Ctrl+J); bookmarks; find in page (Ctrl+F).
2. Settings -> turn on AI features -> Set up AI provider (needs a Groq key) -> **AI Mode** (Ctrl+Shift+Space) -> ask something.
3. Optional: add a Tavily or Brave search key (Tools -> AI -> AI Team... -> Settings) and ask again: answers should cite web pages.
4. Full checklist: `docs/team_windows_checklist.md`.

## Report problems
Say what you did, what you expected, what happened, and attach `%LOCALAPPDATA%\PyBrowser\logs\pybrowser.log` (it contains no keys).
A screenshot helps most.

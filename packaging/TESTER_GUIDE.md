# PyBrowser - tester guide (Windows)

You have an **unsigned preview build**. It is real software that has not been through a wide audience yet.

## Install
* **Installer** (`PyBrowser-Setup-*.exe`): double-click. No admin rights needed; installs for your user only.
* **Portable** (`PyBrowser-Portable-*.zip`): unzip anywhere, run `PyBrowser.exe`. Nothing is installed.

Windows will say **"Windows protected your PC"** because the build is not code-signed. For this one file, click
**More info -> Run anyway**. Please do not turn Windows protection off. (The checksums in `SHA256.txt` let you confirm the
file is the one you were sent: `Get-FileHash .\PyBrowser-Setup-*.exe -Algorithm SHA256`.)

## What to expect
* It starts as an **ordinary browser**. AI features are **off** until you switch them on in **Settings -> AI features**.
* Everything lives under `%LOCALAPPDATA%\PyBrowser` (settings, history, logs in `logs\pybrowser.log`). Uninstalling the app does
  not delete it; delete that folder to reset. Saved API keys are in Windows Credential Manager - use *Remove stored key*
  in the provider dialog before uninstalling if you want them gone.
* Nothing is sent to any AI service unless you turn AI on and add your own key (or point it at a model on your own PC).

## Please try
1. Browse a few normal sites; open/close tabs; downloads (Ctrl+J); bookmarks; find in page (Ctrl+F).
2. Settings -> turn on AI features. Before adding a key, **AI Mode** (Ctrl+Shift+Space) offers an *offline sample result*
   so you can see what a finished comparison looks like. It is hand-written and says so.
3. Set up a provider: the dialog starts on **Gemini** (free tier; get a key at aistudio.google.com/apikey). Groq and OpenRouter also
   have free tiers; Ollama / LM Studio run a model on your own PC with no key. Press **Test Connection**.
4. Open two or three product pages in tabs, then **AI -> AI Team...**: *Compare products*, **Add tabs...**, write your requirements
   (one per line, e.g. `Under $200`), **Start team**. Open the cited sources and check a claim yourself.
5. Close PyBrowser mid-run and reopen it: Team -> History -> **Resume**.
6. Full checklist: `docs/WINDOWS_CLEAN_MACHINE_CHECKLIST.md`.

## Report problems
Say what you did, what you expected, what happened, and attach `%LOCALAPPDATA%\PyBrowser\logs\pybrowser.log` (it contains no keys).
A screenshot helps most.

"""Drive the agent against a REAL website.

What this proves and what it does not
-------------------------------------
This runs the real browser, the real tool layer and the real agent loop against
a live site. The model is scripted (``tests/fake_claude.py``) unless you pass
``--live``, so by default it exercises everything except the Claude API itself.

That separation is deliberate, because three different things can fail and they
should not be confused:

* **browser capability** - can Qt WebEngine load and render the site;
* **agent capability** - do the tools, the loop and the safety layer work on
  real-world markup rather than a tidy fixture;
* **network reach** - can this machine get to the site at all.

    python scripts/agent_smoke.py                       # scripted model
    python scripts/agent_smoke.py --url https://pypi.org
    python scripts/agent_smoke.py --live                # needs a real API key
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-agent-smoke-")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession, AgentState  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.profile import BrowserProfile  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, find_ref, says  # noqa: E402

DEFAULT_URL = "https://pypi.org/"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--live", action="store_true",
                        help="use the real Claude API (requires a configured key)")
    parser.add_argument("--task", default="Find the search box on this page and tell me its label.")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    profile = BrowserProfile(app)
    tabs = TabManager(profile, args.url)
    tabs.resize(1280, 900)
    tabs.show()
    browser = BrowserController(tabs)
    browser.open_tab().wait()

    print(f"Loading {args.url} …")
    loaded = browser.navigate(args.url).wait(60000)
    if loaded is None or not loaded.ok:
        detail = loaded.error.message if loaded and loaded.error else "no result"
        print(f"FAIL  the browser could not load {args.url}: {detail}")
        print("      If an unrelated HTTP client is also blocked for this host, this is a")
        print("      network restriction on this machine, not a browser or agent defect.")
        return 2
    print(f"PASS  browser loaded it: {loaded.page.title!r}")

    if args.live:
        from app.agent.claude_client import ClaudeClient
        from app.agent.keys import ApiKeyStore

        key = ApiKeyStore().get_key()
        if not key:
            print("FAIL  --live needs an API key (keyring or ANTHROPIC_API_KEY).")
            return 2
        transport = ClaudeClient(key, AgentConfig())
        script_note = "live Claude API"
    else:
        # A scripted three-step task: read the page, act on something real it
        # found there, then answer. The refs come from the live site's markup.
        def act(messages):
            try:
                ref = find_ref(messages, "searchbox")
            except AssertionError:
                try:
                    ref = find_ref(messages, "link")
                except AssertionError:
                    return says("I could not find an element to act on.")
            return calls("browser_scroll_to_element", {"ref": ref})

        transport = ScriptedClaude([calls("browser_get_page"), act, says("Done.")])
        script_note = "scripted model (the Claude API is NOT exercised)"

    print(f"Running the agent with the {script_note} …")
    session = AgentSession(browser, transport, AgentConfig(limits=ContextLimits()))
    events: list[str] = []
    problems: list[str] = []
    answers: list[str] = []
    session.activity.connect(lambda text: events.append(text))
    session.error.connect(problems.append)
    session.assistant_message.connect(answers.append)

    done = []
    session.finished.connect(lambda: done.append(True))
    session.send(args.task)

    expired = [False]
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(lambda: expired.__setitem__(0, True))
    guard.start(180000)
    while not done and not expired[0]:
        app.processEvents()
    guard.stop()

    print()
    for line in events:
        print(f"  → {line}")
    for answer in answers:
        print(f"  Claude: {answer}")
    for problem in problems:
        print(f"  error: {problem}")

    structure = browser.get_page_structure().wait()
    element_count = structure.data["structure"].element_count if structure.ok else 0

    print()
    ok = bool(done) and not problems and element_count > 0
    print(f"{'PASS' if ok else 'FAIL'}  agent ran against a real site "
          f"({element_count} interactive elements found, {len(events)} actions)")
    print(f"      state: {session.state} (expected {AgentState.IDLE})")
    session.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

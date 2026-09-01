"""The demonstration task: research a topic across several pages, then report.

    python scripts/agent_demo.py            # scripted model, no API key, free
    python scripts/agent_demo.py --live     # the real Claude API

This is the shape of task PyBrowser is being built for:

    "Find three sources about tidal power and summarise what they say."

The agent opens the index, follows each source into its own tab, reads them,
and answers. Every tool it uses is read-only - get_page, get_page_text,
list_tabs, open_tab, navigate. Nothing is clicked, submitted, bought or sent,
and that is a property of the demo, not a coincidence: the run prints the
sensitivity of every action it took so you can check.

By default the model is scripted (tests/fake_claude.py), so the run is
deterministic, offline and costs nothing - it demonstrates the *loop*, not the
model's prose. With --live the same loop drives the real model against the same
local pages.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-demo-")

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent import trace as tracing  # noqa: E402
from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession, StepState  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.profile import BrowserProfile  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402

TASK = ("Find the three sources listed on this page, read each one, and tell me "
        "what they say about tidal power. Mention where they disagree.")

#: The read-only tools this demo is allowed to need. Anything else appearing in
#: the trace is a finding, not a feature.
READ_ONLY = {
    "browser_get_page", "browser_get_page_text", "browser_list_tabs",
    "browser_find_elements", "browser_navigate", "browser_open_tab",
    "browser_select_tab",
}


def scripted(server):
    """A model that does the research, without being one."""
    from tests.fake_claude import calls, says

    sources = ["/research/one", "/research/two", "/research/three"]
    script = [calls("browser_get_page_text")]
    for path in sources:
        script.append(calls("browser_open_tab", {"url": server.url(path)}))
        script.append(calls("browser_get_page_text"))
    script.append(calls("browser_list_tabs"))
    script.append(says(
        "Three sources on tidal power.\n"
        "  * Barrages dam an estuary and are highly predictable - La Rance has "
        "run at 240 MW since 1966.\n"
        "  * Stream turbines need no dam and disturb the estuary far less, but "
        "each produces much less power - MeyGen totals 6 MW.\n"
        "  * They disagree on cost of scale versus habitat: the barrage source "
        "emphasises output, the turbine and environment sources emphasise the "
        "mudflats and the birds that feed on them."))
    return ScriptedClaudeFor(script)


def ScriptedClaudeFor(script):  # noqa: N802 - reads as a constructor at the call site
    from tests.fake_claude import ScriptedClaude

    return ScriptedClaude(script)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="use the real Claude API instead of a scripted model")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    server = FixtureServer()
    profile = BrowserProfile(app)
    tabs = TabManager(profile, server.base)
    tabs.resize(1200, 850)
    tabs.show()
    browser = BrowserController(tabs)
    browser.open_tab().wait()
    browser.navigate(server.url("/research")).wait(30000)

    if args.live:
        from app.agent.claude_client import ClaudeClient
        from app.agent.credentials import resolve

        credential = resolve()
        if not credential.available:
            print("--live needs a credential; run `ant auth login` or set "
                  "ANTHROPIC_API_KEY.")
            return 2
        print(f"model     : {AgentConfig.from_environment().model} "
              f"via {credential.describe()}")
        transport = ClaudeClient(credential, AgentConfig.from_environment())
    else:
        print("model     : scripted (offline, free). Pass --live for the real one.")
        transport = scripted(server)

    session = AgentSession(browser, transport, AgentConfig.from_environment())
    answers: list[str] = []
    problems: list[str] = []
    session.assistant_message.connect(answers.append)
    session.error.connect(problems.append)
    session.step_changed.connect(
        lambda step: print(f"  {_mark(step.state)} {step.description}"
                           f"{' - ' + step.detail if step.detail else ''}"))
    done: list[bool] = []
    session.finished.connect(lambda: done.append(True))

    print(f"task      : {TASK}\n")
    session.send(TASK)

    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(180000)
    while not done and not expired[0]:
        app.processEvents()
    timer.stop()

    print()
    for answer in answers:
        print(answer)

    # -- what the loop actually did ---------------------------------------
    used = sorted({event.detail.get("tool") for event in session.trace.events
                   if event.name == tracing.TOOL_STARTED and event.detail.get("tool")})
    unexpected = [name for name in used if name not in READ_ONLY]
    tabs_open = len(browser.list_tabs())

    print("\n" + "=" * 66)
    print(f"steps       : {len(session.steps)} "
          f"({sum(1 for s in session.steps if s.state == StepState.DONE)} completed)")
    print(f"tools used  : {', '.join(used) or 'none'}")
    print(f"tabs open   : {tabs_open}")
    print(f"model turns : {session.trace.count(tracing.MODEL_REQUESTED)}")
    # A scripted model reports no tokens; printing "$0.000" would imply the
    # loop was free rather than that nothing was measured.
    spend = (session.task_usage.summary(session.config.model)
             if session.task_usage.prompt_tokens else "not measured (scripted model)")
    print(f"tokens      : {spend}")
    print(f"approvals   : {session.trace.count(tracing.APPROVAL_REQUESTED)} requested")
    print(f"read-only   : {'yes' if not unexpected else 'NO - ' + ', '.join(unexpected)}")

    if problems:
        print(f"\nFAILED: {problems}")
    session.shutdown()
    server.stop()

    ok = bool(answers) and not problems and not unexpected and tabs_open >= 4
    print("\nDEMO PASSED" if ok else "\nDEMO FAILED")
    return 0 if ok else 1


def _mark(state: str) -> str:
    return {StepState.DONE: "[done]", StepState.RUNNING: "[ .. ]",
            StepState.FAILED: "[FAIL]", StepState.WAITING: "[ask ]",
            StepState.SKIPPED: "[skip]"}.get(state, "[    ]")


if __name__ == "__main__":
    raise SystemExit(main())

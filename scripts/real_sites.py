"""Browser + agent against real websites, with honest attribution of failures.

For each site this records, separately:

  * network reach   - can an unrelated HTTP client get to it at all
  * browser         - did Qt WebEngine load and render it
  * page inspection - did the structured representation come back usable
  * agent           - did the real agent loop drive it (scripted model)

Keeping those apart is the point. A site that fails the first check tells you
nothing about the browser, and a site the browser renders but the agent cannot
read is an agent problem, not a network one.

    python scripts/real_sites.py
    python scripts/real_sites.py --sites https://pypi.org https://proxy.golang.org
    python scripts/real_sites.py --live      # use the real Claude API

The model is scripted by default, so the Claude API is NOT exercised unless
--live is passed and a key is configured.
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-real-sites-")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession, AgentState  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.profile import BrowserProfile  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, find_ref, says  # noqa: E402

# Sites the brief asks about, plus the other real hosts this machine can reach.
DEFAULT_SITES = [
    "https://pypi.org/",
    "https://proxy.golang.org/",
    "https://index.crates.io/",
    "https://www.wikipedia.org/",
    "https://www.google.com/",
    "https://www.youtube.com/",
    "https://www.reddit.com/",
]


def probe(url: str, timeout: int = 15) -> tuple[str, str]:
    """Can a client that shares none of the browser's code reach this URL?"""
    request = urllib.request.Request(url, headers={"User-Agent": "real-sites/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(200)
            if 200 <= response.status < 400:
                return "reachable", f"HTTP {response.status}"
            return "intercepted", f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return "intercepted", f"HTTP {exc.code} from an intermediary"
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "403" in reason or "tunnel" in reason.lower() or "CONNECT" in reason:
            return "blocked", "proxy refused CONNECT (403)"
        if isinstance(exc.reason, ssl.SSLError):
            return "blocked", f"TLS refused ({reason})"
        return "blocked", reason
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"{type(exc).__name__}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", nargs="*", default=DEFAULT_SITES)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    profile = BrowserProfile(app)
    tabs = TabManager(profile, "about:blank")
    tabs.resize(1280, 900)
    tabs.show()
    browser = BrowserController(tabs)
    browser.open_tab().wait()

    def pump(predicate, timeout_ms: int) -> bool:
        expired = [False]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: expired.__setitem__(0, True))
        timer.start(timeout_ms)
        while not predicate() and not expired[0]:
            app.processEvents()
        timer.stop()
        return predicate()

    rows = []
    for url in args.sites:
        print(f"\n=== {url}")
        reach, detail = probe(url)
        print(f"  network reach   : {reach} ({detail})")

        row = {"url": url, "reach": reach, "browser": "-", "inspection": "-",
               "agent": "-", "note": detail}

        loaded = browser.navigate(url).wait(60000)
        if loaded is None or not loaded.ok:
            problem = loaded.error.message if loaded and loaded.error else "no result"
            row["browser"] = "FAILED"
            if reach in ("blocked", "intercepted"):
                row["note"] = "Blocked by test environment"
                print(f"  browser         : did not load - {problem}")
                print("  VERDICT         : Blocked by test environment "
                      "(an unrelated client is blocked too)")
            else:
                row["note"] = f"BROWSER PROBLEM: {problem}"
                print(f"  browser         : FAILED although the host is reachable - {problem}")
            rows.append(row)
            continue

        row["browser"] = "loaded"
        print(f"  browser         : loaded, title {loaded.page.title!r}")

        structure_result = browser.get_page_structure().wait(30000)
        if structure_result is None or not structure_result.ok:
            row["inspection"] = "FAILED"
            print("  page inspection : FAILED")
            rows.append(row)
            continue
        structure = structure_result.data["structure"]
        row["inspection"] = f"{structure.element_count} elements"
        print(f"  page inspection : {structure.element_count} interactive elements, "
              f"{len(structure.headings)} headings, {len(structure.forms)} forms, "
              f"{len(structure.text)} chars of text")

        # Drive the real agent loop over this real page.
        if args.live:
            from app.agent.claude_client import ClaudeClient
            from app.agent.keys import ApiKeyStore

            key = ApiKeyStore().get_key()
            if not key:
                print("  agent           : --live needs an API key; skipping")
                rows.append(row)
                continue
            transport = ClaudeClient(key, AgentConfig())
        else:
            def act(messages):
                for role in ("link", "button", "searchbox", "textbox"):
                    try:
                        return calls("browser_scroll_to_element",
                                     {"ref": find_ref(messages, role)})
                    except AssertionError:
                        continue
                return says("Nothing interactive to act on.")

            transport = ScriptedClaude([calls("browser_get_page"), act,
                                        calls("browser_get_page_text"),
                                        says("Read the page.")])

        session = AgentSession(browser, transport, AgentConfig())
        events: list[str] = []
        problems: list[str] = []
        session.activity.connect(events.append)
        session.error.connect(problems.append)
        done = []
        session.finished.connect(lambda: done.append(True))
        session.send("Read this page and tell me what it is.")
        finished = pump(lambda: bool(done), 120000)
        session.shutdown()

        if finished and not problems:
            row["agent"] = f"{len(events)} actions"
            print(f"  agent           : {len(events)} actions - " + "; ".join(events))
        else:
            row["agent"] = "FAILED"
            print(f"  agent           : FAILED {problems}")
        rows.append(row)

    print("\n" + "=" * 78)
    print(f"{'SITE':34} {'REACH':12} {'BROWSER':9} {'INSPECTION':14} {'AGENT'}")
    print("-" * 78)
    for row in rows:
        print(f"{row['url'][:33]:34} {row['reach']:12} {row['browser']:9} "
              f"{row['inspection']:14} {row['agent']}")
    print("-" * 78)
    blocked = [r for r in rows if r["reach"] in ("blocked", "intercepted")]
    tested = [r for r in rows if r["browser"] == "loaded"]
    broken = [r for r in rows if r["browser"] == "FAILED" and r["reach"] == "reachable"]
    print(f"{len(tested)} actually tested, {len(blocked)} blocked by the test environment, "
          f"{len(broken)} genuine browser failures")
    for row in blocked:
        print(f"   Blocked by test environment: {row['url']}")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Frozen-app interaction self-test - diagnostic only, never active for a
real user.

Enabled by setting PYBROWSER_SELFTEST=1 in the process environment before
launch (see main.py's call to start_if_enabled()). Completely inert
otherwise: nothing here is imported, constructed, or reachable from any
menu, button, or shortcut a normal user could hit. This exists to answer
one question from outside a GUI session - whether the packaged/frozen
PyBrowser process survives sustained, realistic interaction (tab churn,
repeated AgentSession/Py-panel rebuilds, MCP settings lifecycle, dialog
open/close, workspace switching) and then quits through the SAME normal
shutdown path (MainWindow.closeEvent -> app.exec() returning ->
main.py's os._exit()) a person quitting the app would go through - not a
forced kill.

Deliberately drives MainWindow's own private lifecycle methods
(_toggle_agent_panel, _rebuild_agent, _show_settings, ...) rather than
simulating clicks: those methods ARE the code path a real click runs, and
they are exactly what earlier investigation traced the QThread/GC/Qt
lifetime hazards this test hunts for to (see app/agent/session.py's
AgentSession and app/ui/mcp_server_settings.py's gc.collect()).

Never makes a real network/API call: providers used to build an
AgentSession here are local-runtime providers (Ollama / Local
OpenAI-compatible), which app/agent/credentials.py explicitly treats as
"available" with no key configured - so a real AgentSession + its
QThread worker gets built and torn down repeatedly without ever needing a
credential, and this test never asks the session to actually run
anything, so no request is ever attempted against the fake endpoint.

Storage isolation is not this module's job: PYBROWSER_DATA_DIR (see
app/config.py) already redirects the database/profile/cache/log
directories, and the CI workflow that runs this sets it to a fresh temp
directory per launch - the same mechanism release.yml's own smoke test
already relies on.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

_ENV_ENABLE = "PYBROWSER_SELFTEST"
_ENV_DURATION = "PYBROWSER_SELFTEST_DURATION_SECONDS"
_DEFAULT_DURATION_SECONDS = 150.0
_TICK_MS = 300
_MODAL_CLOSE_DELAY_MS = 350

# Real QWebEngineView/QWebEnginePage navigations, but never touching the
# network - a CI runner with restricted/flaky internet must not turn a
# lifecycle bug into a false pass or a false failure.
_HARMLESS_PAGES = [
    "data:text/html,<html><body><h1>PyBrowser self-test page A</h1></body></html>",
    "data:text/html,<html><body><h1>PyBrowser self-test page B</h1></body></html>",
    "data:text/html,<html><body><h1>PyBrowser self-test page C</h1></body></html>",
]

# Dialogs that exist, take no destructive/precondition-gated action to
# open, and need no real external service. Collaboration is deliberately
# excluded - it requires an active Mission, which this test does not start.
_SAFE_DIALOG_OPENERS = [
    ("Highlights", "_show_highlights_library"),
    ("Skills", "_show_skills_library"),
    ("Watches", "_show_watches"),
    ("Research Graph", "_show_research_graph"),
    ("Sync", "_show_sync_settings"),
    ("About", "_show_about"),
]

_TAB_CYCLE_TARGET = 20  # 2 tabs opened + closed per cycle -> 40 create/close events
_SESSION_CYCLE_TARGET = 20
_MCP_CYCLE_TARGET = 20

_log = logging.getLogger("pybrowser.selftest")


def selftest_enabled() -> bool:
    return os.environ.get(_ENV_ENABLE) == "1"


def start_if_enabled(app: QApplication, window) -> "SelfTestDriver | None":
    """Build and start the driver if PYBROWSER_SELFTEST=1, else do nothing."""
    if not selftest_enabled():
        return None
    duration = _DEFAULT_DURATION_SECONDS
    raw = os.environ.get(_ENV_DURATION)
    if raw:
        try:
            duration = max(10.0, float(raw))
        except ValueError:
            pass
    driver = SelfTestDriver(app, window, duration_seconds=duration)
    driver.start()
    return driver


class SelfTestDriver:
    """A QTimer-driven state machine: one action per tick, so the real Qt
    event loop keeps processing normally between actions instead of being
    starved by a tight loop - see the module docstring's Phase F note."""

    def __init__(self, app: QApplication, window, duration_seconds: float) -> None:
        self.app = app
        self.window = window
        self.duration_seconds = duration_seconds
        self._deadline = time.monotonic() + duration_seconds
        self._timer = QTimer(window)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

        self._phase = "startup"
        self._pass_number = 0
        self._actions: list[Callable[[], None]] = []
        self._action_index = 0

        self._tab_cycles = 0
        self._session_cycles = 0
        self._mcp_cycles = 0
        self._dialog_cycles = 0
        self._workspace_cycles = 0

        self._session_seq = 0
        self._workspace_seq = 0
        self._selftest_workspace_ids: list[str] = []
        self._original_workspace_id = window._current_workspace_id

        self._failed = False
        self._failure_reason = ""
        self._finished = False

        print(
            f"[selftest] enabled: duration={duration_seconds:.0f}s "
            f"tab_target={_TAB_CYCLE_TARGET} session_target={_SESSION_CYCLE_TARGET} "
            f"mcp_target={_MCP_CYCLE_TARGET} dialogs={len(_SAFE_DIALOG_OPENERS)}",
            flush=True,
        )

    def start(self) -> None:
        self._start_pass()
        self._timer.start()

    # -- pass construction -------------------------------------------------

    def _start_pass(self) -> None:
        self._pass_number += 1
        self._actions = self._build_pass_actions()
        self._action_index = 0
        print(f"[selftest] starting pass {self._pass_number} "
              f"({len(self._actions)} actions)", flush=True)

    def _build_pass_actions(self) -> list[Callable[[], None]]:
        actions: list[Callable[[], None]] = []

        actions.append(lambda: setattr(self, "_phase", "A-tab-churn"))
        for _ in range(_TAB_CYCLE_TARGET):
            actions.append(self._tab_cycle_once)

        actions.append(lambda: setattr(self, "_phase", "B-agent-session-churn"))
        for _ in range(_SESSION_CYCLE_TARGET):
            actions.append(self._session_cycle_once)

        actions.append(lambda: setattr(self, "_phase", "C-mcp-ui-lifecycle"))
        for _ in range(_MCP_CYCLE_TARGET):
            actions.append(self._mcp_cycle_once)

        actions.append(lambda: setattr(self, "_phase", "D-dialogs"))
        for name, method_name in _SAFE_DIALOG_OPENERS:
            actions.append(functools.partial(self._dialog_cycle_once, name, method_name))

        actions.append(lambda: setattr(self, "_phase", "E-workspaces"))
        actions.extend(self._workspace_phase_actions())

        return actions

    # -- Phase A: tab churn --------------------------------------------------

    def _tab_cycle_once(self) -> None:
        self._tab_cycles += 1
        url = _HARMLESS_PAGES[self._tab_cycles % len(_HARMLESS_PAGES)]
        self.window.tabs.new_tab(url)
        self.window.tabs.new_tab(url, background=True)
        for i in range(self.window.tabs.count()):
            self.window.tabs.setCurrentIndex(i)
        for _ in range(2):
            if self.window.tabs.count() > 1:
                self.window.tabs.close_tab(self.window.tabs.count() - 1)

    # -- Phase B: Py panel / AgentSession churn -----------------------------

    def _session_cycle_once(self) -> None:
        self._session_cycles += 1
        from app.agent.config import (
            KEY_AGENT_PROVIDER, PROVIDER_LOCAL_OPENAI, PROVIDER_OLLAMA,
            local_endpoint_settings_key, model_settings_key,
        )

        self._session_seq += 1
        provider = PROVIDER_LOCAL_OPENAI if self._session_seq % 2 else PROVIDER_OLLAMA
        self.window.settings.set(KEY_AGENT_PROVIDER, provider)
        self.window.settings.set(model_settings_key(provider), f"selftest-model-{self._session_seq}")
        self.window.settings.set(local_endpoint_settings_key(provider), "http://127.0.0.1:1/")

        if self.window._side_panel is None:
            self.window._toggle_agent_panel()
        # Unconditionally tears down the old AgentSession (QThread.quit()+
        # wait()) and, since the panel is showing, builds a fresh one under
        # the just-changed settings - the same call _apply_agent_settings()
        # makes after a real provider/model change in the UI.
        self.window._rebuild_agent("selftest rebuild")

    # -- Phase C: MCP settings / external-access UI lifecycle ---------------

    def _mcp_cycle_once(self) -> None:
        self._mcp_cycles += 1
        self._exec_dialog_and_autoclose(self.window._show_settings)

    # -- Phase D: other major dialogs ----------------------------------------

    def _dialog_cycle_once(self, name: str, method_name: str) -> None:
        self._dialog_cycles += 1
        method = getattr(self.window, method_name, None)
        if method is None:
            print(f"[selftest] dialog opener missing: {method_name} ({name})", flush=True)
            return
        self._exec_dialog_and_autoclose(method)

    def _exec_dialog_and_autoclose(self, opener: Callable[[], None]) -> None:
        """Schedule a close of whatever modal dialog is active shortly after
        calling ``opener`` - opener typically both builds AND .exec()s the
        dialog in one call, so this timer fires from inside that nested Qt
        event loop and lets opener() return normally, going through the
        dialog's own real close path rather than a forced destroy."""
        QTimer.singleShot(_MODAL_CLOSE_DELAY_MS, self._close_active_modal)
        opener()

    @staticmethod
    def _close_active_modal() -> None:
        widget = QApplication.activeModalWidget()
        if widget is not None:
            widget.close()

    # -- Phase E: workspaces --------------------------------------------------

    def _workspace_phase_actions(self) -> list[Callable[[], None]]:
        from app.workspaces.model import new_workspace

        def create_ws() -> None:
            self._workspace_seq += 1
            self._workspace_cycles += 1
            ws = new_workspace(f"selftest-{self._workspace_seq}")
            self.window.workspaces.save(ws)
            self._selftest_workspace_ids.append(ws.id)

        def switch_to(index: int) -> Callable[[], None]:
            def _do() -> None:
                wid = self._selftest_workspace_ids[index]
                self.window.switch_workspace(wid)
                self.window.tabs.new_tab(_HARMLESS_PAGES[0])
            return _do

        def switch_back() -> None:
            self.window.switch_workspace(self._original_workspace_id)

        def cleanup() -> None:
            for wid in self._selftest_workspace_ids:
                if wid != self._original_workspace_id:
                    self.window.workspaces.delete(wid)
            self._selftest_workspace_ids.clear()

        return [create_ws, create_ws, switch_to(0), switch_to(1), switch_back, cleanup]

    # -- driving loop ---------------------------------------------------------

    def _tick(self) -> None:
        if self._finished:
            return
        if time.monotonic() >= self._deadline:
            self._finish(success=True)
            return
        if self._action_index >= len(self._actions):
            self._start_pass()
            return
        action = self._actions[self._action_index]
        self._action_index += 1
        try:
            action()
        except Exception:  # noqa: BLE001 - a self-test action failing IS the finding
            self._failure_reason = traceback.format_exc()
            print("[selftest] ACTION FAILED - see traceback below", flush=True)
            print(self._failure_reason, file=sys.stderr, flush=True)
            _log.error("selftest action failed in phase %s: %s", self._phase, self._failure_reason)
            self._finish(success=False)
            return
        if self._action_index % 25 == 0:
            print(
                f"[selftest] phase={self._phase} pass={self._pass_number} "
                f"tab_cycles={self._tab_cycles} session_cycles={self._session_cycles} "
                f"mcp_cycles={self._mcp_cycles} dialog_cycles={self._dialog_cycles} "
                f"workspace_cycles={self._workspace_cycles} "
                f"elapsed={time.monotonic() - (self._deadline - self.duration_seconds):.0f}s",
                flush=True,
            )

    def _finish(self, *, success: bool) -> None:
        self._finished = True
        self._failed = not success
        self._timer.stop()

        print(
            f"[selftest] finished: phase_reached={self._phase} pass={self._pass_number} "
            f"tab_cycles={self._tab_cycles} session_cycles={self._session_cycles} "
            f"mcp_cycles={self._mcp_cycles} dialog_cycles={self._dialog_cycles} "
            f"workspace_cycles={self._workspace_cycles} "
            f"result={'OK' if success else 'FAILED'}",
            flush=True,
        )

        # Phase G: the SAME normal shutdown path a person quitting the app
        # uses - MainWindow.closeEvent() (panel/session/MCP teardown, tab
        # deleteLater()) runs synchronously inside close(), then the event
        # loop is asked to quit so app.exec() returns and main.py's own
        # os._exit() runs. Never Stop-Process/kill/SIGTERM/SIGKILL - if this
        # does not return on its own, the workflow's watchdog timeout (not
        # this code) is what ends the run, and that is a FAILED run.
        self.window.close()

        # Only meaningful AFTER close() returns: closeEvent() runs
        # synchronously inside it (AgentSession.shutdown()'s QThread.quit()+
        # wait(), McpConnectionManager.shutdown()'s thread join, etc.) - a
        # snapshot taken before close() would just show every background
        # thread the running app is *supposed* to have, not a leak.
        threads = [t.name for t in threading.enumerate()]
        print(f"[selftest] active threads after normal shutdown: {threads}", flush=True)

        if not success:
            QApplication.instance().exit(1)
        else:
            QTimer.singleShot(200, lambda: QApplication.instance().quit())

"""McpConnectionManager's shutdown/asyncio-lifecycle contract.

Background: the manager owns one background thread (name "mcp-io") running
its own dedicated asyncio event loop for the lifetime of the manager. Before
this test file, shutdown() scheduled cleanup and a stop() fire-and-forget,
never actually waiting for the loop to stop, the thread to exit, or the loop
to close - confirmed directly (via PYBROWSER_MCP_SHUTDOWN_DIAG and
qInstallMessageHandler-style instrumentation during this investigation) to
leave the background thread, in-flight subprocesses, and asyncio's own
per-subprocess reaper threads (_do_waitpid) still alive well past shutdown()
returning, causing hangs and native crashes under enough test-suite load
(and reported as native crashes in test_mcp_phase2.py/test_mcp_phase3.py on
Windows).

Every test here calls shutdown() and then asserts on its aftermath directly:
the background thread is gone (checked by name, not just "the test didn't
hang"), the loop is closed, and no new work can be scheduled - rather than
merely "the test didn't crash", which the old fire-and-forget shutdown()
could pass by accident on a fast/idle machine.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_connection_manager_shutdown -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mcp-shutdown-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.mcp.config import McpServerStore  # noqa: E402
from app.mcp.connection_manager import McpConnectionManager  # noqa: E402
from app.mcp.types import ConnectionState, McpServerConfig, Transport  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402

_SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def pump(predicate, timeout_ms: int = 5000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _mcp_io_threads() -> list[threading.Thread]:
    """Every currently-alive thread this manager's own background loop
    could be - by name, not by counting all threads (the process may have
    other, unrelated background threads of its own)."""
    return [t for t in threading.enumerate() if t.name == "mcp-io"]


def _assert_no_mcp_io_threads(test: unittest.TestCase, timeout_s: float = 2.0) -> None:
    """shutdown()'s own thread.join() already confirms is_alive() is False
    before returning, but CPython removes a finished thread from
    threading.enumerate()'s bookkeeping a hair after the join() lock
    itself releases - polls briefly rather than asserting instantly, to
    avoid a flaky failure over that harmless internal gap."""
    deadline = time.monotonic() + timeout_s
    threads = _mcp_io_threads()
    while threads and time.monotonic() < deadline:
        time.sleep(0.01)
        threads = _mcp_io_threads()
    test.assertEqual(threads, [])


class McpShutdownTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.settings = SettingsStore(self.db)
        self.store = McpServerStore(self.settings)
        self.manager = McpConnectionManager(self.store)

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.db.close()
        os.unlink(self._tmp.name)

    def add_stdio_server(self, server_id: str = "fake", *, args: tuple = (),
                         env: dict | None = None, connect_timeout_s: float = 10.0) -> McpServerConfig:
        config = McpServerConfig(
            id=server_id, name="Fake MCP", transport=Transport.STDIO,
            enabled=True, command=sys.executable, args=(_SERVER_SCRIPT, *args),
            env=dict(env or {}), connect_timeout_s=connect_timeout_s)
        self.manager.add_or_update_server(config)
        return config

    def connect_and_wait(self, server_id: str = "fake", timeout_ms: int = 5000) -> None:
        self.manager.connect_server(server_id)
        ok = pump(lambda: self.manager.state(server_id) in
                  (ConnectionState.CONNECTED, ConnectionState.ERROR), timeout_ms)
        self.assertTrue(ok, "connection never settled")


class NoConnectionsTests(McpShutdownTestCase):
    def test_shutdown_with_no_connections_stops_the_thread_and_closes_the_loop(self):
        self.manager.shutdown()
        _assert_no_mcp_io_threads(self)
        self.assertIsNone(self.manager._loop)
        self.assertIsNone(self.manager._thread)


class IdempotencyTests(McpShutdownTestCase):
    def test_shutdown_called_twice_is_a_harmless_no_op(self):
        self.manager.shutdown()
        self.manager.shutdown()  # must not raise, hang, or double-join
        _assert_no_mcp_io_threads(self)

    def test_close_after_a_failed_startup_is_safe(self):
        """_loop can legitimately still be None if the background thread
        never signalled ready (e.g. it crashed before setting the event) -
        shutdown() must be a no-op then, not raise on a None loop/thread.

        Nulling out the real manager's _loop/_thread from setUp() to
        simulate this would orphan its real, already-running background
        thread (shutdown() would then have nothing to tell to stop) -
        leaking it for the rest of the process. Testing against a
        second, never-started instance instead."""
        cold = McpConnectionManager.__new__(McpConnectionManager)
        cold._loop = None
        cold._thread = None
        cold._shutdown_lock = threading.Lock()
        cold._shutdown_done = False
        cold._shutting_down = False
        cold._gui_dispatcher = self.manager._gui_dispatcher.__class__()
        cold._connections = {}
        cold.shutdown()  # must not raise
        cold._gui_dispatcher.shutdown()


class ActiveStdioConnectionTests(McpShutdownTestCase):
    def test_shutdown_with_an_active_stdio_subprocess_reaps_it_and_exits_cleanly(self):
        self.add_stdio_server()
        self.connect_and_wait()
        connection = self.manager.connection("fake")
        self.assertEqual(connection.state, ConnectionState.CONNECTED)
        process = connection.client._process
        self.assertIsNone(process.returncode, "the fake server should still be running")

        self.manager.shutdown()

        _assert_no_mcp_io_threads(self)
        self.assertIsNotNone(process.returncode, "the subprocess must be reaped by shutdown")

    def test_shutdown_during_an_in_flight_connect_cancels_it_and_still_exits(self):
        """A command that never speaks the protocol (here: sleep) keeps
        the connect attempt pending indefinitely on its own - shutdown()
        must not wait for that attempt's own (long) connect_timeout_s."""
        # A command that never speaks the protocol, so the connect is
        # provably still in flight when shutdown() is called below.
        slow_config = McpServerConfig(
            id="slow", name="Slow MCP", transport=Transport.STDIO,
            enabled=True, command="sleep", args=("30",), connect_timeout_s=30.0)
        self.manager.add_or_update_server(slow_config)
        self.manager.connect_server("slow")
        self.assertEqual(self.manager.state("slow"), ConnectionState.CONNECTING)
        # Give the connect attempt at least one real turn of the loop, so
        # it is genuinely in flight (subprocess spawned, awaiting the
        # handshake) rather than still suspended inside asyncio's own
        # subprocess-creation machinery - cancelling exactly there is a
        # separate, narrow CPython asyncio edge case (see the comment on
        # _connect_and_discover's except CancelledError branch), not
        # something this test is about.
        pump(lambda: False, 200)

        started = time.monotonic()
        self.manager.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 15.0,
                        "shutdown() must not block for the connect attempt's own timeout")
        _assert_no_mcp_io_threads(self)

    def test_shutdown_with_a_pending_tool_call_still_exits(self):
        self.add_stdio_server(env={"FAKE_MCP_HANG_ON_CALL": "1"})
        self.connect_and_wait()
        self.manager.run_tool("mcp.fake.echo", {"phrase": "hi"})

        started = time.monotonic()
        self.manager.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 15.0)
        _assert_no_mcp_io_threads(self)


class ConnectionFailureTests(McpShutdownTestCase):
    def test_shutdown_after_a_connection_failure_is_clean(self):
        config = McpServerConfig(
            id="missing", name="Missing", transport=Transport.STDIO,
            enabled=True, command="pybrowser-does-not-exist-anywhere", args=())
        self.manager.add_or_update_server(config)
        self.manager.connect_server("missing")
        ok = pump(lambda: self.manager.state("missing") == ConnectionState.ERROR)
        self.assertTrue(ok)

        self.manager.shutdown()
        _assert_no_mcp_io_threads(self)

    def test_a_late_stage_failure_after_connect_succeeded_does_not_leak_the_subprocess(self):
        """connect() itself succeeding but a later step (list_tools here,
        via an intentionally too-short connect_timeout_s) failing must
        still close the client - not just report the error. Without this,
        the subprocess (and asyncio's own per-subprocess reaper thread)
        leaks for the life of the process; see _close_failed_client."""
        self.add_stdio_server(connect_timeout_s=0.001)
        self.manager.connect_server("fake")
        ok = pump(lambda: self.manager.state("fake") == ConnectionState.ERROR, 5000)
        self.assertTrue(ok, self.manager.connection("fake").last_error)
        self.assertIsNone(self.manager.connection("fake").client)

        self.manager.shutdown()
        _assert_no_mcp_io_threads(self)


class NoWorkAfterShutdownTests(McpShutdownTestCase):
    def test_connect_server_after_shutdown_is_refused(self):
        self.add_stdio_server()
        self.manager.shutdown()
        self.manager.connect_server("fake")  # must not raise or spawn anything
        _assert_no_mcp_io_threads(self)

    def test_run_tool_after_shutdown_resolves_with_an_error_not_a_hang(self):
        self.manager.shutdown()
        future = self.manager.run_tool("mcp.fake.echo", {"phrase": "hi"})
        result = future.wait(2000)
        self.assertFalse(result["ok"])


class ReconnectDuringShutdownTests(McpShutdownTestCase):
    def test_shutdown_during_reconnect_is_clean(self):
        self.add_stdio_server()
        self.connect_and_wait()
        self.manager.reconnect_server("fake")  # drops+reconnects, still in flight
        # See test_shutdown_during_an_in_flight_connect_cancels_it_and_
        # still_exits for why this pump matters: cancelling a connect
        # attempt with zero event-loop turns since it started can land
        # inside asyncio's own subprocess-creation machinery, a separate,
        # narrow CPython edge case this test isn't about.
        pump(lambda: False, 200)
        self.manager.shutdown()
        _assert_no_mcp_io_threads(self)


if __name__ == "__main__":
    unittest.main()

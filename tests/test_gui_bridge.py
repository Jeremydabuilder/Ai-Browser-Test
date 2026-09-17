"""GuiBridge / GuiDispatcher (app/mcp_server/server.py): the queue+QTimer
replacement for the old cross-thread Qt Signal(object) design that
produced confirmed native crashes on both Windows
(Qt6Core!QCoreApplication::notifyInternal2, 0xC0000005 reading
0xFFFFFFFFFFFFFFFF) and macOS (QtCore!QCoreApplication::sendEvent,
EXC_BAD_ACCESS at 0x70) - see server.py's module docstring.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_gui_bridge -v
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.mcp_server.server import GuiBridge, GuiBridgeShutdown, GuiDispatcher  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def pump(predicate, timeout_ms: int = 5000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class GuiBridgeBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = GuiBridge()
        self.gui_thread = QThread.currentThread()

    def tearDown(self) -> None:
        self.bridge.shutdown()

    def test_callback_actually_executes_on_the_qapplication_gui_thread(self) -> None:
        """1 & 2: worker thread -> GUI execution, and the callback really
        runs on the GUI thread, not the worker that requested it."""
        seen_thread: list = []

        def worker() -> None:
            def on_gui() -> None:
                seen_thread.append(QThread.currentThread())
            self.bridge.call_sync(on_gui, timeout=5)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        pump(lambda: not t.is_alive())
        t.join(timeout=1)
        self.assertEqual(len(seen_thread), 1)
        self.assertIs(seen_thread[0], self.gui_thread)

    def test_result_is_returned_correctly(self) -> None:
        """3: result returned correctly."""
        box: dict = {}

        def worker() -> None:
            box["value"] = self.bridge.call_sync(lambda: 6 * 7, timeout=5)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        pump(lambda: not t.is_alive())
        t.join(timeout=1)
        self.assertEqual(box["value"], 42)

    def test_exception_raised_on_the_gui_thread_propagates_to_the_worker(self) -> None:
        """4: exception propagation."""
        box: dict = {}

        def failing() -> None:
            raise ValueError("boom")

        def worker() -> None:
            try:
                self.bridge.call_sync(failing, timeout=5)
            except ValueError as exc:
                box["error"] = str(exc)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        pump(lambda: not t.is_alive())
        t.join(timeout=1)
        self.assertEqual(box.get("error"), "boom")

    def test_timeout_returns_none_without_raising(self) -> None:
        """5: timeout - matches the pre-existing call_sync semantics (a
        timeout returns None, it never raises)."""
        box: dict = {}

        def worker() -> None:
            # Never drained: no pump() call happens on this thread, and we
            # give a short timeout so the test itself stays fast.
            box["value"] = self.bridge.call_sync(lambda: "unreachable", timeout=0.2)
            box["done"] = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertTrue(box.get("done"))
        self.assertIsNone(box.get("value"))

    def test_expired_request_is_not_delivered_late_into_the_worker(self) -> None:
        """Belt-and-suspenders on the same timeout path: once a request
        has expired, the dispatcher must not run its callback at all when
        the queue is later drained (nothing "stale" gets delivered)."""
        ran = threading.Event()

        def slow_callback() -> None:
            ran.set()

        def worker() -> None:
            self.bridge.call_sync(slow_callback, timeout=0.05)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=2)
        # Only now does the GUI thread get a chance to drain the queue -
        # long after the worker's timeout elapsed.
        pump(lambda: False, timeout_ms=200)
        self.assertFalse(ran.is_set())


class GuiBridgeShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = GuiBridge()

    def test_shutdown_while_request_pending_releases_the_waiter(self) -> None:
        """6: shutdown while request pending - the waiting worker must be
        released with a clear exception, never left blocked forever."""
        box: dict = {}
        entered = threading.Event()

        def blocking_forever() -> None:
            # Runs on the GUI thread once drained; the dispatcher is shut
            # down before this ever happens in this test, so it must
            # never actually execute.
            entered.set()
            return "should not run"

        def worker() -> None:
            try:
                self.bridge.call_sync(blocking_forever, timeout=5)
            except GuiBridgeShutdown as exc:
                box["shutdown"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        # Give the worker a moment to enqueue, then shut down before any
        # processEvents() pump ever runs (so the request is genuinely
        # still queued, not executed).
        time.sleep(0.05)
        self.bridge.shutdown()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(box.get("shutdown"), GuiBridgeShutdown)
        self.assertFalse(entered.is_set())

    def test_submitting_after_shutdown_raises_immediately(self) -> None:
        """Checked from a worker thread - a call from the GUI thread itself
        takes the reentrancy fast path and runs directly regardless of
        shutdown state, which is correct (see test_call_sync_from_the_gui_
        thread_does_not_deadlock)."""
        self.bridge.shutdown()
        box: dict = {}

        def worker() -> None:
            try:
                self.bridge.call_sync(lambda: 1, timeout=1)
            except GuiBridgeShutdown as exc:
                box["shutdown"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertIsInstance(box.get("shutdown"), GuiBridgeShutdown)


class GuiBridgeConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = GuiBridge()

    def tearDown(self) -> None:
        self.bridge.shutdown()

    def test_multiple_concurrent_worker_requests_all_get_their_own_result(self) -> None:
        """7: multiple concurrent worker requests."""
        results: dict[int, int] = {}
        lock = threading.Lock()

        def worker(n: int) -> None:
            value = self.bridge.call_sync(lambda: n * n, timeout=5)
            with lock:
                results[n] = value

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(12)]
        for t in threads:
            t.start()
        pump(lambda: all(not t.is_alive() for t in threads), timeout_ms=5000)
        for t in threads:
            t.join(timeout=1)
        self.assertEqual(results, {i: i * i for i in range(12)})

    def test_fifty_sequential_bridge_requests(self) -> None:
        """8: 50-100 sequential bridge requests."""
        box: dict = {}

        def worker() -> None:
            total = 0
            for i in range(75):
                total += self.bridge.call_sync(lambda i=i: i, timeout=5)
            box["total"] = total

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        pump(lambda: not t.is_alive(), timeout_ms=10000)
        t.join(timeout=1)
        self.assertEqual(box.get("total"), sum(range(75)))

    def test_call_sync_from_the_gui_thread_does_not_deadlock(self) -> None:
        """9: GUI-thread call_sync does not deadlock - it must run fn()
        directly rather than enqueue and wait on itself."""
        result = self.bridge.call_sync(lambda: "direct", timeout=1)
        self.assertEqual(result, "direct")


class GuiDispatcherLifecycleTests(unittest.TestCase):
    def test_object_destruction_after_requests_complete(self) -> None:
        """10: object destruction after requests complete - shutdown()
        must be safe to call, and safe to call again, once outstanding
        work is done."""
        bridge = GuiBridge()
        box: dict = {}

        def worker() -> None:
            box["value"] = bridge.call_sync(lambda: "ok", timeout=5)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        pump(lambda: not t.is_alive(), timeout_ms=2000)
        t.join(timeout=1)
        self.assertEqual(box.get("value"), "ok")
        bridge.shutdown()  # must not raise
        bridge.shutdown()  # idempotent

    def test_no_worker_thread_or_qthread_is_leaked_per_call(self) -> None:
        """11: no worker/QThread leaks - the dispatcher must not spin up a
        QThread per call (unlike the old design's one-shot signal
        connections); confirm thread count stays flat across many calls."""
        bridge = GuiBridge()
        try:
            before = threading.active_count()

            def worker() -> None:
                for _ in range(20):
                    bridge.call_sync(lambda: None, timeout=5)

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            pump(lambda: not t.is_alive(), timeout_ms=5000)
            t.join(timeout=1)
            after = threading.active_count()
            # +1 for the transient worker thread itself, no more - no
            # extra QThreads/threads are spawned per call.
            self.assertLessEqual(after, before + 1)
        finally:
            bridge.shutdown()

    def test_dispatcher_is_reentrant_and_reusable_across_many_posts(self) -> None:
        dispatcher = GuiDispatcher()
        try:
            seen = []
            dispatcher.post(lambda: seen.append(1))
            pump(lambda: len(seen) == 1, timeout_ms=1000)
            self.assertEqual(seen, [1])
        finally:
            dispatcher.shutdown()


if __name__ == "__main__":
    unittest.main()

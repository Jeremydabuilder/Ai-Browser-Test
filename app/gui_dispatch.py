"""Shared background-thread -> GUI-thread dispatch primitive.

Used wherever a background thread (an HTTP handler, an asyncio I/O loop
running on its own ``threading.Thread``, ...) needs to run a callable on
the GUI thread and, usually, wait for its result.

This replaces an earlier design - used independently by both
``app.mcp_server.server.GuiBridge`` and ``app.mcp.connection_manager.
McpConnectionManager`` - where the background thread emitted a Qt
``Signal(object)`` carrying the callable itself across threads. That
design produced confirmed, reproducible native crashes on both platforms,
always on the GUI thread while Qt's own queued-event delivery ran:
Windows - ``Qt6Core!QCoreApplication::notifyInternal2``, access violation
(0xC0000005) reading address ``0xFFFFFFFFFFFFFFFF``; macOS -
``QtCore!QCoreApplication::sendEvent``, ``EXC_BAD_ACCESS`` at address
``0x70``. See .github/workflows/*-mcp-sequence-diag.yml for the native
backtraces that pinned this down.

Here, nothing Qt-shaped (no QObject, no Signal) ever crosses the thread
boundary - only a plain, thread-safe ``queue.Queue`` does, drained by a
GUI-owned ``QTimer``.
"""

from __future__ import annotations

import queue
import threading
import weakref
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer


class GuiDispatchShutdown(RuntimeError):
    """Raised to a caller blocked in ``run_sync`` when the dispatcher is
    shut down before its request ran - e.g. the application is closing.
    Never left unresolved: every request queued at shutdown time is
    released with this exception rather than left waiting forever."""


class _PendingCall:
    """One in-flight ``run_sync`` request. Holds nothing Qt-shaped - it
    crosses the worker/GUI boundary sitting in a plain ``queue.Queue``,
    which is fully thread-safe on its own and needs no Qt signal/slot
    machinery to move between threads."""

    __slots__ = ("fn", "event", "result", "exception", "expired")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.event = threading.Event()
        self.result: Any = None
        self.exception: BaseException | None = None
        self.expired = False


class GuiDispatcher(QObject):
    """Owned by and created on the GUI thread. Worker threads enqueue
    plain callables (or ``_PendingCall`` requests) onto a thread-safe
    ``queue.Queue``; a GUI-owned ``QTimer`` drains that queue and runs
    each item directly on the GUI thread's own event loop. Nothing
    Qt-shaped (no QObject, no Signal) ever crosses the thread boundary -
    only the queue itself does.

    A dispatcher is parented to the process' ``QCoreApplication`` by
    default. That Qt/C++ ownership is intentional and important: the
    dispatcher owns a live ``QTimer``, so its destruction must be governed
    by the GUI-thread QObject tree rather than by an unpredictable Python
    cyclic-GC sweep that may happen on a worker thread. Callers may supply
    another long-lived GUI-thread QObject parent explicitly, but temporary
    dialogs/workers are not appropriate owners.
    """

    def __init__(self, poll_interval_ms: int = 5, parent: QObject | None = None) -> None:
        if parent is None:
            parent = QCoreApplication.instance()
            if parent is None:
                raise RuntimeError(
                    "GuiDispatcher requires a QCoreApplication/QApplication instance"
                )

        # Fail before constructing any QObject/timer if a caller tries to
        # create the dispatcher from a worker thread. Parenting a QObject
        # to a GUI-thread object from another thread is invalid Qt usage and
        # would put us straight back into the lifetime/thread-affinity class
        # of bugs this primitive exists to avoid.
        if QThread.currentThread() != parent.thread():
            raise RuntimeError("GuiDispatcher must be created on its Qt parent's thread")

        super().__init__(parent)
        self._queue: "queue.Queue[_PendingCall | Callable[[], None]]" = queue.Queue()
        self._gui_thread_ident = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)

        # Keep the timeout callback weak so Qt's connection bookkeeping does
        # not itself introduce a Python self-cycle. The *lifetime* guarantee,
        # however, comes from the QObject parent tree above: QApplication ->
        # GuiDispatcher -> QTimer. In particular, dropping the last ordinary
        # Python reference does not make worker-thread cyclic GC responsible
        # for tearing down a live GUI timer.
        weak_self = weakref.ref(self)

        def _tick() -> None:
            instance = weak_self()
            if instance is not None:
                instance._drain()

        self._timer.timeout.connect(_tick)
        self._timer.start()

    @property
    def is_gui_thread(self) -> bool:
        return threading.get_ident() == self._gui_thread_ident

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _PendingCall):
                self._run_pending(item)
            else:
                item()

    def _run_pending(self, pending: "_PendingCall") -> None:
        if pending.expired:
            return
        try:
            pending.result = pending.fn()
        except Exception as exc:  # noqa: BLE001 - propagated to the waiter
            pending.exception = exc
        finally:
            pending.event.set()

    def run_sync(self, fn: Callable[[], Any], timeout: float) -> Any:
        if self.is_gui_thread:
            return fn()
        with self._lock:
            if self._closed:
                raise GuiDispatchShutdown("GUI dispatcher is shut down")
        pending = _PendingCall(fn)
        self._queue.put(pending)
        finished = pending.event.wait(timeout)
        if not finished:
            pending.expired = True
            return None
        if pending.exception is not None:
            raise pending.exception
        return pending.result

    def post(self, fn: Callable[[], None]) -> None:
        if self.is_gui_thread:
            fn()
            return
        with self._lock:
            if self._closed:
                raise GuiDispatchShutdown("GUI dispatcher is shut down")
        self._queue.put(fn)

    def shutdown(self) -> None:
        if not self.is_gui_thread:
            raise RuntimeError("GuiDispatcher.shutdown() must run on the GUI thread")
        with self._lock:
            self._closed = True
        self._timer.stop()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _PendingCall):
                item.exception = GuiDispatchShutdown("GUI dispatcher is shut down")
                item.event.set()

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

from PySide6.QtCore import QObject, QTimer


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

    One instance is meant to live for the lifetime of its owner (created
    once, e.g. in that owner's ``__init__``), independent of any
    start()/stop() cycles that owner itself goes through - see
    ``shutdown()`` below for the separate, permanent teardown path meant
    to be used at application close.
    """

    def __init__(self, poll_interval_ms: int = 5) -> None:
        super().__init__()
        self._queue: "queue.Queue[_PendingCall | Callable[[], None]]" = queue.Queue()
        self._gui_thread_ident = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        # Connecting a bound method (self._drain) directly here would hold
        # a strong Python reference back to self from Qt's own connection
        # bookkeeping - self -> _timer -> connection -> bound method ->
        # self - a reference cycle only the cyclic GC can break. Since
        # Python's cyclic GC can run on ANY thread (whichever one happens
        # to trip the collection threshold), a dropped-but-cyclic
        # GuiDispatcher could then have its QTimer torn down from a
        # background worker thread instead of the GUI thread it belongs
        # to - reproduced locally as "QBasicTimer::stop: Failed. Possibly
        # trying to stop from a different thread" followed by a segfault.
        # A weakref callback breaks the cycle: refcounting alone then
        # collects a dropped GuiDispatcher immediately, on whichever
        # thread drops its last real reference - deterministic, no
        # GC-thread ambiguity.
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
            # The waiter already gave up (timeout elapsed) and is no
            # longer looking at this object - don't run a callback whose
            # receiver (e.g. a since-closed dialog) may no longer exist.
            return
        try:
            pending.result = pending.fn()
        except Exception as exc:  # noqa: BLE001 - propagated to the waiter
            pending.exception = exc
        finally:
            pending.event.set()

    def run_sync(self, fn: Callable[[], Any], timeout: float) -> Any:
        """Run ``fn`` on the GUI thread and block until it returns, or
        ``timeout`` elapses (in which case ``None`` is returned - no
        exception is raised for an ordinary timeout).

        Reentrant: if already called from the GUI thread, runs ``fn``
        directly instead of enqueuing - enqueuing would deadlock, since
        the GUI thread would then be waiting on the very queue only its
        own (blocked) self could ever drain.
        """
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
        """Fire-and-forget: run ``fn`` on the GUI thread without waiting
        for it to finish. Also reentrant (runs directly if already on the
        GUI thread)."""
        if self.is_gui_thread:
            fn()
            return
        with self._lock:
            if self._closed:
                raise GuiDispatchShutdown("GUI dispatcher is shut down")
        self._queue.put(fn)

    def shutdown(self) -> None:
        """Permanently stop accepting new work and release every request
        already queued with ``GuiDispatchShutdown``, so no worker thread
        is ever left waiting forever. Must be called on the GUI thread
        (it stops this dispatcher's own QTimer). This is a one-way
        teardown - not meant to be paired with any "restart"."""
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
            # A fire-and-forget callable has no waiter to release - drop it.

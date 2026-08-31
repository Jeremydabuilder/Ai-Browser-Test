"""A small future type for the asynchronous browser API.

Qt WebEngine is asynchronous end to end: navigation, JavaScript evaluation and
DOM extraction all complete on a later turn of the event loop. The API does not
pretend otherwise. Every BrowserController operation returns a ``BrowserFuture``
that resolves exactly once, and the caller picks how to observe it:

    controller.click(ref).then(lambda result: ...)      # callback
    controller.click(ref).finished.connect(handler)     # Qt signal
    result = controller.click(ref).wait()               # scripts and tests only

``wait()`` spins a nested QEventLoop. That is the right tool for a test or a
one-off script and the wrong tool inside a GUI slot, where re-entering the event
loop invites reentrancy bugs - so it is documented as such rather than being the
default. A future agent driving the browser should use ``then()``.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal


class BrowserFuture(QObject):
    """A one-shot result holder with callback, signal and blocking access."""

    finished = Signal(object)

    def __init__(self, action: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.action = action
        self._done = False
        self._result: Any = None
        self._callbacks: list[Callable[[Any], None]] = []
        self._timeout_timer: QTimer | None = None
        self._on_timeout: Callable[[], Any] | None = None

    # -- producer side ---------------------------------------------------
    def set_result(self, result: Any) -> None:
        """Resolve the future. Later calls are ignored, not an error.

        Ignoring repeats matters: a click can plausibly be resolved by both a
        navigation signal and a settle timer racing each other, and whichever
        arrives first should win quietly.
        """
        if self._done:
            return
        self._done = True
        self._result = result
        self._cancel_timeout()
        for callback in self._callbacks:
            callback(result)
        self._callbacks.clear()
        self.finished.emit(result)

    def set_timeout(self, milliseconds: int, factory: Callable[[], Any]) -> None:
        """Resolve with ``factory()`` if nothing else resolves us in time.

        Every asynchronous operation gets one of these, so a page that never
        finishes loading produces a TIMEOUT result rather than a caller that
        waits for ever.
        """
        self._cancel_timeout()
        self._on_timeout = factory
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._fire_timeout)
        timer.start(max(1, milliseconds))
        self._timeout_timer = timer

    def _fire_timeout(self) -> None:
        if self._done or self._on_timeout is None:
            return
        self.set_result(self._on_timeout())

    def _cancel_timeout(self) -> None:
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer.deleteLater()
            self._timeout_timer = None

    # -- consumer side ---------------------------------------------------
    @property
    def done(self) -> bool:
        return self._done

    def result(self) -> Any:
        """The result, or None if it has not resolved yet."""
        return self._result

    def then(self, callback: Callable[[Any], None]) -> "BrowserFuture":
        """Run ``callback`` with the result; immediately if already resolved."""
        if self._done:
            callback(self._result)
        else:
            self._callbacks.append(callback)
        return self

    def wait(self, timeout_ms: int = 30000) -> Any:
        """Block until resolved and return the result.

        For tests and scripts. Do not call this from a GUI slot.
        """
        if self._done:
            return self._result
        loop = QEventLoop()
        self.finished.connect(lambda _result: loop.quit())
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(max(1, timeout_ms))
        loop.exec()
        guard.stop()
        return self._result


def resolved(action: str, value: Any) -> BrowserFuture:
    """A future that is already finished - for failures detected synchronously."""
    future = BrowserFuture(action)
    future.set_result(value)
    return future

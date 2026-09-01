"""Tracking downloads, without any UI attached.

Qt gives us a `QWebEngineDownloadRequest` per download and then talks to us in
signals. This module turns that into something a list widget can render and a
test can assert on: a stable list of `DownloadItem`s, each a plain snapshot of
one download, plus signals saying when one appeared or changed.

Deliberately in-memory only. Chromium cannot resume a download across a
restart, so a persisted list would be a list of things you can no longer do
anything about; the files themselves are on disk, which is where a finished
download actually lives. This is a real limitation of the engine, stated rather
than papered over with a history list that pretends otherwise.

Nothing here fabricates progress. If Qt does not know the total size - which is
common, a server need not send Content-Length - `percent` is None and the UI
shows bytes received instead of a progress bar that lies.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtWebEngineCore import QWebEngineDownloadRequest

_STATE_NAMES = {
    QWebEngineDownloadRequest.DownloadState.DownloadRequested: "requested",
    QWebEngineDownloadRequest.DownloadState.DownloadInProgress: "in_progress",
    QWebEngineDownloadRequest.DownloadState.DownloadCompleted: "completed",
    QWebEngineDownloadRequest.DownloadState.DownloadCancelled: "cancelled",
    QWebEngineDownloadRequest.DownloadState.DownloadInterrupted: "interrupted",
}


def human_size(count: int) -> str:
    """Bytes as something a person reads at a glance."""
    if count < 1024:
        return f"{count} B"
    size = float(count)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}".replace(".0 ", " ")
    return f"{size:.1f} PB"


@dataclass
class DownloadItem:
    """One download, as the UI sees it. Holds no Qt object."""

    id: int
    file_name: str
    directory: str
    url: str
    state: str = "requested"
    received: int = 0
    total: int = 0
    #: Why an interrupted download stopped, in Qt's words.
    reason: str = ""

    @property
    def finished(self) -> bool:
        return self.state in ("completed", "cancelled", "interrupted")

    @property
    def percent(self) -> int | None:
        """0-100, or None when the size is genuinely unknown.

        None is not the same as 0. A server that sends no Content-Length gives
        us no way to know how far along we are, and a progress bar filling up
        on invented numbers is worse than one that says "downloading".
        """
        if self.total <= 0:
            return None
        return min(100, int(self.received * 100 / self.total))

    def describe(self) -> str:
        if self.state == "completed":
            return f"Completed · {human_size(self.received)}"
        if self.state == "cancelled":
            return "Cancelled"
        if self.state == "interrupted":
            return f"Failed · {self.reason}" if self.reason else "Failed"
        share = self.percent
        if share is None:
            return f"Downloading · {human_size(self.received)}"
        return f"Downloading · {share}% of {human_size(self.total)}"


class DownloadManager(QObject):
    """Accepts downloads and keeps a live list of them.

    The profile hands each `QWebEngineDownloadRequest` here. We keep the Qt
    object (cancelling needs it) but never hand it out; callers get
    `DownloadItem` snapshots and act through `cancel(id)`.
    """

    started = Signal(object)     # DownloadItem
    changed = Signal(object)     # DownloadItem
    finished = Signal(object)    # DownloadItem

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: dict[int, DownloadItem] = {}
        self._requests: dict[int, QWebEngineDownloadRequest] = {}
        self._next_id = 1

    # -- reading ---------------------------------------------------------
    def items(self) -> list[DownloadItem]:
        """Newest first, which is the order a downloads list is read in."""
        return sorted(self._items.values(), key=lambda item: item.id, reverse=True)

    def active_count(self) -> int:
        return sum(1 for item in self._items.values() if not item.finished)

    def get(self, download_id: int) -> DownloadItem | None:
        return self._items.get(download_id)

    # -- the engine's side ------------------------------------------------
    def accept(self, request: QWebEngineDownloadRequest, directory: str) -> DownloadItem:
        """Take responsibility for a download Qt is asking about.

        Qt cancels a download unless it is explicitly accepted, which is why a
        browser that ignores `downloadRequested` looks broken on any link to a
        file. Qt also de-duplicates the file name itself, so accepting never
        silently overwrites an existing file.
        """
        download_id = self._next_id
        self._next_id += 1
        request.setDownloadDirectory(directory)

        item = DownloadItem(
            id=download_id,
            file_name=request.downloadFileName(),
            directory=request.downloadDirectory(),
            url=request.url().toString(),
            total=max(0, request.totalBytes()),
        )
        self._items[download_id] = item
        self._requests[download_id] = request

        request.receivedBytesChanged.connect(lambda i=download_id: self._sync(i))
        request.totalBytesChanged.connect(lambda i=download_id: self._sync(i))
        request.stateChanged.connect(lambda _s, i=download_id: self._sync(i))
        request.isFinishedChanged.connect(lambda i=download_id: self._sync(i))

        request.accept()
        self._sync(download_id, quiet=True)
        self.started.emit(item)
        return item

    def cancel(self, download_id: int) -> bool:
        request = self._requests.get(download_id)
        if request is None:
            return False
        request.cancel()
        self._sync(download_id)
        return True

    def clear_finished(self) -> None:
        for download_id in [i for i, item in self._items.items() if item.finished]:
            self._items.pop(download_id, None)
            self._requests.pop(download_id, None)

    # -- internals --------------------------------------------------------
    def _sync(self, download_id: int, *, quiet: bool = False) -> None:
        """Copy the engine's current numbers into our snapshot."""
        item = self._items.get(download_id)
        request = self._requests.get(download_id)
        if item is None or request is None:
            return
        was_finished = item.finished
        try:
            item.received = max(0, request.receivedBytes())
            item.total = max(0, request.totalBytes())
            item.state = _STATE_NAMES.get(request.state(), item.state)
            item.file_name = request.downloadFileName() or item.file_name
            item.directory = request.downloadDirectory() or item.directory
            if item.state == "interrupted":
                item.reason = request.interruptReasonString()
        except RuntimeError:
            # The engine deleted the request object under us; keep the last
            # snapshot rather than losing the row from the list.
            return
        if quiet:
            return
        self.changed.emit(item)
        if item.finished and not was_finished:
            self.finished.emit(item)

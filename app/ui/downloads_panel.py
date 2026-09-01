"""The downloads list.

Shows what is downloading, how far along it is, and where it went. Nothing
here invents a number: when the server sends no Content-Length the engine does
not know the total, and the row says "Downloading · 2.4 MB" with an
indeterminate bar rather than a progress bar filling up on a guess.
"""

from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.browser.downloads import DownloadItem, DownloadManager


class _Row(QWidget):
    """One download."""

    def __init__(self, item: DownloadItem, manager: DownloadManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self._manager = manager

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = QLabel(item.file_name, self)
        self.name.setStyleSheet("font-weight:600;")
        self.name.setToolTip(item.url)
        text.addWidget(self.name)

        self.status = QLabel("", self)
        self.status.setStyleSheet("color:#666; font-size:11px;")
        text.addWidget(self.status)

        self.bar = QProgressBar(self)
        self.bar.setMaximumHeight(6)
        self.bar.setTextVisible(False)
        text.addWidget(self.bar)
        layout.addLayout(text, 1)

        self.action = QPushButton("", self)
        self.action.clicked.connect(self._act)
        layout.addWidget(self.action)

        self.refresh(item)

    def refresh(self, item: DownloadItem) -> None:
        self._item = item
        self.name.setText(item.file_name)
        self.status.setText(item.describe())
        if item.finished:
            self.bar.hide()
            self.action.setText("Show in folder" if item.state == "completed" else "")
            self.action.setVisible(item.state == "completed")
        else:
            self.bar.show()
            share = item.percent
            if share is None:
                # Unknown total: an indeterminate bar is the honest display.
                self.bar.setRange(0, 0)
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(share)
            self.action.setVisible(True)
            self.action.setText("Cancel")

    def _act(self) -> None:
        if self._item.finished:
            reveal(os.path.join(self._item.directory, self._item.file_name))
        else:
            self._manager.cancel(self._item.id)


def reveal(path: str) -> bool:
    """Show a finished file in the system file manager.

    Falls back to opening the containing folder, and then to doing nothing
    visible rather than raising - a browser must not crash because a desktop
    has no file manager.
    """
    folder = os.path.dirname(path) or "."
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
            return True
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        # Linux desktops vary; opening the folder is the portable answer.
        return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    except Exception:  # noqa: BLE001
        return False


class DownloadsDialog(QDialog):
    """A live list of this session's downloads."""

    def __init__(self, manager: DownloadManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._rows: dict[int, _Row] = {}

        self.setWindowTitle("Downloads")
        self.resize(560, 420)

        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._body = QWidget(scroll)
        self._list = QVBoxLayout(self._body)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(1)
        self._list.addStretch(1)
        scroll.setWidget(self._body)
        layout.addWidget(scroll, 1)

        self._empty = QLabel(
            "Nothing downloaded yet.\nFiles you download will appear here.", self)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color:#888; padding:32px;")
        layout.addWidget(self._empty)

        buttons = QDialogButtonBox(self)
        clear = buttons.addButton("Clear finished", QDialogButtonBox.ButtonRole.ActionRole)
        clear.clicked.connect(self._clear)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        manager.started.connect(self._on_started)
        manager.changed.connect(self._on_changed)
        self.reload()

    # -- list management ---------------------------------------------------
    def reload(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for item in self._manager.items():
            self._insert(item)
        self._sync_empty()

    def _insert(self, item: DownloadItem) -> None:
        row = _Row(item, self._manager, self._body)
        self._rows[item.id] = row
        self._list.insertWidget(0, row)      # newest at the top

    def _on_started(self, item: DownloadItem) -> None:
        if item.id not in self._rows:
            self._insert(item)
        self._sync_empty()

    def _on_changed(self, item: DownloadItem) -> None:
        row = self._rows.get(item.id)
        if row is not None:
            row.refresh(item)

    def _clear(self) -> None:
        self._manager.clear_finished()
        self.reload()

    def _sync_empty(self) -> None:
        self._empty.setVisible(not self._rows)

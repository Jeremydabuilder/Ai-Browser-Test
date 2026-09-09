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
    QApplication,
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
from app.ui import icons, theme
from app.ui.theme import METRICS
from app.utils.urls import short_host


class _Row(QWidget):
    """One download."""

    def __init__(self, item: DownloadItem, manager: DownloadManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self._manager = manager

        m = METRICS
        c = theme.palette_for(QApplication.instance())
        self._colours = c
        # A card, not a bare row: the same surface/radius/hover language as
        # every other list this session touched (Mission's source rows, the
        # Mission list page), so Downloads stops being the one place still
        # rendered as an unstyled QWidget in a stack.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"_Row {{ background: {c.surface}; border-radius: {m.radius_md}px; }}"
            f"_Row:hover {{ background: {c.surface_hover}; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(m.space_3, m.space_2, m.space_3, m.space_2)
        layout.setSpacing(m.space_3)

        icon = QLabel(self)
        icon.setPixmap(icons.icon("download", c.muted, size=32, weight=2.0)
                       .pixmap(m.icon, m.icon))
        icon.setFixedWidth(m.icon)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.name = QLabel(item.file_name, self)
        self.name.setStyleSheet(f"color:{c.text}; font-weight:600; font-size:{m.text}px;")
        self.name.setToolTip(item.url)
        text.addWidget(self.name)

        self.status = QLabel("", self)
        self.status.setStyleSheet(f"font-size:{m.text_xs}px;")
        text.addWidget(self.status)

        self.bar = QProgressBar(self)
        self.bar.setMaximumHeight(6)
        self.bar.setTextVisible(False)
        text.addWidget(self.bar)
        layout.addLayout(text, 1)

        self.action = QPushButton("", self)
        self.action.setProperty("kind", "quiet")
        self.action.clicked.connect(self._act)
        layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)

        self.refresh(item)

    #: The colour a status line reads in, by state - the same tones the
    #: rest of the app already uses for success/danger/muted/in-progress,
    #: so "this one failed" is felt before the word "Failed" is read.
    def _status_colour(self, state: str) -> str:
        c = self._colours
        return {
            "completed": c.success,
            "cancelled": c.disabled,
            "interrupted": c.danger,
        }.get(state, c.accent)

    def refresh(self, item: DownloadItem) -> None:
        self._item = item
        self.name.setText(item.file_name)
        domain = short_host(QUrl(item.url))
        detail = item.describe()
        self.status.setText(f"{detail} · {domain}" if domain else detail)
        self.status.setStyleSheet(
            f"color:{self._status_colour(item.state)}; font-size:{METRICS.text_xs}px;"
            f"{'font-weight:600;' if item.state == 'interrupted' else ''}")
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
        m = METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._body = QWidget(scroll)
        self._list = QVBoxLayout(self._body)
        self._list.setContentsMargins(0, 0, 0, 2)
        self._list.setSpacing(m.space_2)
        self._list.addStretch(1)
        scroll.setWidget(self._body)
        layout.addWidget(scroll, 1)

        c = theme.palette_for(QApplication.instance())
        self._empty = QLabel(
            "Nothing downloaded yet.\nFiles you download will appear here.", self)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"color:{c.disabled}; font-size:{METRICS.text_sm}px; padding:{METRICS.space_6}px;")
        layout.addWidget(self._empty)

        buttons = QDialogButtonBox(self)
        clear = buttons.addButton("Clear finished", QDialogButtonBox.ButtonRole.ActionRole)
        clear.setProperty("kind", "quiet")
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

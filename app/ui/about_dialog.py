"""Phase 22 Part 2: About PyBrowser - version/build/platform/channel, plus
a Copy Diagnostics button (Part 11) that copies a redacted crash-style
report (version/platform/recent safe events) to the clipboard, never
uploading anything automatically.
"""

from __future__ import annotations

from PySide6.QtCore import qVersion
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME
from app.diagnostics import build_report
from app.version import build_info


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(380, 260)

        layout = QVBoxLayout(self)

        info = build_info()
        title = QLabel(f"<b>{info.app_name}</b> {info.version}", self)
        layout.addWidget(title)

        tagline = QLabel(
            "The browser that finishes internet tasks.<br>"
            "Browse normally, or give Py a goal and let it research, "
            "compare, and act across the web.", self)
        tagline.setWordWrap(True)
        layout.addWidget(tagline)

        details_lines = [f"Channel: {info.channel}"]
        if info.commit:
            details_lines.append(f"Build: {info.commit[:12]}")
        details_lines.append(f"Platform: {info.platform}")
        details_lines.append(f"Python: {info.python_version}")
        details_lines.append(f"Qt: {qVersion()}")
        details = QLabel("<br>".join(details_lines), self)
        layout.addWidget(details)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        copy_button = QPushButton("Copy Diagnostics", self)
        copy_button.clicked.connect(self._copy_diagnostics)
        button_row.addWidget(copy_button)
        button_row.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    def _copy_diagnostics(self) -> None:
        report = build_report()
        QApplication.clipboard().setText(report.text)
        note = "Diagnostics copied to clipboard."
        if report.redacted:
            note += " (some values were redacted)"
        self._status_label.setText(note)

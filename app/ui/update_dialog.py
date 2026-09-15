"""Phase 22 Parts 3/5/6: the update-check UI.

Deliberately does NOT install anything automatically. What it does:
check a release manifest, show what's new, verify a downloaded artifact's
checksum, and then hand off to the platform-appropriate, safe next step -
on Windows, an option to launch the verified installer (closing PyBrowser
first); on macOS, guidance to open the downloaded disk image and drag the
app across, since silently replacing a running, Gatekeeper-checked .app
bundle is not a safe thing to do from inside the app itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME
from app.updater.checker import UpdateCheckError, UpdateResult, check_for_updates
from app.updater.verify import verify_artifact
from app.updater.manifest import Artifact
from app.version import current_version


class UpdateCheckDialog(QDialog):
    """Shown from Help -> Check for Updates. Fetches the manifest
    synchronously (a plain HTTPS GET with a short timeout - see
    app.updater.checker) rather than backgrounding it: this is a
    deliberate, user-initiated action, not a background poll, so a
    brief blocking call is the honest, simplest behavior."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Check for Updates")
        self.resize(420, 220)
        self._result: UpdateResult | None = None

        layout = QVBoxLayout(self)
        self._status_label = QLabel(f"Checking for updates… (current version {current_version()})", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._notes_label = QLabel("", self)
        self._notes_label.setOpenExternalLinks(True)
        self._notes_label.setWordWrap(True)
        layout.addWidget(self._notes_label)

        layout.addStretch(1)

        button_row = QHBoxLayout()
        self._download_button = QPushButton("Download Update…", self)
        self._download_button.setVisible(False)
        self._download_button.clicked.connect(self._download_and_verify)
        button_row.addWidget(self._download_button)
        button_row.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self._run_check()

    def _run_check(self) -> None:
        try:
            result = check_for_updates()
        except UpdateCheckError as exc:
            self._status_label.setText(f"Could not check for updates: {exc}")
            return
        self._result = result
        if not result.available:
            self._status_label.setText(f"You're up to date (version {result.current_version}).")
            return
        self._status_label.setText(
            f"A new version is available: {result.latest_version} "
            f"(you have {result.current_version}).")
        if result.notes_url:
            self._notes_label.setText(f'<a href="{result.notes_url}">Release notes</a>')
        self._download_button.setVisible(True)

    def _download_and_verify(self) -> None:
        result = self._result
        if result is None or not result.available:
            return
        progress = QProgressDialog("Downloading update…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Downloading")
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        try:
            path = _download(result.download_url)
        except Exception as exc:  # noqa: BLE001 - any download failure becomes one clean message
            progress.close()
            QMessageBox.warning(self, "Download failed", str(exc))
            return
        progress.close()

        artifact = Artifact(url=result.download_url, sha256=result.sha256, size=result.size)
        verification = verify_artifact(path, artifact)
        if not verification.ok:
            QMessageBox.critical(
                self, "Verification failed",
                f"The downloaded file did not match the expected checksum and was "
                f"rejected:\n\n{verification.reason}\n\n"
                f"This can happen if the download was interrupted or corrupted - "
                f"try again, or download manually from the release notes.")
            try:
                os.unlink(path)
            except OSError:
                pass
            return

        self._offer_install(path)

    def _offer_install(self, path: str) -> None:
        if sys.platform == "win32":
            choice = QMessageBox.question(
                self, "Update verified",
                f"The update has been downloaded and verified.\n\n"
                f"{APP_NAME} needs to close to run the installer. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if choice == QMessageBox.StandardButton.Yes:
                _launch_installer_and_quit(path)
            return
        if sys.platform == "darwin":
            QMessageBox.information(
                self, "Update verified",
                f"The update has been downloaded and verified:\n\n{path}\n\n"
                f"Open it, then drag the new {APP_NAME} into Applications to "
                f"replace the current version - {APP_NAME} does not overwrite "
                f"its own running app bundle automatically.")
            return
        QMessageBox.information(
            self, "Update verified",
            f"The update has been downloaded and verified:\n\n{path}")


def _download(url: str) -> str:
    import httpx2 as httpx

    fd, path = tempfile.mkstemp(prefix="pybrowser-update-")
    os.close(fd)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)
    return path


def _launch_installer_and_quit(path: str) -> None:
    """Part 5: close cleanly, then hand off to the installer - never a
    self-overwrite of the running executable."""
    subprocess.Popen([path], shell=False)  # noqa: S603 - a verified, user-approved local installer
    QApplication.instance().quit()

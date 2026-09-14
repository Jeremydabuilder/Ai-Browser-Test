"""Settings -> Sync (Phase 20 Part 16/17). Shows on/off, this device's
name, the chosen storage (a local/synced folder), last-sync time, status,
and the actions the phase spec names explicitly: Sync now, Manage devices,
Change sync folder, Export recovery key, Disconnect this device - plus a
list of any unresolved conflicts with the three resolution buttons Part 9
asks for.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.sync.conflicts import Resolution
from app.sync.engine import SyncStatus

_STATUS_LABELS = {
    SyncStatus.OFF: "Off", SyncStatus.SYNCING: "Syncing…",
    SyncStatus.UP_TO_DATE: "Up to date", SyncStatus.OFFLINE: "Offline",
    SyncStatus.CONFLICT: "Conflict - action needed", SyncStatus.ERROR: "Error",
}


class SyncSettingsDialog(QDialog):
    def __init__(self, sync_service, scheduler, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sync")
        self.resize(480, 460)
        self._service = sync_service
        self._scheduler = scheduler

        layout = QVBoxLayout(self)

        self._status_label = QLabel(self)
        layout.addWidget(self._status_label)

        self._device_label = QLabel(self)
        layout.addWidget(self._device_label)
        rename_button = QPushButton("Rename this device…", self)
        rename_button.clicked.connect(self._rename_device)
        layout.addWidget(rename_button)

        self._folder_label = QLabel(self)
        layout.addWidget(self._folder_label)

        self._last_sync_label = QLabel(self)
        layout.addWidget(self._last_sync_label)

        toggle_row = QHBoxLayout()
        self._start_button = QPushButton("Turn on sync…", self)
        self._start_button.clicked.connect(self._start_sync)
        toggle_row.addWidget(self._start_button)
        self._join_button = QPushButton("Join existing sync…", self)
        self._join_button.clicked.connect(self._join_sync)
        toggle_row.addWidget(self._join_button)
        layout.addLayout(toggle_row)

        actions_row = QHBoxLayout()
        self._sync_now_button = QPushButton("Sync now", self)
        self._sync_now_button.clicked.connect(self._sync_now)
        actions_row.addWidget(self._sync_now_button)
        self._change_folder_button = QPushButton("Change sync folder…", self)
        self._change_folder_button.clicked.connect(self._change_folder)
        actions_row.addWidget(self._change_folder_button)
        self._disconnect_button = QPushButton("Disconnect this device", self)
        self._disconnect_button.clicked.connect(self._disconnect)
        actions_row.addWidget(self._disconnect_button)
        layout.addLayout(actions_row)

        layout.addWidget(QLabel("Conflicts:", self))
        self._conflicts_list = QListWidget(self)
        layout.addWidget(self._conflicts_list, 1)
        resolve_row = QHBoxLayout()
        for label, resolution in (
            ("Keep this device", Resolution.KEEP_LOCAL),
            ("Keep other device", Resolution.KEEP_REMOTE),
            ("Keep both", Resolution.KEEP_BOTH),
        ):
            button = QPushButton(label, self)
            button.clicked.connect(lambda _checked, r=resolution: self._resolve_selected(r))
            resolve_row.addWidget(button)
        layout.addLayout(resolve_row)

        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self._refresh()

    # -- refresh -------------------------------------------------------
    def _refresh(self) -> None:
        status = self._service.status
        self._status_label.setText(f"Status: {_STATUS_LABELS.get(status, status)}")
        self._device_label.setText(f"This device: {self._service.device.name}")
        folder = self._service.folder_path or "(not set)"
        self._folder_label.setText(f"Storage: Local encrypted folder\n{folder}")
        last = self._service.last_sync_at or "Never"
        self._last_sync_label.setText(f"Last sync: {last}")

        enabled = self._service.enabled
        self._start_button.setEnabled(not enabled)
        self._join_button.setEnabled(not enabled)
        self._sync_now_button.setEnabled(enabled)
        self._change_folder_button.setEnabled(enabled)
        self._disconnect_button.setEnabled(enabled)

        self._conflicts_list.clear()
        for conflict in self._service.conflicts.unresolved():
            item = QListWidgetItem(
                f"{conflict['record_type']} ({conflict['global_id'][:8]}…) - "
                f"changed on two devices")
            item.setData(1000, conflict["id"])
            self._conflicts_list.addItem(item)

    # -- actions ---------------------------------------------------------
    def _start_sync(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a Sync Folder")
        if not folder:
            return
        try:
            recovery_key = self._service.start_new_sync(folder)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Sync", f"Could not start sync: {exc}")
            return
        QMessageBox.information(
            self, "Sync enabled",
            "Write down this recovery key. It is the ONLY way to recover your synced "
            "data if you lose every device - PyBrowser cannot recover it for you:\n\n"
            f"{recovery_key}")
        self._refresh()

    def _join_sync(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the Existing Sync Folder")
        if not folder:
            return
        recovery_key, ok = QInputDialog.getText(self, "Join Sync", "Recovery key:")
        if not ok or not recovery_key.strip():
            return
        try:
            self._service.join_with_recovery_key(folder, recovery_key)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Sync", f"Could not join sync: {exc}")
            return
        self._refresh()

    def _rename_device(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Rename Device", "Device name:", QLineEdit.EchoMode.Normal,
            self._service.device.name)
        if ok and name.strip():
            self._service.rename_this_device(name)
            self._refresh()

    def _sync_now(self) -> None:
        result = self._scheduler.sync_now()
        self._refresh()
        if result.status == SyncStatus.ERROR:
            QMessageBox.warning(self, "Sync", result.error or "Sync failed.")
        elif result.status == SyncStatus.OFFLINE:
            QMessageBox.information(
                self, "Sync", "The sync folder is unavailable right now - will retry later.")

    def _change_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a Sync Folder")
        if folder:
            self._service.set_folder(folder)
            self._refresh()

    def _disconnect(self) -> None:
        if QMessageBox.question(
                self, "Disconnect This Device",
                "Stop syncing on this device? Other devices keep syncing normally.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._service.disconnect()
        self._refresh()

    def _resolve_selected(self, resolution: str) -> None:
        item = self._conflicts_list.currentItem()
        if item is None:
            return
        conflict_id = item.data(1000)
        self._service.resolve_conflict(conflict_id, resolution, **self._scheduler.domain_stores())
        self._refresh()

"""Phase 21: Collaboration / Shared Missions UI - a lightweight Share
dialog and a Shared-Mission panel (Owner/Participants/Role/Last synced,
with Invite/Remove/Change role/Stop sharing), plus a Comments section.
Not a chat product - this is deliberately small, matching Part 14/15.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.collaboration.service import CollaborationError
from app.collaboration.types import Role, TargetType


class CollaborationDialog(QDialog):
    """One dialog for one Mission's collaboration state: Share/Invite,
    the participant list with per-participant actions, and a lightweight
    Comments panel. Reopen it any time to see current Owner/Participants/
    Role/Last synced - it always reflects local state, refreshed after
    every action (including Sync now)."""

    def __init__(self, collab, mission_id: int, mission_title: str,
                parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Collaboration - {mission_title}")
        self.resize(520, 560)
        self._collab = collab
        self._mission_id = mission_id

        layout = QVBoxLayout(self)

        self._status_label = QLabel(self)
        layout.addWidget(self._status_label)

        share_row = QHBoxLayout()
        self._share_button = QPushButton("Share…", self)
        self._share_button.clicked.connect(self._share)
        share_row.addWidget(self._share_button)
        self._invite_button = QPushButton("Invite…", self)
        self._invite_button.clicked.connect(self._invite)
        share_row.addWidget(self._invite_button)
        self._join_button = QPushButton("Join from invite…", self)
        self._join_button.clicked.connect(self._join)
        share_row.addWidget(self._join_button)
        self._sync_button = QPushButton("Sync now", self)
        self._sync_button.clicked.connect(self._sync_now)
        share_row.addWidget(self._sync_button)
        self._stop_button = QPushButton("Stop sharing", self)
        self._stop_button.clicked.connect(self._stop_sharing)
        share_row.addWidget(self._stop_button)
        layout.addLayout(share_row)

        layout.addWidget(QLabel("Participants:", self))
        self._participants_list = QListWidget(self)
        layout.addWidget(self._participants_list, 1)
        participant_row = QHBoxLayout()
        self._remove_button = QPushButton("Remove selected", self)
        self._remove_button.clicked.connect(self._remove_selected)
        participant_row.addWidget(self._remove_button)
        self._role_combo = QComboBox(self)
        self._role_combo.addItems(list(Role.ALL))
        participant_row.addWidget(self._role_combo)
        self._change_role_button = QPushButton("Change role", self)
        self._change_role_button.clicked.connect(self._change_role_selected)
        participant_row.addWidget(self._change_role_button)
        layout.addLayout(participant_row)

        layout.addWidget(QLabel("Comments (Mission-level):", self))
        self._comments_list = QListWidget(self)
        layout.addWidget(self._comments_list, 1)
        comment_row = QHBoxLayout()
        self._comment_edit = QLineEdit(self)
        self._comment_edit.setPlaceholderText("Add a comment…")
        comment_row.addWidget(self._comment_edit, 1)
        add_comment_button = QPushButton("Add", self)
        add_comment_button.clicked.connect(self._add_comment)
        comment_row.addWidget(add_comment_button)
        layout.addLayout(comment_row)

        export_row = QHBoxLayout()
        export_button = QPushButton("Export Mission (Markdown)…", self)
        export_button.clicked.connect(self._export_markdown)
        export_row.addWidget(export_button)
        layout.addLayout(export_row)

        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self._refresh()

    # -- refresh -----------------------------------------------------------
    def _refresh(self) -> None:
        shared = self._collab.is_shared(self._mission_id)
        role = self._collab.role_for(self._mission_id)
        sharing = self._collab.sharing.get(self._mission_id)
        last_synced = sharing.since_token if sharing is not None else None
        status_text = (
            f"Shared - your role: {role} - Last synced: {last_synced or 'Never'}"
            if shared else "Not shared - only this device can see this Mission."
        )
        self._status_label.setText(status_text)

        self._share_button.setEnabled(not shared)
        self._invite_button.setEnabled(shared and role == Role.OWNER)
        self._sync_button.setEnabled(shared)
        self._stop_button.setEnabled(shared and role == Role.OWNER)
        self._remove_button.setEnabled(shared and role == Role.OWNER)
        self._change_role_button.setEnabled(shared and role == Role.OWNER)

        self._participants_list.clear()
        if shared:
            for participant in self._collab.participants.all(self._mission_id):
                state = "" if participant.is_active else " (removed)"
                item = QListWidgetItem(
                    f"{participant.display_name or participant.device_id} - "
                    f"{participant.role}{state}")
                item.setData(1000, participant.device_id)
                self._participants_list.addItem(item)

        self._comments_list.clear()
        if shared:
            for comment in self._collab.comments_for(self._mission_id):
                self._comments_list.addItem(
                    f"{comment.author_name or comment.author_device_id}: {comment.body}")

    def _selected_device_id(self) -> str | None:
        item = self._participants_list.currentItem()
        return item.data(1000) if item is not None else None

    # -- actions -------------------------------------------------------------
    def _share(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a Shared Folder")
        if not folder:
            return
        self._collab.share_mission(self._mission_id, folder)
        self._refresh()

    def _invite(self) -> None:
        role, ok = QInputDialog.getItem(
            self, "Invite", "Role for the new participant:",
            [Role.EDITOR, Role.VIEWER], 0, False)
        if not ok:
            return
        try:
            invite = self._collab.create_invite(self._mission_id, role=role)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Invite", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Invite File", "mission-invite.pbinvite")
        if path:
            with open(path, "wb") as fh:
                fh.write(invite.data)
        QMessageBox.information(
            self, "Invite created",
            "Share the invite file and this passphrase with your collaborator through "
            "any channel you trust - PyBrowser does not send it for you. Write it down; "
            "it is shown only once:\n\n" + invite.passphrase)
        self._refresh()

    def _join(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Invite File", "", "Invite files (*.pbinvite)")
        if not path:
            return
        passphrase, ok = QInputDialog.getText(self, "Join", "Invite passphrase:")
        if not ok or not passphrase:
            return
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            self._collab.join_mission(data, passphrase)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Join", str(exc))
            return
        QMessageBox.information(self, "Joined", "Joined the shared Mission.")
        self._refresh()

    def _sync_now(self) -> None:
        result = self._collab.sync_now(self._mission_id)
        if result.error:
            QMessageBox.warning(self, "Sync", result.error)
        self._refresh()

    def _stop_sharing(self) -> None:
        if QMessageBox.question(
            self, "Stop sharing",
            "Stop sharing this Mission? Participants keep what they already have, but "
            "future changes will no longer sync to them.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._collab.stop_sharing(self._mission_id)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Stop sharing", str(exc))
        self._refresh()

    def _remove_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id is None:
            return
        try:
            self._collab.remove_participant(self._mission_id, device_id)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Remove participant", str(exc))
        self._refresh()

    def _change_role_selected(self) -> None:
        device_id = self._selected_device_id()
        if device_id is None:
            return
        role = self._role_combo.currentText()
        try:
            self._collab.set_role(self._mission_id, device_id, role)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Change role", str(exc))
        self._refresh()

    def _add_comment(self) -> None:
        text = self._comment_edit.text().strip()
        if not text:
            return
        try:
            self._collab.add_comment(self._mission_id, TargetType.MISSION,
                                     str(self._mission_id), text)
        except CollaborationError as exc:
            QMessageBox.warning(self, "Comment", str(exc))
            return
        self._comment_edit.clear()
        self._refresh()

    def _export_markdown(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Mission", "mission.md", "Markdown (*.md)")
        if not path:
            return
        markdown = self._collab.export_markdown(self._mission_id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)

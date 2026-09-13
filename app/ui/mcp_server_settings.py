"""Settings -> External AI Access: the PyBrowser MCP Server (Phase 11).

Status, Enable/Disable, the paired-client list, and the pairing flow
itself - name + capability checkboxes, a token shown exactly once. Modeled
on app/ui/mcp_settings.py's "Connected Tools" panel (that one manages
PyBrowser's *outbound* MCP connections; this one manages *inbound* access
from external MCP clients), so the same house style applies: a compact
list plus a detail/action row, never a developer console.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.mcp_server import auth
from app.mcp_server.types import Capability
from app.ui import theme


class PairClientDialog(QDialog):
    """Name + capability checkboxes -> a token shown exactly once."""

    def __init__(self, store, host: str, port: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._host = host
        self._port = port
        self.setWindowTitle("Pair New AI Client")
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_4, m.space_4, m.space_4, m.space_4)
        layout.setSpacing(m.space_3)

        form = QFormLayout()
        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("e.g. ChatGPT, Claude Desktop")
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Permissions:", self))
        self._checks: dict[Capability, QCheckBox] = {}
        for capability, label in Capability.labels().items():
            box = QCheckBox(label, self)
            self._checks[capability] = box
            layout.addWidget(box)

        self._result_area = QPlainTextEdit(self)
        self._result_area.setReadOnly(True)
        self._result_area.setMaximumHeight(90)
        self._result_area.hide()
        layout.addWidget(self._result_area)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._generate_button = QPushButton("Generate", self)
        self._generate_button.clicked.connect(self._on_generate)
        buttons.addWidget(self._generate_button)
        self._close_button = QPushButton("Close", self)
        self._close_button.clicked.connect(self.accept)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

    def _on_generate(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Name required", "Give this client a name first.")
            return
        capabilities = [c.value for c, box in self._checks.items() if box.isChecked()]
        client, token = auth.pair_client(self._store, display_name=name, capabilities=capabilities)
        self._result_area.setPlainText(
            f"Pairing token (shown once - copy it now):\n{token}\n\n"
            f"Connection: http://{self._host}:{self._port}/mcp")
        self._result_area.show()
        self.name_edit.setEnabled(False)
        for box in self._checks.values():
            box.setEnabled(False)
        self._generate_button.setEnabled(False)


class ExternalAiAccessPanel(QWidget):
    """Status, Enable/Disable, paired clients, and their actions."""

    _COLUMNS = ["Name", "Permissions", "Created", "Last used", "Status"]

    def __init__(self, mcp_server, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._server = mcp_server
        m = theme.METRICS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(m.space_5, m.space_4, m.space_5, m.space_4)
        layout.setSpacing(m.space_3)

        status_row = QHBoxLayout()
        self.status_label = QLabel(self)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.toggle_button = QPushButton(self)
        self.toggle_button.clicked.connect(self._on_toggle)
        status_row.addWidget(self.toggle_button)
        layout.addLayout(status_row)

        self.table = QTableWidget(0, len(self._COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)

        button_row = QHBoxLayout()
        pair_button = QPushButton("Pair New AI Client…", self)
        pair_button.clicked.connect(self._on_pair)
        button_row.addWidget(pair_button)
        self.revoke_button = QPushButton("Revoke", self)
        self.revoke_button.clicked.connect(self._on_revoke)
        button_row.addWidget(self.revoke_button)
        self.activity_button = QPushButton("View Activity", self)
        self.activity_button.clicked.connect(self._on_view_activity)
        button_row.addWidget(self.activity_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._refresh()

    def _refresh(self) -> None:
        running = self._server.running
        self.status_label.setText(f"PyBrowser MCP Server: {'Running' if running else 'Stopped'}")
        self.toggle_button.setText("Disable" if running else "Enable")
        clients = self._server.store.list_clients()
        self.table.setRowCount(len(clients))
        for row, client in enumerate(clients):
            labels = [Capability.labels().get(Capability(c), c) for c in client.capabilities]
            self.table.setItem(row, 0, QTableWidgetItem(client.display_name))
            self.table.setItem(row, 1, QTableWidgetItem(", ".join(labels) or "(none)"))
            self.table.setItem(row, 2, QTableWidgetItem(client.created_at))
            self.table.setItem(row, 3, QTableWidgetItem(client.last_used_at or "Never"))
            status = "Revoked" if client.revoked else "Active"
            item = QTableWidgetItem(status)
            item.setData(Qt.ItemDataRole.UserRole, client.id)
            self.table.setItem(row, 4, item)

    def _selected_client_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 4)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_toggle(self) -> None:
        if self._server.running:
            self._server.stop()
        else:
            if not self._server.start():
                QMessageBox.warning(
                    self, "Could not start",
                    f"The MCP server could not bind to {self._server.host}:{self._server.port}.")
        self._refresh()

    def _on_pair(self) -> None:
        dialog = PairClientDialog(self._server.store, self._server.host, self._server.port, self)
        dialog.exec()
        self._refresh()

    def _on_revoke(self) -> None:
        client_id = self._selected_client_id()
        if client_id is None:
            return
        auth.revoke_client(self._server.store, client_id)
        self._refresh()

    def _on_view_activity(self) -> None:
        entries = self._server.store.recent_audit(limit=100)
        lines = [f"{e.created_at}  {e.tool}  {e.outcome}  {e.duration_ms}ms" for e in entries]
        QMessageBox.information(
            self, "Recent Activity", "\n".join(lines) or "No activity recorded yet.")

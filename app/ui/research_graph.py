"""Research Graph view (Phase 19): a central selected node, its connected
nodes, filters, and a details panel - deliberately a list/details view, not
a node-link canvas. The phase's own spec says it best: "A good list/details
graph is better than a flashy unusable one" - and every query behind it
(app.knowledge_graph.queries) is already capped, so there is nothing here
that would benefit from a physics-simulated canvas at desktop scale.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.knowledge_graph.types import NodeType

#: Shown in the type filter combo - "All" plus every concrete node type,
#: labelled the way the phase's own UI spec names its filters (Missions/
#: Findings/Sources/Claims/Topics - "Sources" covers WebPage/PDF/File,
#: since those three share every query and action here).
_FILTERS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("All", None),
    ("Missions", (NodeType.MISSION,)),
    ("Findings", (NodeType.FINDING,)),
    ("Sources", (NodeType.WEBPAGE, NodeType.PDF, NodeType.FILE)),
    ("Highlights", (NodeType.HIGHLIGHT,)),
    ("Claims", (NodeType.CLAIM,)),
    ("Topics", (NodeType.TOPIC,)),
)


class ResearchGraphDialog(QDialog):
    def __init__(self, graph, parent: QWidget | None = None, *, workspace_id: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Research Graph")
        self.resize(760, 520)
        self._graph = graph
        self._workspace_id = workspace_id
        self._selected_node_id: str | None = None

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._search_field = QLineEdit(self)
        self._search_field.setPlaceholderText("Search Missions, findings, sources, claims, topics…")
        self._search_field.returnPressed.connect(self._run_search)
        top.addWidget(self._search_field, 1)
        self._filter_box = QComboBox(self)
        for label, _types in _FILTERS:
            self._filter_box.addItem(label)
        self._filter_box.currentIndexChanged.connect(self._run_search)
        top.addWidget(self._filter_box)
        search_button = QPushButton("Search", self)
        search_button.clicked.connect(self._run_search)
        top.addWidget(search_button)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._results_list = QListWidget(self)
        self._results_list.currentItemChanged.connect(self._on_result_selected)
        splitter.addWidget(self._results_list)

        detail_panel = QWidget(self)
        detail_layout = QVBoxLayout(detail_panel)
        self._title_label = QLabel("Select a node to see its details.", self)
        self._title_label.setWordWrap(True)
        detail_layout.addWidget(self._title_label)
        self._details_text = QTextEdit(self)
        self._details_text.setReadOnly(True)
        detail_layout.addWidget(self._details_text, 1)

        actions = QHBoxLayout()
        self._open_source_button = QPushButton("Open Source", self)
        self._open_source_button.clicked.connect(self._open_source)
        self._open_source_button.setEnabled(False)
        actions.addWidget(self._open_source_button)
        self._rename_topic_button = QPushButton("Rename Topic…", self)
        self._rename_topic_button.clicked.connect(self._rename_topic)
        self._rename_topic_button.setEnabled(False)
        actions.addWidget(self._rename_topic_button)
        detail_layout.addLayout(actions)

        detail_layout.addWidget(QLabel("Related:", self))
        self._neighbors_list = QListWidget(self)
        self._neighbors_list.itemDoubleClicked.connect(self._navigate_to_neighbor)
        detail_layout.addWidget(self._neighbors_list, 1)
        unlink_button = QPushButton("Unlink Selected Relationship", self)
        unlink_button.clicked.connect(self._unlink_selected)
        detail_layout.addWidget(unlink_button)

        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)

        self._run_search()

    # -- search / selection ------------------------------------------------
    def _current_node_types(self) -> tuple[str, ...] | None:
        return _FILTERS[self._filter_box.currentIndex()][1]

    def _run_search(self) -> None:
        self._results_list.clear()
        query = self._search_field.text().strip()
        node_types = self._current_node_types()
        if self._graph is None:
            return
        if query:
            nodes = self._graph.search(query, node_types=node_types, limit=50)
        else:
            nodes = []
            for node_type in (node_types or NodeType.ALL):
                nodes.extend(self._graph.nodes_by_type(
                    node_type, workspace_id=self._workspace_id, limit=25))
        for node in nodes:
            item = QListWidgetItem(f"[{node.node_type}] {node.title or node.id}")
            item.setData(Qt.ItemDataRole.UserRole, node.id)
            self._results_list.addItem(item)

    def _on_result_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        self._show_node(current.data(Qt.ItemDataRole.UserRole))

    def _navigate_to_neighbor(self, item: QListWidgetItem) -> None:
        node_id = item.data(Qt.ItemDataRole.UserRole)
        if node_id:
            self._show_node(node_id)

    # -- details -------------------------------------------------------
    def _show_node(self, node_id: str) -> None:
        if self._graph is None:
            return
        node = self._graph.get_node(node_id)
        if node is None:
            return
        self._selected_node_id = node_id
        self._title_label.setText(f"{node.title or node_id}  ·  {node.node_type}")
        lines = [
            f"Type: {node.node_type}", f"Id: {node.id}",
            f"Source: {node.source_ref or '(none)'}", f"Provenance: {node.provenance}",
            f"Created: {node.created_at}", f"Updated: {node.updated_at}",
        ]
        if node.mission_id is not None:
            lines.append(f"Discovered in Mission: {node.mission_id}")
        if node.data:
            lines.append("")
            lines.append("Details:")
            for key, value in node.data.items():
                lines.append(f"  {key}: {value}")
        self._details_text.setPlainText("\n".join(lines))

        looks_like_url = (node.source_ref or "").startswith(("http://", "https://"))
        self._open_source_button.setEnabled(looks_like_url)
        self._rename_topic_button.setEnabled(node.node_type == NodeType.TOPIC)

        self._neighbors_list.clear()
        for neighbor in self._graph.neighbors(node_id, limit=50):
            if neighbor.node is None:
                continue
            arrow = "->" if neighbor.direction == "out" else "<-"
            label = f"{arrow} {neighbor.edge.edge_type} {arrow} [{neighbor.node.node_type}] " \
                   f"{neighbor.node.title or neighbor.node.id}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, neighbor.node.id)
            item.setData(Qt.ItemDataRole.UserRole + 1,
                        (neighbor.edge.edge_type, neighbor.edge.src_id, neighbor.edge.dst_id))
            self._neighbors_list.addItem(item)

    def _open_source(self) -> None:
        if self._selected_node_id is None or self._graph is None:
            return
        node = self._graph.get_node(self._selected_node_id)
        if node is not None and node.source_ref:
            QDesktopServices.openUrl(QUrl(node.source_ref))

    def _rename_topic(self) -> None:
        if self._selected_node_id is None or self._graph is None:
            return
        node = self._graph.get_node(self._selected_node_id)
        if node is None:
            return
        new_label, ok = QInputDialog.getText(
            self, "Rename Topic", "New name:", QLineEdit.EchoMode.Normal, node.title)
        if ok and new_label.strip():
            self._graph.rename_topic(self._selected_node_id, new_label)
            self._show_node(self._selected_node_id)
            self._run_search()

    def _unlink_selected(self) -> None:
        """Part USER CORRECTIONS: "unlink an incorrect relationship" -
        removes the edge outright and remembers the rejection so an
        automated rebuild does not silently recreate it (see
        app.knowledge_graph.service.RejectionStore)."""
        item = self._neighbors_list.currentItem()
        if item is None or self._graph is None:
            return
        edge_type, src_id, dst_id = item.data(Qt.ItemDataRole.UserRole + 1)
        if not QMessageBox.question(
                self, "Unlink Relationship",
                "Remove this relationship? PyBrowser will not recreate it automatically.",
        ) == QMessageBox.StandardButton.Yes:
            return
        self._graph.reject_edge(edge_type, src_id, dst_id)
        if self._selected_node_id:
            self._show_node(self._selected_node_id)

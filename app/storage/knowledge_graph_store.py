"""Raw SQL for the research graph - see app/knowledge_graph/. Thin, no
policy: parametrized queries and row->dataclass mapping only, the same
shape KnowledgeStore/HighlightStore already use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.storage.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_node(row) -> "GraphNode":
    from app.knowledge_graph.types import GraphNode

    data = dict(row)
    try:
        data["data"] = json.loads(data.pop("data_json") or "{}")
    except Exception:  # noqa: BLE001 - a corrupted blob never crashes a read
        data["data"] = {}
        data.pop("data_json", None)
    return GraphNode(**data)


def _row_to_edge(row) -> "GraphEdge":
    from app.knowledge_graph.types import GraphEdge

    data = dict(row)
    try:
        data["data"] = json.loads(data.pop("data_json") or "{}")
    except Exception:  # noqa: BLE001
        data["data"] = {}
        data.pop("data_json", None)
    return GraphEdge(**data)


class GraphStore:
    """CRUD + queries over ``knowledge_graph_nodes``/``knowledge_graph_edges``.

    Every write here is an upsert keyed by the node's deterministic id (or,
    for an edge, the (edge_type, src_id, dst_id) UNIQUE index) - "building
    the graph again for something already seen" is expected to happen
    often (Part "GRAPH BUILDING": incremental updates on every save), and
    must never grow a duplicate row.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- nodes -------------------------------------------------------------
    def upsert_node(self, node: "GraphNode") -> "GraphNode | None":
        now = _now()
        existing = self.get_node(node.id)
        created_at = existing.created_at if existing is not None else (node.created_at or now)
        self._db.execute(
            "INSERT INTO knowledge_graph_nodes "
            "(id, node_type, title, data_json, provenance, source_ref, mission_id, "
            " workspace_id, extraction_method, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  title=excluded.title, data_json=excluded.data_json, "
            "  provenance=excluded.provenance, source_ref=excluded.source_ref, "
            "  mission_id=excluded.mission_id, workspace_id=excluded.workspace_id, "
            "  extraction_method=excluded.extraction_method, confidence=excluded.confidence, "
            "  updated_at=excluded.updated_at",
            (node.id, node.node_type, node.title, json.dumps(node.data),
             node.provenance, node.source_ref, node.mission_id, node.workspace_id,
             node.extraction_method, node.confidence, created_at, now),
        )
        return self.get_node(node.id)

    def get_node(self, node_id: str) -> "GraphNode | None":
        row = self._db.query_one(
            "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
            "workspace_id, extraction_method, confidence, created_at, updated_at "
            "FROM knowledge_graph_nodes WHERE id = ?", (node_id,))
        return _row_to_node(row) if row is not None else None

    def nodes_by_type(self, node_type: str, *, workspace_id: str | None = None,
                      include_global: bool = True, limit: int = 500) -> list["GraphNode"]:
        """All nodes of one type, optionally scoped to a workspace - the
        same "this workspace OR global (NULL)" convention MissionStore.recent
        already uses, so a Phase 19 query never mixes workspaces when the
        caller explicitly scoped one, but still shows content that predates
        workspaces or was never workspace-tagged (see the module docstring
        on Highlights, which have no workspace_id at all)."""
        if workspace_id is None:
            rows = self._db.query(
                "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
                "workspace_id, extraction_method, confidence, created_at, updated_at "
                "FROM knowledge_graph_nodes WHERE node_type = ? "
                "ORDER BY updated_at DESC LIMIT ?", (node_type, limit))
        elif include_global:
            rows = self._db.query(
                "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
                "workspace_id, extraction_method, confidence, created_at, updated_at "
                "FROM knowledge_graph_nodes WHERE node_type = ? "
                "AND (workspace_id = ? OR workspace_id IS NULL) "
                "ORDER BY updated_at DESC LIMIT ?", (node_type, workspace_id, limit))
        else:
            rows = self._db.query(
                "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
                "workspace_id, extraction_method, confidence, created_at, updated_at "
                "FROM knowledge_graph_nodes WHERE node_type = ? AND workspace_id = ? "
                "ORDER BY updated_at DESC LIMIT ?", (node_type, workspace_id, limit))
        return [_row_to_node(row) for row in rows]

    def search_nodes(self, query: str, *, node_types: tuple[str, ...] | None = None,
                     limit: int = 20) -> list["GraphNode"]:
        needle = f"%{(query or '').strip()}%"
        if not needle.strip("%"):
            return []
        if node_types:
            placeholders = ",".join("?" for _ in node_types)
            rows = self._db.query(
                "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
                f"workspace_id, extraction_method, confidence, created_at, updated_at "
                f"FROM knowledge_graph_nodes WHERE title LIKE ? AND node_type IN ({placeholders}) "
                "ORDER BY updated_at DESC LIMIT ?", (needle, *node_types, limit))
        else:
            rows = self._db.query(
                "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
                "workspace_id, extraction_method, confidence, created_at, updated_at "
                "FROM knowledge_graph_nodes WHERE title LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?", (needle, limit))
        return [_row_to_node(row) for row in rows]

    def remove_node(self, node_id: str) -> None:
        """Cascades to every edge touching this node via ON DELETE CASCADE -
        never a ghost edge pointing at a node that no longer exists."""
        self._db.execute("DELETE FROM knowledge_graph_nodes WHERE id = ?", (node_id,))

    def remove_nodes_for_mission(self, mission_id: int) -> None:
        """Part DELETION: a deleted Mission's own node, plus any node whose
        provenance says it was discovered specifically while working on
        this mission (its Finding nodes; a Claim built only from this
        Mission's finding). Shared evidence nodes (a WebPage/PDF/File also
        referenced elsewhere) are never removed just because one Mission
        that used them is gone."""
        self.remove_node(f"mission:{mission_id}")
        rows = self._db.query(
            "SELECT id FROM knowledge_graph_nodes WHERE mission_id = ? "
            "AND node_type IN ('finding')", (mission_id,))
        for row in rows:
            self.remove_node(row["id"])

    def remove_nodes_for_highlight(self, highlight_id: int) -> None:
        from app.knowledge_graph.types import highlight_node_id

        self.remove_node(highlight_node_id(highlight_id))

    def remove_nodes_for_document(self, node_type: str, node_id: str) -> None:
        self.remove_node(node_id)

    # -- edges ---------------------------------------------------------
    def upsert_edge(self, edge: "GraphEdge") -> "GraphEdge | None":
        now = edge.created_at or _now()
        self._db.execute(
            "INSERT INTO knowledge_graph_edges "
            "(edge_type, src_id, dst_id, data_json, provenance, mission_id, workspace_id, "
            " confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(edge_type, src_id, dst_id) DO UPDATE SET "
            "  data_json=excluded.data_json, provenance=excluded.provenance, "
            "  confidence=excluded.confidence",
            (edge.edge_type, edge.src_id, edge.dst_id, json.dumps(edge.data),
             edge.provenance, edge.mission_id, edge.workspace_id, edge.confidence, now),
        )
        row = self._db.query_one(
            "SELECT id, edge_type, src_id, dst_id, data_json, provenance, mission_id, "
            "workspace_id, confidence, created_at FROM knowledge_graph_edges "
            "WHERE edge_type = ? AND src_id = ? AND dst_id = ?",
            (edge.edge_type, edge.src_id, edge.dst_id))
        return _row_to_edge(row) if row is not None else None

    def edges_from(self, node_id: str, *, edge_types: tuple[str, ...] | None = None,
                  limit: int = 200) -> list["GraphEdge"]:
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            rows = self._db.query(
                "SELECT id, edge_type, src_id, dst_id, data_json, provenance, mission_id, "
                f"workspace_id, confidence, created_at FROM knowledge_graph_edges "
                f"WHERE src_id = ? AND edge_type IN ({placeholders}) LIMIT ?",
                (node_id, *edge_types, limit))
        else:
            rows = self._db.query(
                "SELECT id, edge_type, src_id, dst_id, data_json, provenance, mission_id, "
                "workspace_id, confidence, created_at FROM knowledge_graph_edges "
                "WHERE src_id = ? LIMIT ?", (node_id, limit))
        return [_row_to_edge(row) for row in rows]

    def edges_to(self, node_id: str, *, edge_types: tuple[str, ...] | None = None,
                limit: int = 200) -> list["GraphEdge"]:
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            rows = self._db.query(
                "SELECT id, edge_type, src_id, dst_id, data_json, provenance, mission_id, "
                f"workspace_id, confidence, created_at FROM knowledge_graph_edges "
                f"WHERE dst_id = ? AND edge_type IN ({placeholders}) LIMIT ?",
                (node_id, *edge_types, limit))
        else:
            rows = self._db.query(
                "SELECT id, edge_type, src_id, dst_id, data_json, provenance, mission_id, "
                "workspace_id, confidence, created_at FROM knowledge_graph_edges "
                "WHERE dst_id = ? LIMIT ?", (node_id, limit))
        return [_row_to_edge(row) for row in rows]

    def remove_edge(self, edge_type: str, src_id: str, dst_id: str) -> None:
        """A user-rejected relationship (Part USER CORRECTIONS) - removed
        outright, not soft-deleted, since there is no "undo a rejection"
        UI; ``GraphBuilder`` checks ``rejected_edges`` before re-creating
        one, so this alone does not guarantee it never comes back (see
        service.py's RejectionStore for the actual guard)."""
        self._db.execute(
            "DELETE FROM knowledge_graph_edges WHERE edge_type = ? AND src_id = ? AND dst_id = ?",
            (edge_type, src_id, dst_id))

    def all_nodes(self, *, limit: int = 5000) -> list["GraphNode"]:
        rows = self._db.query(
            "SELECT id, node_type, title, data_json, provenance, source_ref, mission_id, "
            "workspace_id, extraction_method, confidence, created_at, updated_at "
            "FROM knowledge_graph_nodes ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [_row_to_node(row) for row in rows]

    def node_count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM knowledge_graph_nodes")
        return int(row["n"]) if row is not None else 0

    def edge_count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM knowledge_graph_edges")
        return int(row["n"]) if row is not None else 0

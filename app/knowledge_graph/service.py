"""The one thing UI/agent code actually constructs and holds - mirrors
KnowledgeIndex's role for the semantic index. See the module docstring in
builder.py for what each event hook does; this class is where those hooks
meet the real save/index/delete points elsewhere in the app (MissionService,
HighlightStore-owning UI code, KnowledgeIndex's document-indexing callers).

Semantic-history-off distinction (Part PRIVACY), stated once, here, since
it is a property of this module's own dependencies rather than a runtime
check: this service and everything it calls (builder.py, extraction.py,
queries.py) never imports or calls into app.knowledge (the semantic
index) at all. So "explicit Mission/finding relationships still get built
when Semantic History is off" is true simply because nothing here depends
on that toggle in the first place - and "semantic-index-derived
relationships are not created when semantic history is off" is equally
true, because this module has no semantic-index-derived relationship to
begin with (topic extraction is lexical, not embedding-based - see
extraction.py). A future, richer "related via the semantic index" edge
would need to check ``knowledge_index.enabled`` before running; none
exists yet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from app.knowledge_graph import queries
from app.knowledge_graph.builder import GraphBuilder
from app.knowledge_graph.extraction import extract_topics
from app.knowledge_graph.types import NodeType, source_node_id, topic_node_id

if TYPE_CHECKING:
    from app.storage.knowledge_graph_store import GraphStore
    from app.storage.settings import SettingsStore


class RejectionStore:
    """Part USER CORRECTIONS: "Never let automated rebuilding immediately
    recreate a relationship the user explicitly rejected without new
    evidence." A small settings-backed set of rejected (edge_type, src,
    dst) triples - the same lightweight persistence style
    app.agent.capabilities.CapabilityCache already uses for a similarly
    small, rarely-written record."""

    _SETTINGS_KEY = "knowledge_graph_rejected_edges"

    def __init__(self, settings: "SettingsStore | None" = None) -> None:
        self._settings = settings
        self._rejected: set[str] = set()
        self._load()

    @staticmethod
    def _key(edge_type: str, src_id: str, dst_id: str) -> str:
        return f"{edge_type}|{src_id}|{dst_id}"

    def _load(self) -> None:
        if self._settings is None:
            return
        try:
            raw = self._settings.get(self._SETTINGS_KEY, "") or ""
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    self._rejected = {str(item) for item in data}
        except Exception:  # noqa: BLE001 - a corrupted list is never fatal
            pass

    def _persist(self) -> None:
        if self._settings is None:
            return
        try:
            self._settings.set(self._SETTINGS_KEY, json.dumps(sorted(self._rejected)))
        except Exception:  # noqa: BLE001
            pass

    def reject(self, edge_type: str, src_id: str, dst_id: str) -> None:
        self._rejected.add(self._key(edge_type, src_id, dst_id))
        self._persist()

    def is_rejected(self, edge_type: str, src_id: str, dst_id: str) -> bool:
        return self._key(edge_type, src_id, dst_id) in self._rejected

    def clear(self, edge_type: str, src_id: str, dst_id: str) -> None:
        """"...without new evidence" is a human judgement this class does
        not make; this is the explicit undo for a rejection made in
        error."""
        self._rejected.discard(self._key(edge_type, src_id, dst_id))
        self._persist()


class KnowledgeGraphService:
    def __init__(self, store: "GraphStore", *, rejections: "RejectionStore | None" = None) -> None:
        self._store = store
        self._rejections = rejections
        is_rejected = rejections.is_rejected if rejections is not None else None
        self._builder = GraphBuilder(store, is_rejected=is_rejected)

    @property
    def store(self) -> "GraphStore":
        return self._store

    @property
    def builder(self) -> GraphBuilder:
        return self._builder

    # -- event hooks - call these at the existing save/index/complete points --
    def on_finding_saved(self, *, finding_id: int, mission, text: str,
                         source_url: str = "", source_title: str = "",
                         provenance: str | None = None, contributed_by: str | None = None):
        kwargs = {}
        if provenance is not None:
            kwargs["provenance"] = provenance
        if contributed_by is not None:
            kwargs["contributed_by"] = contributed_by
        return self._builder.on_finding_saved(
            finding_id=finding_id, mission=mission, text=text,
            source_url=source_url, source_title=source_title, **kwargs)

    def on_highlight_created(self, highlight):
        return self._builder.on_highlight_created(highlight)

    def on_document_indexed(self, node_type: str, path: str, text: str, *, title: str = "",
                            mission_id: int | None = None, workspace_id: str | None = None):
        return self._builder.on_document_indexed(
            node_type, path, text, title=title, mission_id=mission_id, workspace_id=workspace_id)

    def on_mission_completed(self, mission):
        return self._builder.on_mission_completed(mission)

    # -- deletion cascades (Part DELETION) ---------------------------------
    def remove_mission(self, mission_id: int) -> None:
        self._store.remove_nodes_for_mission(mission_id)

    def remove_highlight(self, highlight_id: int) -> None:
        self._store.remove_nodes_for_highlight(highlight_id)

    def remove_document(self, node_type: str, locator: str) -> None:
        self._store.remove_node(source_node_id(node_type, locator))

    # -- user corrections (Part USER CORRECTIONS) --------------------------
    def reject_edge(self, edge_type: str, src_id: str, dst_id: str) -> None:
        self._store.remove_edge(edge_type, src_id, dst_id)
        if self._rejections is not None:
            self._rejections.reject(edge_type, src_id, dst_id)

    def rename_topic(self, topic_id: str, new_label: str):
        node = self._store.get_node(topic_id)
        if node is None or node.node_type != NodeType.TOPIC:
            return None
        new_label = (new_label or "").strip()
        return self._store.upsert_node(replace(node, title=new_label or node.title))

    def merge_topics(self, keep_id: str, absorb_id: str) -> None:
        """Part ENTITY DEDUPLICATION: a deliberately manual action - never
        run automatically - that re-points every edge touching
        ``absorb_id`` at ``keep_id`` and removes the now-redundant node."""
        if keep_id == absorb_id:
            return
        for edge in self._store.edges_to(absorb_id, limit=500):
            self._builder.link(edge.edge_type, edge.src_id, keep_id, data=edge.data,
                               provenance=edge.provenance, mission_id=edge.mission_id,
                               workspace_id=edge.workspace_id, confidence=edge.confidence)
        for edge in self._store.edges_from(absorb_id, limit=500):
            self._builder.link(edge.edge_type, keep_id, edge.dst_id, data=edge.data,
                               provenance=edge.provenance, mission_id=edge.mission_id,
                               workspace_id=edge.workspace_id, confidence=edge.confidence)
        self._store.remove_node(absorb_id)

    # -- queries -------------------------------------------------------
    def neighbors(self, node_id: str, *, limit: int = queries.MAX_NEIGHBORS):
        return queries.neighbors(self._store, node_id, limit=limit)

    def sources_for_claim(self, claim_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.sources_for_claim(self._store, claim_id, limit=limit)

    def contradictions_for_claim(self, claim_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.contradictions_for_claim(self._store, claim_id, limit=limit)

    def findings_for_source(self, source_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.findings_for_source(self._store, source_id, limit=limit)

    def missions_for_source(self, source_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.missions_for_source(self._store, source_id, limit=limit)

    def missions_for_topic(self, topic_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.missions_for_topic(self._store, topic_id, limit=limit)

    def related_sources(self, source_id: str, *, limit: int = queries.MAX_RESULTS):
        return queries.related_sources(self._store, source_id, limit=limit)

    def provenance_chain(self, node_id: str, *, max_hops: int = 6):
        return queries.provenance_chain(self._store, node_id, max_hops=max_hops)

    def search(self, query: str, *, node_types: tuple[str, ...] | None = None, limit: int = 20):
        return self._store.search_nodes(query, node_types=node_types, limit=limit)

    def get_node(self, node_id: str):
        return self._store.get_node(node_id)

    def nodes_by_type(self, node_type: str, *, workspace_id: str | None = None, limit: int = 500):
        return self._store.nodes_by_type(node_type, workspace_id=workspace_id, limit=limit)

    # -- Mission historical context (Part MISSION INTEGRATION) ------------
    def historical_context_for_goal(self, goal: str, *, workspace_id: str | None = None,
                                    limit: int = 5) -> list[dict]:
        """Prior Missions whose topics overlap this goal's. Every entry is
        explicitly labeled with its own Mission id/title/status/date -
        "Historical evidence must never masquerade as current web
        evidence" - so a caller can only ever present this as "you looked
        into this before," never as a fresh finding. Workspace-scoped the
        same "this workspace OR global" way every other query in this app
        already is (see GraphStore.nodes_by_type)."""
        seen: dict[str, dict] = {}
        for label in extract_topics(goal):
            topic_id = topic_node_id(label)
            for mission_node in queries.missions_for_topic(self._store, topic_id, limit=limit * 2):
                if workspace_id is not None and mission_node.workspace_id not in (workspace_id, None):
                    continue
                seen.setdefault(mission_node.id, {
                    "mission_id": mission_node.mission_id,
                    "title": mission_node.title,
                    "status": mission_node.data.get("status", ""),
                    "updated_at": mission_node.updated_at,
                    "note": "Previously researched - historical context only, not current evidence.",
                })
        return list(seen.values())[:limit]

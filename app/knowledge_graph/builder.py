"""Incremental graph construction - one small update per real event, never
a rebuild-everything sweep (Part GRAPH BUILDING). Every public method here
is meant to be called exactly where the thing it describes already
happens: a finding saved, a highlight created, a PDF/file indexed, a
Mission marked complete - see app/knowledge_graph/service.py for where
those call sites are wired in.

Claims (Part CLAIM MODEL) are created only when linked to evidence: a
Finding becomes a Claim only when it has a source page/PDF/file behind it,
never merely because a model said something. There is deliberately no path
here that turns a model's own free-text synthesis into a Claim by itself -
Part CLAIM MODEL is explicit that "User/model synthesis can become a Claim
only when linked to evidence or explicitly marked as inference," and this
module only implements the evidence-linked half; an "explicit inference"
Claim would be a separate, user-initiated action this phase does not add.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from app.knowledge_graph.extraction import extract_topics, strip_numbers
from app.knowledge_graph.types import (
    ContradictionKind,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    claim_node_id,
    file_node_id,
    finding_node_id,
    highlight_node_id,
    mission_node_id,
    normalize_claim_statement,
    pdf_node_id,
    source_node_id,
    webpage_node_id,
)
from app.security.provenance import Provenance

if TYPE_CHECKING:
    from app.storage.knowledge_graph_store import GraphStore

#: A claim contradicted by another one seen at least this many days later
#: is read as "superseded" (newer information) rather than "contradiction"
#: (two sources disagreeing about the same moment in time) - Part
#: CONTRADICTIONS' own example (a price that changed) is exactly this case.
#: Deliberately conservative and coarse: this is a heuristic default, not a
#: claim about how fast any particular fact actually changes.
SUPERSEDED_THRESHOLD_DAYS = 14

_SOURCE_PROVENANCE = {
    NodeType.WEBPAGE: Provenance.WEBPAGE,
    NodeType.PDF: Provenance.PDF,
    NodeType.FILE: Provenance.FILE,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GraphBuilder:
    def __init__(self, store: "GraphStore", *,
                 is_rejected: "Callable[[str, str, str], bool] | None" = None) -> None:
        self._store = store
        #: Part USER CORRECTIONS: "Never let automated rebuilding
        #: immediately recreate a relationship the user explicitly
        #: rejected without new evidence." Checked before every edge write;
        #: None (the default, e.g. in isolated tests) means nothing is
        #: rejected.
        self._is_rejected = is_rejected or (lambda edge_type, src, dst: False)

    # -- node helpers ----------------------------------------------------
    def ensure_mission_node(self, mission) -> GraphNode:
        node = GraphNode(
            id=mission_node_id(mission.id), node_type=NodeType.MISSION,
            title=mission.title or (mission.goal or "")[:120],
            data={"goal": (mission.goal or "")[:500], "status": mission.status},
            provenance=Provenance.TRUSTED_APP_STATE, source_ref=f"mission:{mission.id}",
            mission_id=mission.id, workspace_id=mission.workspace_id,
            extraction_method="direct")
        return self._store.upsert_node(node)

    def ensure_source_node(self, node_type: str, locator: str, *, title: str = "",
                           mission_id: int | None = None,
                           workspace_id: str | None = None) -> GraphNode:
        node = GraphNode(
            id=source_node_id(node_type, locator), node_type=node_type,
            title=title or locator, data={},
            provenance=_SOURCE_PROVENANCE.get(node_type, Provenance.WEBPAGE),
            source_ref=locator, mission_id=mission_id, workspace_id=workspace_id,
            extraction_method="direct")
        return self._store.upsert_node(node)

    def ensure_topic_node(self, label: str) -> "GraphNode | None":
        from app.knowledge_graph.types import normalize_topic, topic_node_id

        normalized = normalize_topic(label)
        if not normalized:
            return None
        node = GraphNode(
            id=topic_node_id(label), node_type=NodeType.TOPIC, title=normalized,
            data={}, provenance=Provenance.SYSTEM, extraction_method="lexical")
        return self._store.upsert_node(node)

    def link(self, edge_type: str, src_id: str, dst_id: str, *, data: dict | None = None,
             provenance: str = Provenance.SYSTEM, mission_id: int | None = None,
             workspace_id: str | None = None, confidence: float | None = None) -> "GraphEdge | None":
        if self._is_rejected(edge_type, src_id, dst_id):
            return None
        edge = GraphEdge(
            edge_type=edge_type, src_id=src_id, dst_id=dst_id, data=data or {},
            provenance=provenance, mission_id=mission_id, workspace_id=workspace_id,
            confidence=confidence)
        return self._store.upsert_edge(edge)

    def link_topics(self, node_id: str, text: str, *, mission_id: int | None = None,
                    workspace_id: str | None = None) -> list[GraphNode]:
        """Lexical-only (Part TOPICS: no cloud model required) - always
        runs regardless of whether Semantic History is enabled, since
        topic extraction here never touches app.knowledge/the semantic
        index at all (see the module docstring's semantic-history-off
        distinction, documented fully in service.py)."""
        created = []
        for label in extract_topics(text):
            topic = self.ensure_topic_node(label)
            if topic is None:
                continue
            self.link(EdgeType.ABOUT_TOPIC, node_id, topic.id,
                      provenance=Provenance.SYSTEM, mission_id=mission_id,
                      workspace_id=workspace_id)
            created.append(topic)
        return created

    # -- claims ------------------------------------------------------------
    def _upsert_claim(self, statement: str, *, evidence_node_id: str,
                      mission_id: int | None = None, workspace_id: str | None = None) -> GraphNode:
        now = _now()
        claim_id = claim_node_id(statement)
        existing = self._store.get_node(claim_id)
        if existing is not None:
            data = dict(existing.data)
            refs = set(data.get("source_refs", []))
            refs.add(evidence_node_id)
            data["source_refs"] = sorted(refs)
            data["support_count"] = int(data.get("support_count", 0)) + 1
            data["last_seen"] = now
            data.setdefault("first_seen", now)
            data.setdefault("original_text", statement[:500])
            data.setdefault("normalized_statement", normalize_claim_statement(statement))
            data.setdefault("contradiction_count", 0)
            node = replace(existing, data=data, workspace_id=existing.workspace_id or workspace_id)
        else:
            data = {
                "normalized_statement": normalize_claim_statement(statement),
                "original_text": statement[:500],
                "source_refs": [evidence_node_id],
                "first_seen": now, "last_seen": now,
                "support_count": 1, "contradiction_count": 0,
            }
            node = GraphNode(
                id=claim_id, node_type=NodeType.CLAIM, title=statement[:160], data=data,
                provenance=Provenance.TRUSTED_APP_STATE, mission_id=mission_id,
                workspace_id=workspace_id, extraction_method="heuristic")
        return self._store.upsert_node(node)

    def _bump_contradiction_count(self, node_id: str) -> None:
        node = self._store.get_node(node_id)
        if node is None:
            return
        data = dict(node.data)
        data["contradiction_count"] = int(data.get("contradiction_count", 0)) + 1
        self._store.upsert_node(replace(node, data=data))

    @staticmethod
    def _classify_disagreement(a: GraphNode, b: GraphNode) -> str:
        """Part CONTRADICTIONS: preserve timestamps before calling
        something contradictory. Two claims about the same underlying
        statement, seen far apart in time, more likely reflect the world
        changing (superseded) than two sources disagreeing about the same
        moment (contradiction)."""
        try:
            first_a = datetime.fromisoformat(a.data.get("first_seen", ""))
            first_b = datetime.fromisoformat(b.data.get("first_seen", ""))
        except (ValueError, TypeError):
            return ContradictionKind.CONTRADICTION
        delta_days = abs((first_a - first_b).total_seconds()) / 86400
        return (ContradictionKind.SUPERSEDED if delta_days >= SUPERSEDED_THRESHOLD_DAYS
                else ContradictionKind.CONTRADICTION)

    def _detect_contradictions(self, claim_node: GraphNode) -> None:
        """Deterministic only (Part CONTRADICTIONS: "Use deterministic
        rules where obvious"): two Claims with the same statement once
        numbers are stripped out, but a different normalized statement
        (i.e. the numbers actually differ), are a genuine disagreement -
        never merely "differing opinion", which this method does not
        attempt to detect at all (that would need real semantic judgement
        this codebase has no reliable deterministic way to make; disclosed
        as a known limitation rather than guessed at)."""
        base = strip_numbers(claim_node.data.get("original_text", claim_node.title))
        if not base:
            return
        already_linked = {
            edge.dst_id for edge in
            self._store.edges_from(claim_node.id, edge_types=(EdgeType.CLAIM_CONTRADICTED_BY,))
        }
        for other in self._store.nodes_by_type(NodeType.CLAIM, limit=500):
            if other.id == claim_node.id or other.id in already_linked:
                continue
            other_base = strip_numbers(other.data.get("original_text", other.title))
            if not other_base or other_base != base:
                continue
            if other.data.get("normalized_statement") == claim_node.data.get("normalized_statement"):
                continue  # identical claim, not a disagreement
            kind = self._classify_disagreement(claim_node, other)
            self.link(EdgeType.CLAIM_CONTRADICTED_BY, claim_node.id, other.id,
                      data={"kind": kind}, provenance=Provenance.SYSTEM)
            self.link(EdgeType.CLAIM_CONTRADICTED_BY, other.id, claim_node.id,
                      data={"kind": kind}, provenance=Provenance.SYSTEM)
            self._bump_contradiction_count(claim_node.id)
            self._bump_contradiction_count(other.id)

    # -- event hooks -------------------------------------------------------
    def on_finding_saved(self, *, finding_id: int, mission, text: str,
                         source_url: str = "", source_title: str = "",
                         provenance: str = Provenance.TRUSTED_APP_STATE,
                         contributed_by: str | None = None
                         ) -> tuple[GraphNode, "GraphNode | None"]:
        """``provenance``/``contributed_by`` let Phase 21 collaboration
        sync (see app.sync.adapters.MissionFindingAdapter) tag a finding
        that arrived from a peer device as Provenance.COLLABORATOR_CONTENT
        rather than this method's own default of TRUSTED_APP_STATE - a
        finding this device typed itself is authoritative app state; one
        that arrived over a shared folder from someone else's device is
        not, and the graph should say so (Part 9)."""
        mission_node = self.ensure_mission_node(mission)
        node_data = {"text": text[:500]}
        if contributed_by:
            node_data["contributed_by_device"] = contributed_by
        finding_node = self._store.upsert_node(GraphNode(
            id=finding_node_id(finding_id), node_type=NodeType.FINDING,
            title=text[:160], data=node_data,
            provenance=provenance, mission_id=mission.id,
            workspace_id=mission.workspace_id, extraction_method="direct"))
        self.link(EdgeType.MISSION_HAS_FINDING, mission_node.id, finding_node.id,
                  provenance=provenance, mission_id=mission.id,
                  workspace_id=mission.workspace_id)

        source_node = None
        if source_url:
            source_node = self.ensure_source_node(
                NodeType.WEBPAGE, source_url, title=source_title,
                mission_id=mission.id, workspace_id=mission.workspace_id)
            self.link(EdgeType.MISSION_USED_SOURCE, mission_node.id, source_node.id,
                      provenance=provenance, mission_id=mission.id,
                      workspace_id=mission.workspace_id)
            self.link(EdgeType.DERIVED_FROM, finding_node.id, source_node.id,
                      provenance=provenance, mission_id=mission.id,
                      workspace_id=mission.workspace_id)

        self.link_topics(finding_node.id, text, mission_id=mission.id,
                         workspace_id=mission.workspace_id)

        claim_node = None
        if source_node is not None:
            claim_node = self._upsert_claim(
                text, evidence_node_id=source_node.id, mission_id=mission.id,
                workspace_id=mission.workspace_id)
            self.link(EdgeType.CLAIM_SUPPORTED_BY, claim_node.id, source_node.id,
                      provenance=Provenance.TRUSTED_APP_STATE, mission_id=mission.id,
                      workspace_id=mission.workspace_id)
            self.link(EdgeType.FINDING_SUPPORTED_BY, finding_node.id, source_node.id,
                      provenance=Provenance.TRUSTED_APP_STATE, mission_id=mission.id,
                      workspace_id=mission.workspace_id)
            self.link(EdgeType.DERIVED_FROM, claim_node.id, finding_node.id,
                      provenance=Provenance.TRUSTED_APP_STATE, mission_id=mission.id,
                      workspace_id=mission.workspace_id)
            self._detect_contradictions(claim_node)
        return finding_node, claim_node

    def on_highlight_created(self, highlight) -> GraphNode:
        source_node = None
        if highlight.url:
            source_node = self.ensure_source_node(
                NodeType.WEBPAGE, highlight.url, title=highlight.title)
        highlight_node = self._store.upsert_node(GraphNode(
            id=highlight_node_id(highlight.id), node_type=NodeType.HIGHLIGHT,
            title=highlight.title or highlight.text[:80],
            data={"text": highlight.text[:500]}, provenance=Provenance.TRUSTED_APP_STATE,
            source_ref=highlight.url, extraction_method="direct"))
        if source_node is not None:
            self.link(EdgeType.HIGHLIGHT_FROM_SOURCE, highlight_node.id, source_node.id,
                      provenance=Provenance.TRUSTED_APP_STATE)
        self.link_topics(highlight_node.id, highlight.text)
        return highlight_node

    def on_document_indexed(self, node_type: str, path: str, text: str, *, title: str = "",
                            mission_id: int | None = None,
                            workspace_id: str | None = None) -> GraphNode:
        node = self.ensure_source_node(node_type, path, title=title, mission_id=mission_id,
                                       workspace_id=workspace_id)
        self.link_topics(node.id, text, mission_id=mission_id, workspace_id=workspace_id)
        if mission_id is not None and self._store.get_node(mission_node_id(mission_id)) is not None:
            self.link(EdgeType.MISSION_USED_SOURCE, mission_node_id(mission_id), node.id,
                      provenance=Provenance.TRUSTED_APP_STATE, mission_id=mission_id,
                      workspace_id=workspace_id)
        return node

    def on_mission_completed(self, mission) -> GraphNode:
        """Part MISSION INTEGRATION's other half: once a Mission finishes,
        connect it (deterministically - shared Topic nodes, at least two in
        common) to prior Missions on a similar subject, so a later
        Mission's historical-context lookup (see service.py) has something
        to find. A cheap, bounded rule rather than an all-pairs similarity
        sweep - see Part PERFORMANCE."""
        mission_node = self.ensure_mission_node(mission)
        my_topics = {edge.dst_id for edge in
                    self._store.edges_from(mission_node.id, edge_types=(EdgeType.ABOUT_TOPIC,))}
        if not my_topics and mission.goal:
            self.link_topics(mission_node.id, mission.goal, mission_id=mission.id,
                             workspace_id=mission.workspace_id)
            my_topics = {edge.dst_id for edge in
                        self._store.edges_from(mission_node.id, edge_types=(EdgeType.ABOUT_TOPIC,))}
        seen_others: set[str] = set()
        for topic_id in my_topics:
            for edge in self._store.edges_to(topic_id, edge_types=(EdgeType.ABOUT_TOPIC,), limit=50):
                other_id = edge.src_id
                if other_id == mission_node.id or not other_id.startswith("mission:") \
                        or other_id in seen_others:
                    continue
                seen_others.add(other_id)
                other_topics = {e.dst_id for e in
                               self._store.edges_from(other_id, edge_types=(EdgeType.ABOUT_TOPIC,))}
                shared = my_topics & other_topics
                if len(shared) >= 2:
                    self.link(EdgeType.RELATED_TO, mission_node.id, other_id,
                              data={"shared_topics": len(shared)}, provenance=Provenance.SYSTEM)
                    self.link(EdgeType.RELATED_TO, other_id, mission_node.id,
                              data={"shared_topics": len(shared)}, provenance=Provenance.SYSTEM)
        return mission_node

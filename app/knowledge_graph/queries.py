"""High-level, capped graph queries - the only way agent tools and UI code
read the graph; nothing outside app/knowledge_graph/ or app/storage/
touches raw SQL for it (Part GRAPH QUERY API: "Add higher-level query
helpers rather than exposing raw SQL to agents.").

Every query here is bounded (Part PERFORMANCE: "Cap visualization/query
expansion") - a capped one-hop or few-hop walk, never an unbounded
traversal, and never a return of "the whole graph."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.knowledge_graph.types import EdgeType, GraphEdge, GraphNode, NodeType

if TYPE_CHECKING:
    from app.storage.knowledge_graph_store import GraphStore

MAX_NEIGHBORS = 50
MAX_RESULTS = 25


@dataclass(frozen=True)
class Neighbor:
    edge: GraphEdge
    node: "GraphNode | None"
    direction: str  # "out" | "in"


def neighbors(store: "GraphStore", node_id: str, *, limit: int = MAX_NEIGHBORS) -> list[Neighbor]:
    """The node's immediate neighborhood, both directions - the "central
    selected node + connected nodes" the Research Graph UI's own spec asks
    for, capped so a heavily-connected node never blows up the view."""
    out: list[Neighbor] = []
    for edge in store.edges_from(node_id, limit=limit):
        out.append(Neighbor(edge=edge, node=store.get_node(edge.dst_id), direction="out"))
    remaining = max(0, limit - len(out))
    if remaining:
        for edge in store.edges_to(node_id, limit=remaining):
            out.append(Neighbor(edge=edge, node=store.get_node(edge.src_id), direction="in"))
    return out[:limit]


def sources_for_claim(store: "GraphStore", claim_id: str, *, limit: int = MAX_RESULTS) -> list[GraphNode]:
    """"What sources support this conclusion?" """
    edges = store.edges_from(claim_id, edge_types=(EdgeType.CLAIM_SUPPORTED_BY,), limit=limit)
    return [n for n in (store.get_node(e.dst_id) for e in edges) if n is not None]


def contradictions_for_claim(store: "GraphStore", claim_id: str,
                             *, limit: int = MAX_RESULTS) -> list[tuple[GraphNode, str]]:
    """"Which two sources disagree?" - each result is the contradicting
    Claim plus its disagreement kind (contradiction/superseded/
    differing_opinion, see builder.ContradictionKind)."""
    edges = store.edges_from(claim_id, edge_types=(EdgeType.CLAIM_CONTRADICTED_BY,), limit=limit)
    out = []
    for edge in edges:
        node = store.get_node(edge.dst_id)
        if node is not None:
            out.append((node, edge.data.get("kind", "")))
    return out


def findings_for_source(store: "GraphStore", source_id: str, *, limit: int = MAX_RESULTS) -> list[GraphNode]:
    """"Which Missions used this PDF?"'s finding-level cousin: every
    Finding derived from this source."""
    edges = store.edges_to(source_id, edge_types=(EdgeType.FINDING_SUPPORTED_BY, EdgeType.DERIVED_FROM),
                           limit=limit * 2)
    out = []
    for edge in edges:
        node = store.get_node(edge.src_id)
        if node is not None and node.node_type == NodeType.FINDING:
            out.append(node)
    return out[:limit]


def missions_for_source(store: "GraphStore", source_id: str, *, limit: int = MAX_RESULTS) -> list[GraphNode]:
    """"Which Missions used this PDF?" """
    edges = store.edges_to(source_id, edge_types=(EdgeType.MISSION_USED_SOURCE,), limit=limit)
    out = []
    for edge in edges:
        node = store.get_node(edge.src_id)
        if node is not None and node.node_type == NodeType.MISSION:
            out.append(node)
    return out


def missions_for_topic(store: "GraphStore", topic_id: str, *, limit: int = MAX_RESULTS) -> list[GraphNode]:
    """"What have I learned about MCP across all my Missions?" """
    edges = store.edges_to(topic_id, edge_types=(EdgeType.ABOUT_TOPIC,), limit=limit * 2)
    out = []
    for edge in edges:
        node = store.get_node(edge.src_id)
        if node is not None and node.node_type == NodeType.MISSION:
            out.append(node)
    return out[:limit]


def related_sources(store: "GraphStore", source_id: str, *, limit: int = MAX_RESULTS) -> list[GraphNode]:
    """Other sources reached from this one via a shared Mission or an
    explicit SOURCE_REFERENCES edge - a bounded one-hop neighborhood, never
    a similarity search over content."""
    seen = {source_id}
    out: list[GraphNode] = []
    for mission_edge in store.edges_to(source_id, edge_types=(EdgeType.MISSION_USED_SOURCE,), limit=limit):
        for edge in store.edges_from(mission_edge.src_id, edge_types=(EdgeType.MISSION_USED_SOURCE,),
                                     limit=limit):
            if edge.dst_id in seen:
                continue
            seen.add(edge.dst_id)
            node = store.get_node(edge.dst_id)
            if node is not None:
                out.append(node)
            if len(out) >= limit:
                return out
    for edge in store.edges_from(source_id, edge_types=(EdgeType.SOURCE_REFERENCES,), limit=limit):
        if edge.dst_id in seen:
            continue
        seen.add(edge.dst_id)
        node = store.get_node(edge.dst_id)
        if node is not None:
            out.append(node)
    return out[:limit]


def provenance_chain(store: "GraphStore", node_id: str, *, max_hops: int = 6) -> list[GraphNode]:
    """"Where did this claim originally come from?" - follows DERIVED_FROM
    edges back to the original evidence, capped so a (should-never-happen)
    cycle can never hang a query."""
    chain: list[GraphNode] = []
    current = node_id
    seen: set[str] = set()
    for _ in range(max_hops):
        if current in seen:
            break
        seen.add(current)
        node = store.get_node(current)
        if node is None:
            break
        chain.append(node)
        edges = store.edges_from(current, edge_types=(EdgeType.DERIVED_FROM,), limit=1)
        if not edges:
            break
        current = edges[0].dst_id
    return chain

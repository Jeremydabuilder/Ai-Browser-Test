"""Node/edge identity and data shapes for the research graph.

Core architecture rule (Phase 19): this module stores identity,
relationships, lightweight metadata and provenance - never a second copy of
full source content. A node's ``data`` dict is a small summary (a title, a
normalized claim statement, a support count); the real text still lives in
its owning store - ``MissionStore``, ``HighlightStore``, ``KnowledgeStore`` -
which stays the single source of truth. A graph node only ever *points at*
that content via ``source_ref``/``mission_id``, the same "snapshot the
locator, not the body" convention ``MissionFinding.source_url`` and
``Highlight.url`` already use.

Node ids are deterministic, not surrogate keys, precisely so the same real
thing (the same URL, the same Mission) never gets a second node: building
the graph again for content already seen must be idempotent (Part "GRAPH
BUILDING": incremental, not a rebuild-everything sweep).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


class NodeType:
    MISSION = "mission"
    FINDING = "finding"
    WEBPAGE = "webpage"
    PDF = "pdf"
    FILE = "file"
    HIGHLIGHT = "highlight"
    TOPIC = "topic"
    CLAIM = "claim"

    #: "Source" in the phase brief's own vocabulary is any concrete
    #: evidence node - a page, a PDF, or a local file. There is no separate
    #: undifferentiated "Source" row: every real source in this codebase
    #: already resolves to one of these three concrete kinds, so keeping
    #: them distinct (rather than adding a fourth, vaguer "source" type)
    #: is more useful, not less - see the module docstring in builder.py.
    SOURCE_KINDS = frozenset({WEBPAGE, PDF, FILE})

    ALL = frozenset({MISSION, FINDING, WEBPAGE, PDF, FILE, HIGHLIGHT, TOPIC, CLAIM})


class EdgeType:
    MISSION_HAS_FINDING = "MISSION_HAS_FINDING"
    MISSION_USED_SOURCE = "MISSION_USED_SOURCE"
    FINDING_SUPPORTED_BY = "FINDING_SUPPORTED_BY"
    FINDING_CONTRADICTED_BY = "FINDING_CONTRADICTED_BY"
    SOURCE_REFERENCES = "SOURCE_REFERENCES"
    HIGHLIGHT_FROM_SOURCE = "HIGHLIGHT_FROM_SOURCE"
    CLAIM_SUPPORTED_BY = "CLAIM_SUPPORTED_BY"
    CLAIM_CONTRADICTED_BY = "CLAIM_CONTRADICTED_BY"
    RELATED_TO = "RELATED_TO"
    ABOUT_TOPIC = "ABOUT_TOPIC"
    DERIVED_FROM = "DERIVED_FROM"

    ALL = frozenset({
        MISSION_HAS_FINDING, MISSION_USED_SOURCE, FINDING_SUPPORTED_BY,
        FINDING_CONTRADICTED_BY, SOURCE_REFERENCES, HIGHLIGHT_FROM_SOURCE,
        CLAIM_SUPPORTED_BY, CLAIM_CONTRADICTED_BY, RELATED_TO, ABOUT_TOPIC,
        DERIVED_FROM,
    })


#: How a CLAIM_CONTRADICTED_BY edge's disagreement should be read - kept
#: distinct per the phase brief's explicit "do not flatten them all into
#: conflict" instruction. Stored in the edge's ``data["kind"]``.
class ContradictionKind:
    CONTRADICTION = "contradiction"
    SUPERSEDED = "superseded"
    DIFFERING_OPINION = "differing_opinion"


def _hash16(text: str) -> str:
    """Same convention as app.knowledge.index.url_source_id - the first 16
    hex chars of a sha256 digest - reused here rather than imported, since
    this module has no other dependency on app.knowledge and importing it
    just for one hash helper would be a needless coupling."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def normalize_topic(label: str) -> str:
    """A topic's identity is its normalized text, not a row id - so
    "MCP" and "mcp" (and "  MCP  ") are one Topic node, never two."""
    text = re.sub(r"\s+", " ", (label or "").strip().lower())
    return text


def normalize_claim_statement(text: str) -> str:
    """Loose normalization for claim-identity/comparison purposes only -
    collapses whitespace and case. Digits are kept (a price IS the claim),
    unlike the separate "base statement" comparison contradiction
    detection uses (see extraction.strip_numbers)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def mission_node_id(mission_id: int) -> str:
    return f"mission:{mission_id}"


def finding_node_id(finding_id: int) -> str:
    return f"finding:{finding_id}"


def webpage_node_id(url: str) -> str:
    return f"webpage:{_hash16(url)}"


def pdf_node_id(path: str) -> str:
    return f"pdf:{_hash16(path)}"


def file_node_id(path: str) -> str:
    return f"file:{_hash16(path)}"


def highlight_node_id(highlight_id: int) -> str:
    return f"highlight:{highlight_id}"


def topic_node_id(label: str) -> str:
    return f"topic:{_hash16(normalize_topic(label))}"


def claim_node_id(statement: str) -> str:
    return f"claim:{_hash16(normalize_claim_statement(statement))}"


def source_node_id(node_type: str, locator: str) -> str:
    """The node id for whichever concrete source kind this is - dispatches
    to the matching *_node_id helper above. ``node_type`` must be one of
    ``NodeType.SOURCE_KINDS``."""
    if node_type == NodeType.WEBPAGE:
        return webpage_node_id(locator)
    if node_type == NodeType.PDF:
        return pdf_node_id(locator)
    if node_type == NodeType.FILE:
        return file_node_id(locator)
    raise ValueError(f"'{node_type}' is not a source node type.")


@dataclass(frozen=True)
class GraphNode:
    id: str
    node_type: str
    title: str = ""
    #: Small, structured metadata - never a copy of full source content.
    #: For a Claim: normalized_statement/original_text/first_seen/
    #: last_seen/support_count/contradiction_count. For a WebPage/PDF/File:
    #: nothing beyond what title/source_ref already say, usually {}.
    data: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    #: A short, human-readable locator - a URL, a file path, a Mission's
    #: title - never the content itself. Mirrors ContentOrigin.source.
    source_ref: str = ""
    mission_id: int | None = None
    workspace_id: str | None = None
    extraction_method: str = ""
    confidence: float | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class GraphEdge:
    edge_type: str
    src_id: str
    dst_id: str
    id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    mission_id: int | None = None
    workspace_id: str | None = None
    confidence: float | None = None
    created_at: str = ""

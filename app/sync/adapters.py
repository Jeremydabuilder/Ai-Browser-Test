"""Per-domain SyncAdapters - the only place app/sync/ touches a domain
store directly. Each adapter builds a small, explicit payload dict (never
"every attribute this object happens to have") and knows how to apply one
back - see app/sync/engine.SyncAdapter for the four-method contract every
adapter here implements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.sync.engine import SyncAdapter
from app.sync.types import RecordType, SYNCABLE_SETTINGS_KEYS

if TYPE_CHECKING:
    from app.missions.repository import MissionStore
    from app.storage.highlights import HighlightStore
    from app.storage.knowledge_graph_store import GraphStore
    from app.storage.scheduled_tasks import ScheduledTaskStore
    from app.storage.settings import SettingsStore
    from app.storage.skills import SkillStore
    from app.storage.sync_store import GlobalIdStore, OwnershipStore
    from app.storage.watches import WatchStore
    from app.workspaces.store import WorkspaceStore

#: A Mission library beyond this size will not fully sync in one pass -
#: an explicit, documented cap rather than an unbounded query, matching
#: this codebase's existing "recent(limit=...)" convention everywhere else.
MAX_SYNCED_ROWS = 20_000


# ---------------------------------------------------------------------------
# Missions + findings
# ---------------------------------------------------------------------------

class MissionAdapter(SyncAdapter):
    record_type = RecordType.MISSION

    def __init__(self, store: "MissionStore", *, mission_ids: "set[int] | None" = None,
                share_workspace: bool = True) -> None:
        #: Phase 21: when given, this adapter only ever syncs these
        #: specific Missions - how a collaboration-scoped SyncEngine (one
        #: Mission, one collaboration key, one shared folder) reuses the
        #: exact same adapter as the personal, whole-profile sync in
        #: Phase 20 rather than needing a second implementation.
        self._store = store
        self._mission_ids = mission_ids
        #: Phase 21 Part 10: a shared Mission's workspace placement is
        #: local metadata, never forced onto a collaborator - a Mission's
        #: workspace_id is one device's own local workspace's id, which
        #: rarely even exists on a peer device (and could fail a foreign
        #: key if it doesn't). True (the default) preserves Phase 20's
        #: exact whole-profile personal-sync behavior, where every device
        #: is this same user's own and workspace_id really is shared
        #: state; CollaborationService passes False.
        self._share_workspace = share_workspace

    def iter_local_ids(self) -> list[str]:
        ids = [m.id for m in self._store.recent(limit=MAX_SYNCED_ROWS, with_pages=False)
              if not self._store.is_deleted(m.id)]
        if self._mission_ids is not None:
            ids = [i for i in ids if i in self._mission_ids]
        return [str(i) for i in ids]

    def in_scope(self, local_id: str, global_id: str) -> bool:
        if self._mission_ids is None:
            return True
        try:
            return int(local_id) in self._mission_ids
        except ValueError:
            return False

    def build_payload(self, local_id: str) -> dict | None:
        mission = self._store.get(int(local_id), with_pages=False)
        if mission is None or self._store.is_deleted(mission.id):
            return None
        payload = {
            "title": mission.title, "goal": mission.goal, "status": mission.status,
            "constraints": list(mission.constraints), "result": mission.result,
            "follow_ups": list(mission.follow_ups),
        }
        if self._share_workspace:
            payload["workspace_id"] = mission.workspace_id
        return payload

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        if local_id is None:
            workspace_id = payload.get("workspace_id") if self._share_workspace else None
            mission = self._store.create(payload.get("title", "") or "Untitled Mission",
                                         payload.get("goal", ""),
                                         workspace_id=workspace_id)
            if mission is None:
                return None
            local_id = str(mission.id)
        else:
            mission_id = int(local_id)
            self._store.rename(mission_id, payload.get("title", ""))
            self._store.set_goal(mission_id, payload.get("goal", ""))
            self._store.set_status(mission_id, payload.get("status", "active"))
        mission_id = int(local_id)
        self._store.set_constraints(mission_id, list(payload.get("constraints", [])))
        self._store.set_result(mission_id, payload.get("result", ""),
                               list(payload.get("follow_ups", [])))
        return local_id

    def apply_delete(self, local_id: str) -> None:
        self._store.soft_delete(int(local_id))


class MissionFindingAdapter(SyncAdapter):
    """Findings are append-oriented and merge by their own stable id
    (Part 10) - each Finding syncs as its own record, distinct from its
    Mission's. A finding whose parent Mission has not arrived on this
    device yet is simply skipped this cycle (returns None) and retried on
    the next sync, once MissionAdapter (processed first - see
    app/sync/service.py's adapter ordering) has created it."""

    record_type = RecordType.MISSION_FINDING

    def __init__(self, store: "MissionStore", global_ids: "GlobalIdStore", *,
                mission_ids: "set[int] | None" = None,
                graph: "object | None" = None, origin_device_id: str | None = None) -> None:
        self._store = store
        self._global_ids = global_ids
        self._mission_ids = mission_ids
        #: Phase 21 Part 9: when given, a newly-arrived (peer-authored)
        #: finding is also entered into the local Knowledge Graph, tagged
        #: Provenance.COLLABORATOR_CONTENT rather than the ordinary
        #: TRUSTED_APP_STATE a locally-typed finding gets - see
        #: app.knowledge_graph.builder.GraphBuilder.on_finding_saved.
        #: ``origin_device_id`` is this adapter's OWN device id, stamped
        #: onto payloads it builds so a peer receiving them can label the
        #: contributor; it is never used to tag graph nodes built here,
        #: since those nodes come from findings created by OTHERS.
        self._graph = graph
        self._origin_device_id = origin_device_id

    def iter_local_ids(self) -> list[str]:
        ids: list[str] = []
        for mission in self._store.recent(limit=MAX_SYNCED_ROWS, with_pages=False):
            if self._store.is_deleted(mission.id):
                continue
            if self._mission_ids is not None and mission.id not in self._mission_ids:
                continue
            for finding in self._store.findings(mission.id):
                ids.append(str(finding.id))
        return ids

    def build_payload(self, local_id: str) -> dict | None:
        finding = self._store.get_finding(int(local_id))
        if finding is None:
            return None
        mission_global_id = self._global_ids.get_global_id(RecordType.MISSION, str(finding.mission_id))
        if mission_global_id is None:
            return None  # parent Mission not yet synced from this device either
        payload = {"mission_global_id": mission_global_id, "text": finding.text,
                  "source_url": finding.source_url, "source_title": finding.source_title}
        if self._origin_device_id:
            payload["contributed_by"] = self._origin_device_id
        return payload

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        mission_local_id = self._global_ids.get_local_id(
            RecordType.MISSION, payload.get("mission_global_id", ""))
        if mission_local_id is None:
            return None  # parent Mission not present locally yet - retry later
        if local_id is not None:
            self._store.edit_finding(int(local_id), payload.get("text", ""))
            return local_id
        page_id = None
        source_url = payload.get("source_url", "")
        if source_url:
            from app.missions.model import PageOutcome, PageSource

            page = self._store.add_page(int(mission_local_id), source_url,
                                        payload.get("source_title", ""), PageSource.READ,
                                        outcome=PageOutcome.USEFUL)
            page_id = page.id if page is not None else None
        _outcome, finding = self._store.add_finding(int(mission_local_id), payload.get("text", ""),
                                                    page_id)
        if finding is not None and self._graph is not None:
            from app.security.provenance import Provenance

            mission = self._store.get(int(mission_local_id), with_pages=False)
            if mission is not None:
                self._graph.on_finding_saved(
                    finding_id=finding.id, mission=mission, text=finding.text,
                    source_url=source_url, source_title=payload.get("source_title", ""),
                    provenance=Provenance.COLLABORATOR_CONTENT,
                    contributed_by=payload.get("contributed_by"))
        return str(finding.id) if finding is not None else None

    def apply_delete(self, local_id: str) -> None:
        self._store.remove_finding(int(local_id))

    def in_scope(self, local_id: str, global_id: str) -> bool:
        if self._mission_ids is None:
            return True
        finding = self._store.get_finding(int(local_id)) if local_id.isdigit() else None
        if finding is None:
            # Already gone locally, and there is no surviving row to say
            # which Mission it belonged to - conservatively leave its
            # tombstone to whichever engine (e.g. the Phase 20 personal,
            # whole-profile one, which is always in-scope) can still
            # resolve it, rather than guessing.
            return False
        return finding.mission_id in self._mission_ids


# ---------------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------------

class HighlightAdapter(SyncAdapter):
    record_type = RecordType.HIGHLIGHT

    def __init__(self, store: "HighlightStore") -> None:
        self._store = store

    def iter_local_ids(self) -> list[str]:
        return [str(h.id) for h in self._store.all()]

    def build_payload(self, local_id: str) -> dict | None:
        highlight = self._store.get(int(local_id))
        if highlight is None:
            return None
        return {"url": highlight.url, "title": highlight.title, "text": highlight.text,
               "note": highlight.note}

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        if local_id is not None:
            self._store.set_note(int(local_id), payload.get("note", ""))
            return local_id
        highlight, _truncated = self._store.add(
            payload.get("url", ""), payload.get("title", ""), payload.get("text", ""),
            payload.get("note", ""))
        return str(highlight.id) if highlight is not None else None

    def apply_delete(self, local_id: str) -> None:
        self._store.remove(int(local_id))


# ---------------------------------------------------------------------------
# Skills (including recorded workflows - already secret-scrubbed at
# recording time, Phase 16's parameterization/secret-placeholder step)
# ---------------------------------------------------------------------------

class SkillAdapter(SyncAdapter):
    """Built-in Skills never appear here at all - SkillStore.all() only
    ever returns custom ones (see its own module docstring); a Skill's own
    id is already a stable string (assigned at creation), used directly."""

    record_type = RecordType.SKILL
    deterministic_ids = True

    def __init__(self, store: "SkillStore") -> None:
        self._store = store

    def iter_local_ids(self) -> list[str]:
        return [skill.id for skill in self._store.all()]

    def build_payload(self, local_id: str) -> dict | None:
        skill = self._store.get(local_id)
        if skill is None:
            return None
        return skill.as_dict()

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        from app.agent.skills import Skill

        data = dict(payload)
        data["id"] = local_id or global_id
        skill = Skill.from_dict(data)
        saved = self._store.save(skill)
        return saved.id if saved is not None else None

    def apply_delete(self, local_id: str) -> None:
        self._store.remove(local_id)


# ---------------------------------------------------------------------------
# Workspaces - metadata, tab URLs/order/groups. NEVER cookies/login
# sessions (Part 13): isolated_profile/profile_storage_name are excluded
# from the payload below, so an isolated workspace's real browser profile
# - and everything logged into it - always stays device-local; a new
# device gets the workspace's tabs/settings with a fresh, empty profile.
# ---------------------------------------------------------------------------

class WorkspaceAdapter(SyncAdapter):
    deterministic_ids = True
    record_type = RecordType.WORKSPACE

    def __init__(self, store: "WorkspaceStore") -> None:
        self._store = store

    def iter_local_ids(self) -> list[str]:
        return [w.id for w in self._store.all()]

    def build_payload(self, local_id: str) -> dict | None:
        workspace = self._store.get(local_id)
        if workspace is None:
            return None
        data = workspace.as_dict()
        data.pop("isolated_profile", None)
        data.pop("profile_storage_name", None)
        return data

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        from app.workspaces.model import Workspace

        data = dict(payload)
        data["id"] = local_id or global_id
        existing = self._store.get(data["id"]) if local_id is not None else None
        # Preserve this device's own isolation choice - never imported from
        # a peer (Part 13).
        data["isolated_profile"] = existing.isolated_profile if existing is not None else False
        data["profile_storage_name"] = existing.profile_storage_name if existing is not None else ""
        workspace = Workspace.from_dict(data)
        saved = self._store.save(workspace)
        return saved.id if saved is not None else None

    def apply_delete(self, local_id: str) -> None:
        self._store.delete(local_id)


# ---------------------------------------------------------------------------
# Scheduled Tasks / Watches - definitions sync; ONE device executes/polls
# (Part 11/12). ``OwnershipStore`` (not the sync payload) is the single
# source of truth for who runs it, so ownership itself never becomes a
# field two devices could race to overwrite via ordinary sync.
# ---------------------------------------------------------------------------

class ScheduledTaskAdapter(SyncAdapter):
    record_type = RecordType.SCHEDULED_TASK

    def __init__(self, store: "ScheduledTaskStore", ownership: "OwnershipStore",
                device_id: str) -> None:
        self._store = store
        self._ownership = ownership
        self._device_id = device_id

    def iter_local_ids(self) -> list[str]:
        return [str(t.id) for t in self._store.all()]

    def build_payload(self, local_id: str) -> dict | None:
        task = self._store.get(int(local_id))
        if task is None:
            return None
        self._ownership.set_if_unset(self.record_type, local_id, self._device_id)
        return {
            "goal": task.goal, "schedule_kind": task.schedule_kind,
            "schedule_at": task.schedule_at, "time_of_day": task.time_of_day,
            "weekday": task.weekday, "interval_seconds": task.interval_seconds,
            "workspace_id": task.workspace_id,
            "execution_device": self._ownership.get(self.record_type, local_id) or self._device_id,
        }

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        if local_id is None:
            task = self._store.create(
                goal=payload.get("goal", ""), schedule_kind=payload.get("schedule_kind", "once"),
                schedule_at=payload.get("schedule_at"), time_of_day=payload.get("time_of_day"),
                weekday=payload.get("weekday"), interval_seconds=payload.get("interval_seconds"),
                workspace_id=payload.get("workspace_id"))
            if task is None:
                return None
            # A task that arrives from a peer keeps that peer's declared
            # owner - never silently becomes owned by this device (Part 11).
            self._ownership.set(self.record_type, str(task.id),
                                payload.get("execution_device", self._device_id))
            return str(task.id)
        return local_id  # definition edits beyond creation are out of scope this phase

    def apply_delete(self, local_id: str) -> None:
        self._store.remove(int(local_id))
        self._ownership.remove(self.record_type, local_id)


class WatchAdapter(SyncAdapter):
    record_type = RecordType.WATCH

    def __init__(self, store: "WatchStore", ownership: "OwnershipStore", device_id: str) -> None:
        self._store = store
        self._ownership = ownership
        self._device_id = device_id

    def iter_local_ids(self) -> list[str]:
        return [str(w.id) for w in self._store.all()]

    def build_payload(self, local_id: str) -> dict | None:
        watch = self._store.get(int(local_id))
        if watch is None:
            return None
        self._ownership.set_if_unset(self.record_type, local_id, self._device_id)
        return {
            "title": watch.title, "url": watch.url, "target_type": watch.target_type,
            "selection_hint": watch.selection_hint, "condition": watch.condition,
            "condition_value": watch.condition_value,
            "check_interval_seconds": watch.check_interval_seconds,
            "execution_device": self._ownership.get(self.record_type, local_id) or self._device_id,
        }

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        if local_id is None:
            watch = self._store.create(
                title=payload.get("title", ""), url=payload.get("url", ""),
                target_type=payload.get("target_type", "page"),
                condition=payload.get("condition", ""),
                check_interval_seconds=int(payload.get("check_interval_seconds", 3600)),
                selection_hint=payload.get("selection_hint", ""),
                condition_value=payload.get("condition_value", ""))
            if watch is None:
                return None
            self._ownership.set(self.record_type, str(watch.id),
                                payload.get("execution_device", self._device_id))
            return str(watch.id)
        return local_id

    def apply_delete(self, local_id: str) -> None:
        self._store.remove(int(local_id))
        self._ownership.remove(self.record_type, local_id)


# ---------------------------------------------------------------------------
# Knowledge Graph - only nodes whose id is already content-derived and
# therefore stable across devices (Topic/WebPage/PDF/File/Claim); Mission/
# Finding/Highlight graph nodes embed a local row id and are not synced by
# this adapter (see the module docstring in app/knowledge_graph/types.py -
# a Mission/Finding's own record still syncs fully via MissionAdapter/
# MissionFindingAdapter above, just not its graph-node representation).
# Local semantic-index embeddings are never synced at all (Part 14) - they
# are cheaply rebuildable from the same local content on any device.
# ---------------------------------------------------------------------------

STABLE_GRAPH_NODE_TYPES = ("topic", "webpage", "pdf", "file", "claim")


class GraphNodeAdapter(SyncAdapter):
    deterministic_ids = True
    record_type = RecordType.GRAPH_NODE

    def __init__(self, store: "GraphStore") -> None:
        self._store = store

    def iter_local_ids(self) -> list[str]:
        ids: list[str] = []
        for node_type in STABLE_GRAPH_NODE_TYPES:
            ids.extend(node.id for node in self._store.nodes_by_type(node_type, limit=MAX_SYNCED_ROWS))
        return ids

    def build_payload(self, local_id: str) -> dict | None:
        node = self._store.get_node(local_id)
        if node is None or node.node_type not in STABLE_GRAPH_NODE_TYPES:
            return None
        return {"node_type": node.node_type, "title": node.title, "data": node.data,
               "provenance": node.provenance, "source_ref": node.source_ref}

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        from app.knowledge_graph.types import GraphNode

        node_id = local_id or global_id
        if not node_id:
            return None
        node = GraphNode(id=node_id, node_type=payload.get("node_type", ""),
                         title=payload.get("title", ""), data=payload.get("data", {}),
                         provenance=payload.get("provenance", ""),
                         source_ref=payload.get("source_ref", ""))
        saved = self._store.upsert_node(node)
        return saved.id if saved is not None else None

    def apply_delete(self, local_id: str) -> None:
        self._store.remove_node(local_id)


class GraphEdgeAdapter(SyncAdapter):
    """An edge's own deterministic id is its (edge_type, src, dst) triple -
    only ever synced when both endpoints are stable-typed nodes (see
    STABLE_GRAPH_NODE_TYPES above)."""

    deterministic_ids = True
    record_type = RecordType.GRAPH_EDGE

    def __init__(self, store: "GraphStore") -> None:
        self._store = store

    @staticmethod
    def _key(edge_type: str, src_id: str, dst_id: str) -> str:
        return f"{edge_type}|{src_id}|{dst_id}"

    def iter_local_ids(self) -> list[str]:
        seen: set[str] = set()
        ids: list[str] = []
        for node_type in STABLE_GRAPH_NODE_TYPES:
            for node in self._store.nodes_by_type(node_type, limit=MAX_SYNCED_ROWS):
                for edge in self._store.edges_from(node.id, limit=MAX_SYNCED_ROWS):
                    dst = self._store.get_node(edge.dst_id)
                    if dst is None or dst.node_type not in STABLE_GRAPH_NODE_TYPES:
                        continue
                    key = self._key(edge.edge_type, edge.src_id, edge.dst_id)
                    if key not in seen:
                        seen.add(key)
                        ids.append(key)
        return ids

    def build_payload(self, local_id: str) -> dict | None:
        edge_type, src_id, dst_id = local_id.split("|", 2)
        edges = self._store.edges_from(src_id, edge_types=(edge_type,), limit=MAX_SYNCED_ROWS)
        match = next((e for e in edges if e.dst_id == dst_id), None)
        if match is None:
            return None
        return {"edge_type": match.edge_type, "src_id": match.src_id, "dst_id": match.dst_id,
               "data": match.data, "provenance": match.provenance}

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        from app.knowledge_graph.types import GraphEdge

        edge = GraphEdge(edge_type=payload.get("edge_type", ""), src_id=payload.get("src_id", ""),
                         dst_id=payload.get("dst_id", ""), data=payload.get("data", {}),
                         provenance=payload.get("provenance", ""))
        if self._store.get_node(edge.src_id) is None or self._store.get_node(edge.dst_id) is None:
            return None  # endpoint node not synced to this device yet - retry later
        self._store.upsert_edge(edge)
        return self._key(edge.edge_type, edge.src_id, edge.dst_id)

    def apply_delete(self, local_id: str) -> None:
        edge_type, src_id, dst_id = local_id.split("|", 2)
        self._store.remove_edge(edge_type, src_id, dst_id)


# ---------------------------------------------------------------------------
# Settings - one small, allowlisted, singleton record (Part 16/21)
# ---------------------------------------------------------------------------

class SettingsAdapter(SyncAdapter):
    deterministic_ids = True
    record_type = RecordType.SETTINGS
    _SINGLETON_ID = "default"

    def __init__(self, settings: "SettingsStore") -> None:
        self._settings = settings

    def iter_local_ids(self) -> list[str]:
        return [self._SINGLETON_ID]

    def build_payload(self, local_id: str) -> dict | None:
        return {key: self._settings.get(key, "") for key in sorted(SYNCABLE_SETTINGS_KEYS)}

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        for key in SYNCABLE_SETTINGS_KEYS:
            if key in payload:
                self._settings.set(key, payload[key])
        return self._SINGLETON_ID

    def apply_delete(self, local_id: str) -> None:
        pass  # settings are never meaningfully "deleted", only overwritten

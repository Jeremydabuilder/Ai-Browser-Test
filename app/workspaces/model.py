"""What a Workspace is, and - just as importantly - what it is not.

**Workspace != Isolated Profile.** A Workspace is a *scoping* boundary: its
own tabs, its own default Mission scope, its own knowledge-search scope, its
own MCP/Skill visibility and provider preference. By default several
Workspaces still share one QWebEngineProfile - the same cookies, the same
login state - because that is what "School" and "Personal" usually want to
be: separate *contexts*, not separate *browsers*. An Isolated Profile is the
much stronger claim that a Workspace's cookies/local storage/cache are
genuinely separate from every other Workspace's, backed by a second, real
QWebEngineProfile (see app/browser/profile.py). ``Workspace.isolated_profile``
is what actually turns that on; nothing in this module, or the UI built on
it, is ever allowed to describe plain Workspace separation as "isolated" -
see app/ui/workspace_switcher.py's own wording.

That distinction is not cosmetic. app/browser/internal.py's claim_scheme()
documents a measured Qt WebEngine 6.11 limit: only the *first*
QWebEngineProfile created in the process can ever serve PyBrowser's own
``pybrowser://`` pages (the New Tab dashboard, the Mission Library) - a
second profile's tabs simply cannot show them, with no error. An isolated
workspace's tabs therefore fall back to a plain blank new-tab page rather
than PyBrowser's own dashboard; see app/workspaces/profiles.py. That is a
real, disclosed trade-off of real cookie isolation, not a bug to paper over.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

DEFAULT_ICON = "🗂"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class TabRecord:
    """One tab's worth of session state - what a Workspace remembers about
    a tab across a switch or a restart. Never a live BrowserTab: this is
    exactly what get serialized when a workspace is deactivated and what a
    reactivation replays through TabManager.new_tab()/set_pinned()/
    move_tab_to_group()."""

    url: str
    pinned: bool = False
    group_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "pinned": self.pinned, "group_id": self.group_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TabRecord":
        return cls(url=data.get("url", ""), pinned=bool(data.get("pinned", False)),
                  group_id=data.get("group_id"))


@dataclass(frozen=True)
class TabState:
    """A whole workspace's tab strip: every tab, its groups, and which tab
    was active - everything TabManager needs to look the same again after
    a switch, restored in order."""

    tabs: tuple[TabRecord, ...] = field(default_factory=tuple)
    #: group_id -> {"name":..., "collapsed": bool}, mirroring
    #: TabManager._groups exactly.
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_index: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tabs": [t.as_dict() for t in self.tabs],
            "groups": dict(self.groups),
            "active_index": self.active_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TabState":
        if not data:
            return cls()
        return cls(
            tabs=tuple(TabRecord.from_dict(t) for t in data.get("tabs") or []),
            groups=dict(data.get("groups") or {}),
            active_index=int(data.get("active_index", 0)),
        )

    @property
    def is_empty(self) -> bool:
        return not self.tabs


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    icon: str = DEFAULT_ICON
    color: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    #: "" = use the app-wide default new-tab address (Settings).
    homepage: str = ""
    #: A *preference*, never a hard lock - see MainWindow._resolve_provider_
    #: for_workspace, which falls back visibly when unavailable.
    preferred_provider: str = ""
    preferred_model: str = ""
    #: None = every connected MCP server is visible here (today's global
    #: behaviour, unchanged). A tuple restricts visibility to exactly those
    #: server ids - never a second copy of a server's credentials, just a
    #: filter over the one global app.mcp.connection_manager list.
    mcp_visible_server_ids: tuple[str, ...] | None = None
    #: True = this workspace's tabs use their own QWebEngineProfile (real
    #: cookie/storage isolation - see the module docstring on the one
    #: trade-off that comes with it). False (default) = this workspace
    #: shares the app's one default profile, exactly like every workspace
    #: before this phase existed.
    isolated_profile: bool = False
    #: The QWebEngineProfile storage_name to use when isolated_profile is
    #: True - stable for the workspace's whole life so its storage
    #: directory never moves under it. Empty when isolated_profile is False.
    profile_storage_name: str = ""
    tab_state: TabState = field(default_factory=TabState)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "icon": self.icon, "color": self.color,
            "created_at": self.created_at, "last_used_at": self.last_used_at,
            "homepage": self.homepage,
            "preferred_provider": self.preferred_provider,
            "preferred_model": self.preferred_model,
            "mcp_visible_server_ids": (list(self.mcp_visible_server_ids)
                                      if self.mcp_visible_server_ids is not None else None),
            "isolated_profile": self.isolated_profile,
            "profile_storage_name": self.profile_storage_name,
            "tab_state": self.tab_state.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workspace":
        visible = data.get("mcp_visible_server_ids")
        return cls(
            id=data["id"], name=data.get("name", ""), icon=data.get("icon", DEFAULT_ICON),
            color=data.get("color", ""),
            created_at=float(data.get("created_at", 0.0)),
            last_used_at=float(data.get("last_used_at", 0.0)),
            homepage=data.get("homepage", ""),
            preferred_provider=data.get("preferred_provider", ""),
            preferred_model=data.get("preferred_model", ""),
            mcp_visible_server_ids=tuple(visible) if visible is not None else None,
            isolated_profile=bool(data.get("isolated_profile", False)),
            profile_storage_name=data.get("profile_storage_name", ""),
            tab_state=TabState.from_dict(data.get("tab_state")),
        )

    def touched(self) -> "Workspace":
        return replace(self, last_used_at=time.time())

    def with_tab_state(self, tab_state: TabState) -> "Workspace":
        return replace(self, tab_state=tab_state)


def new_workspace(name: str, *, icon: str = DEFAULT_ICON, color: str = "",
                  isolated_profile: bool = False) -> Workspace:
    now = time.time()
    workspace_id = new_id()
    return Workspace(
        id=workspace_id, name=name.strip() or "Workspace", icon=icon, color=color,
        created_at=now, last_used_at=now, isolated_profile=isolated_profile,
        profile_storage_name=f"workspace-{workspace_id}" if isolated_profile else "",
    )


#: The id of the one Workspace every profile is guaranteed to have, created
#: on first launch (or synthesised for a profile that pre-dates Phase 17 -
#: see WorkspaceStore.ensure_default). Never deletable - see
#: WorkspaceStore.delete - so there is always somewhere for global
#: Missions/tabs to land.
DEFAULT_WORKSPACE_ID = "default"


def default_workspace() -> Workspace:
    now = time.time()
    return Workspace(id=DEFAULT_WORKSPACE_ID, name="Home", icon=DEFAULT_ICON,
                     created_at=now, last_used_at=now)

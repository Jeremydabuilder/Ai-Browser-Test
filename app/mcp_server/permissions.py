"""Least-privilege enforcement: which Capability each exposed tool needs,
and the single helper that checks a PairedClient against it. A newly
paired client has an empty capability set, so every tool is denied until
the user explicitly grants it via the pairing/review UI - see
app/mcp_server/auth.py's pair_client and app/ui's External AI Access panel.
"""

from __future__ import annotations

from app.mcp_server.types import Capability, PairedClient

TOOL_REQUIRED_CAPABILITY: dict[str, Capability] = {
    "browser.current_page": Capability.READ_PAGES,
    "browser.read_page": Capability.READ_PAGES,
    "browser.get_selected_text": Capability.READ_PAGES,
    "browser.list_tabs": Capability.READ_TABS,
    "browser.search": Capability.READ_TABS,
    "browser.open_tab": Capability.OPEN_TABS,
    "browser.navigate": Capability.NAVIGATE,
    "mission.list": Capability.READ_MISSIONS,
    "mission.get": Capability.READ_MISSIONS,
    "mission.get_findings": Capability.READ_MISSIONS,
    "mission.get_sources": Capability.READ_MISSIONS,
    "mission.get_plan": Capability.READ_MISSIONS,
    "mission.create": Capability.CREATE_MISSION,
}


def required_capability(tool: str) -> Capability | None:
    return TOOL_REQUIRED_CAPABILITY.get(tool)


def is_permitted(client: PairedClient, tool: str) -> bool:
    """Fail closed: an unknown tool name is never permitted, and a client
    can never grant itself a capability it wasn't paired with - there is
    no code path here that adds to client.capabilities."""
    capability = required_capability(tool)
    if capability is None:
        return False
    return client.has(capability)

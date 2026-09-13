"""PyBrowser as an MCP Server (Phase 11).

Exposes a safe, authenticated, least-privilege subset of PyBrowser's
browser and Mission capabilities to external MCP clients over Streamable
HTTP, bound to 127.0.0.1 only. This package adapts external calls onto the
EXISTING BrowserController / MissionService / MissionGraphStore / safety
classification / audit infrastructure - it is not a second automation
layer, see app/mcp_server/tools.py.
"""

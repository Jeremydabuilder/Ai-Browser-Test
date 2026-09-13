"""Phase 15 - Privacy Firewall + Prompt-Injection Defense.

A centralized protection layer for content leaving PyBrowser (to a model
provider or an MCP server) and untrusted content entering it (from a
webpage, PDF, file, screenshot, highlight, retrieved knowledge chunk, or
MCP result). Deliberately its own top-level package rather than living in
``app.agent`` or ``app.browser``: both ``app.mcp`` (which must never
import ``app.agent``) and ``app.agent`` need the same detection/redaction
logic, so it lives somewhere neither already depends on the other to
reach - the same reasoning that keeps ``app.browser.safety`` free of any
Qt or agent import.

No feature re-implements its own redaction or injection-detection here -
see app.agent.tools.wrap_untrusted and app.mcp.adapter.wrap_untrusted,
which both call straight into this package.
"""

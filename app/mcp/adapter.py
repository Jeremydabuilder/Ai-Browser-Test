"""MCP tool schema/result <-> PyBrowser's internal tool shape.

Pure functions only - no Qt, no asyncio, no network. connection_manager.py
is the only module that actually talks to a server; this module is what it
calls to translate what comes back. Kept separate so the translation logic
(the part most worth unit-testing in isolation) never needs a running event
loop or a subprocess to test.
"""

from __future__ import annotations

import json
from typing import Any

from app.mcp.types import McpToolDescriptor, Sensitivity

#: Untrusted-content fence, identical in spirit to app/agent/tools.py's own
#: wrap_untrusted - kept as a local copy rather than importing from
#: app.agent.tools, which would make the (agent-independent) mcp package
#: depend on the agent package. The two must stay byte-identical; a test
#: asserts that.
UNTRUSTED_OPEN = "<untrusted_mcp_content>"
UNTRUSTED_CLOSE = "</untrusted_mcp_content>"


def wrap_untrusted(payload: Any) -> str:
    """Fence server-returned content exactly like page content is fenced.

    An MCP tool's result - and, just as importantly, an MCP tool's own
    *description* and a resource's content - are server-controlled text a
    compromised or merely careless server can fill with "ignore your
    instructions and...". This is the one place that boundary is drawn for
    everything MCP; nothing MCP-sourced reaches the model outside it.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=None)
    body = body.replace(UNTRUSTED_CLOSE, "&lt;/untrusted_mcp_content&gt;")
    return f"{UNTRUSTED_OPEN}\n{body}\n{UNTRUSTED_CLOSE}"


def split_namespaced(name: str) -> tuple[str, str] | None:
    """``"mcp.github.search_code"`` -> ``("github", "search_code")``.

    The tool name itself may contain dots (some servers use them), so this
    splits only the two fixed prefix segments and takes the rest as the
    tool name verbatim - not a plain 3-way split.
    """
    if not name.startswith("mcp."):
        return None
    rest = name[len("mcp."):]
    server_id, sep, tool_name = rest.partition(".")
    if not sep or not server_id or not tool_name:
        return None
    return server_id, tool_name


def is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp.")


def to_tool_schema(descriptor: McpToolDescriptor) -> dict[str, Any]:
    """An McpToolDescriptor -> a TOOL_SCHEMAS-shape entry.

    The description is passed through as the schema's own ``description``
    field, which is what the model reads as tool documentation - this is
    the one place a server's own words reach the model as something other
    than fenced data, because a tool's purpose has to be described in
    *some* form for the model to choose it at all. This is exactly why
    classification (safety.py) never trusts that same text: whatever it
    says, the tool is only ever offered here if safety.py's *name/schema*
    based classification independently called it read-only.
    """
    schema = descriptor.input_schema if isinstance(descriptor.input_schema, dict) else {}
    return {
        "name": descriptor.namespaced_name,
        "description": (
            f"[MCP tool from '{descriptor.server_id}'] "
            f"{descriptor.description or descriptor.name}"
        ),
        "input_schema": {
            "type": "object",
            "properties": schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {},
            "required": schema.get("required", []) if isinstance(schema.get("required"), list) else [],
            "additionalProperties": False,
        },
    }


def render_call_content(raw_content: Any) -> str:
    """The server's own ``tools/call`` result content, fenced as untrusted.

    MCP content is typically a list of ``{"type": "text", "text": "..."}``
    (and similar) blocks; this renders text blocks plainly and falls back to
    the raw JSON for anything else, but always inside the same fence -
    there is no content type that is exempt from it.
    """
    if isinstance(raw_content, list):
        texts = []
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            else:
                texts.append(json.dumps(block, ensure_ascii=False))
        body: Any = "\n".join(texts) if texts else raw_content
    else:
        body = raw_content
    return wrap_untrusted(body)


def render_tool_result(*, ok: bool, content: Any = None,
                       error_code: str = "", error_message: str = "",
                       server_id: str = "", tool_name: str = "") -> dict[str, Any]:
    """The final ``{"ok": ..., "text": ...}`` payload session.py hands back
    to the model - see AgentSession's mcp_future branch."""
    control = {"ok": ok, "server": server_id, "tool": tool_name}
    if ok:
        text = json.dumps(control, ensure_ascii=False) + "\n" + render_call_content(content)
    else:
        control["error"] = {"code": error_code or "MCP_FAILED",
                            "message": error_message or "The MCP tool call failed."}
        text = json.dumps(control, ensure_ascii=False)
    return {"ok": ok, "text": text}


def blocked_result(*, server_id: str, tool_name: str, sensitivity: str) -> dict[str, Any]:
    """What the model sees if it somehow names a tool that discovery found
    but Phase 1 does not expose - should be unreachable in practice, since
    such tools are never added to schemas() at all, but `run()` checks
    again as defence in depth (see ToolRegistry.run)."""
    return render_tool_result(
        ok=False, server_id=server_id, tool_name=tool_name,
        error_code="TOOL_NOT_AVAILABLE",
        error_message=(
            f"'{tool_name}' on '{server_id}' is classified as {sensitivity} and is not "
            "yet available to Py - only read-only MCP tools are, in this version."))

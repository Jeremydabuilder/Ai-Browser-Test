"""PyBrowser as an MCP client (Phase 1: read-only tools only).

See docs/mcp_openai_architecture.md for the full design. This package is
deliberately isolated from the rest of the agent system - everything it
exposes to app/agent/tools.py is a plain data adapter
(McpAdapter.tool_schemas() / McpAdapter.run()), the same shape as any other
tool, so ToolRegistry does not need to know MCP exists beyond one namespace
check and one dispatch branch.

Modules
-------
types.py               Plain dataclasses: server config, tool descriptors,
                        call results. No Qt, no I/O.
safety.py               Pattern-based sensitivity classifier. Pure function,
                        no Qt, no I/O - a server's own tool description is
                        never trusted to say how sensitive it is.
protocol.py             The MCP JSON-RPC wire protocol over stdio and
                        Streamable HTTP. Pure asyncio, no Qt.
config.py               Persistence: non-secret server config in the
                        settings table, secrets in the OS keyring (reusing
                        app.agent.keys' keyring plumbing).
connection_manager.py    The only Qt-aware module. Owns a background thread
                        running the asyncio loop that every connection's
                        protocol client runs on, and marshals results back
                        to the GUI thread via Qt signals - the same
                        QThread + signal pattern app.agent.session already
                        uses for the Claude worker.
adapter.py              MCP tool schema -> TOOL_SCHEMAS-shape entry;
                        namespacing; the read-only gate.
"""

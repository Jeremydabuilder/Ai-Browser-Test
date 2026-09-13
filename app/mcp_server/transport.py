"""JSON-RPC 2.0 message framing for the server side of Streamable HTTP -
the mirror image of app/mcp/protocol.py's client-side ``_validate_message``,
``PROTOCOL_VERSION`` and method names. One endpoint (POST /mcp), one JSON
request in, one JSON response out - no SSE, no `Mcp-Session-Id`: every call
here already carries its own Bearer token, so there is no server-side
session state a session id would need to key into.
"""

from __future__ import annotations

import json
from typing import Any

from app.mcp.protocol import PROTOCOL_VERSION

_SERVER_INFO = {"name": "PyBrowser", "version": "0.1.0"}

# JSON-RPC 2.0 standard error codes, plus a small server-defined range for
# this server's own auth/permission failures (-32000 to -32099 is reserved
# for implementation-defined server errors by the spec).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001
FORBIDDEN = -32002
SERVER_DISABLED = -32003


class TransportError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_request(raw: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError(PARSE_ERROR, "Malformed JSON.") from exc
    if not isinstance(obj, dict):
        raise TransportError(INVALID_REQUEST, "Expected a JSON object.")
    if obj.get("jsonrpc") != "2.0":
        raise TransportError(INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
    if "method" not in obj:
        raise TransportError(INVALID_REQUEST, "Missing 'method'.")
    return obj


def build_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": _SERVER_INFO,
    }

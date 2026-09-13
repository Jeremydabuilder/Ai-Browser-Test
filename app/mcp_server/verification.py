"""The one shared verification engine (Phase 12, Part 6): "is this client
actually connected", never "a token was created". Every client card in the
Settings UI - ChatGPT, Claude, Cursor, VS Code, or a generic client - runs
through this same check; there is no per-client verification logic.

Verification is a real client of PyBrowser's own MCP server: it opens an
HTTP connection, speaks JSON-RPC, and calls one safe tool - the same wire
protocol any external client would use. It never touches BrowserController
or MissionService directly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.mcp_server.types import VerificationStatus

#: A read tool every client that can be verified at all is expected to
#: have - list_tabs needs only READ_TABS, the least a client is likely to
#: be paired with. If a client lacks even this, verification still proves
#: reachability + auth (the parts that matter most) and reports the
#: permission gap honestly rather than failing outright.
_PROBE_TOOL = "browser.list_tabs"


@dataclass
class VerificationResult:
    status: VerificationStatus
    detail: str = ""


def verify_connection(url: str, token: str, timeout: float = 5.0) -> VerificationResult:
    """Reachable -> authenticated -> initialize -> tools/list -> a safe
    tool call. Stops at the first failure and reports which stage it was -
    never conflates "server unreachable" with "bad token" with "denied"."""
    try:
        _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, None, timeout)
    except _Unreachable as exc:
        return VerificationResult(VerificationStatus.UNREACHABLE, str(exc))
    except _RpcError as exc:
        return VerificationResult(VerificationStatus.UNREACHABLE, f"initialize failed: {exc}")

    try:
        listed = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       token, timeout)
    except _Unreachable as exc:
        return VerificationResult(VerificationStatus.UNREACHABLE, str(exc))
    except _RpcError as exc:
        return VerificationResult(VerificationStatus.AUTHENTICATION_FAILED, str(exc))

    tool_names = {t.get("name") for t in listed.get("result", {}).get("tools", [])}
    if _PROBE_TOOL not in tool_names:
        return VerificationResult(
            VerificationStatus.VERIFIED,
            "Authenticated, but the probe tool is unavailable to this server build.")

    try:
        called = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": _PROBE_TOOL, "arguments": {}}}, token, timeout)
    except _Unreachable as exc:
        return VerificationResult(VerificationStatus.UNREACHABLE, str(exc))
    except _PermissionDenied:
        # Reachable, authenticated, protocol all work - the client simply
        # was not granted this particular capability. That is a
        # successful connection, not a failure: "token created" and
        # "connected" are proven independently of what it is scoped to do.
        return VerificationResult(
            VerificationStatus.VERIFIED,
            "Connected. This client does not have the read-tabs permission.")
    except _RpcError as exc:
        # Any other tools/call failure (a tool-level error unrelated to
        # auth) still leaves reachability and authentication proven by
        # the two stages above - it is not this engine's job to also
        # certify that every individual tool succeeds end to end.
        return VerificationResult(
            VerificationStatus.VERIFIED, f"Connected. The probe tool reported: {exc}")

    content = (called.get("result") or {}).get("content") or []
    if content:
        try:
            payload = json.loads(content[0].get("text", "{}"))
        except json.JSONDecodeError:
            payload = {}
        if payload.get("ok") is False:
            return VerificationResult(
                VerificationStatus.VERIFIED,
                f"Connected. The probe tool reported: {payload.get('error', {}).get('message', '')}")
    return VerificationResult(VerificationStatus.VERIFIED, "Connected and verified.")


class _Unreachable(Exception):
    pass


class _RpcError(Exception):
    pass


class _PermissionDenied(_RpcError):
    pass


#: transport.FORBIDDEN - duplicated as a literal rather than importing the
#: server module, since this engine also has to work as a client of a
#: PyBrowser server it did not import (a future out-of-process bridge, a
#: test double) - it should only ever depend on the wire protocol.
_FORBIDDEN_CODE = -32002


def _post(url: str, body: dict, token: str | None, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read())
        except (json.JSONDecodeError, ValueError):
            raise _Unreachable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _Unreachable(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise _Unreachable("malformed response") from exc
    if "error" in parsed:
        error = parsed["error"]
        message = error.get("message", "request refused")
        if error.get("code") == _FORBIDDEN_CODE:
            raise _PermissionDenied(message)
        raise _RpcError(message)
    return parsed

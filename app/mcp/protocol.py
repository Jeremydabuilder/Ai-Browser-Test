"""The MCP wire protocol itself - JSON-RPC 2.0 over stdio or Streamable HTTP.

Pure asyncio, no Qt, no PyBrowser-specific concepts (no sensitivity, no
namespacing - that is the adapter's job, one layer up). Verified against the
MCP spec's currently-sanctioned transports: stdio (newline-delimited JSON-RPC
over a spawned subprocess's stdin/stdout) and Streamable HTTP (a single
``/mcp``-style endpoint, POST for requests, a plain JSON or
``text/event-stream`` response). The older dual-endpoint HTTP+SSE transport
is legacy per the spec and is intentionally not implemented here.

Both client classes below expose the same small async surface -
``connect()``, ``list_tools()``, ``call_tool()``, ``close()`` - so
connection_manager.py can hold either behind one attribute without an
if/else on transport kind anywhere except at construction time.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2 as httpx

#: The MCP protocol version this client speaks. Sent verbatim in
#: ``initialize``; a server that only supports an older version is expected
#: to negotiate down (or refuse), same as any versioned protocol handshake.
PROTOCOL_VERSION = "2026-07-28"

_CLIENT_INFO = {"name": "PyBrowser", "version": "0.1.0"}


class McpProtocolError(Exception):
    """Something at the JSON-RPC/transport level went wrong.

    Distinct from a tool reporting its own failure (see McpCallResult in
    types.py) - this is "the connection itself failed", not "the tool ran
    and said no".
    """


class McpTimeoutError(McpProtocolError):
    pass


def _validate_message(obj: Any) -> dict[str, Any]:
    """A parsed JSON-RPC message, or a loud error for anything malformed.

    A server's own output is untrusted input like anything else an MCP
    server sends - a non-object, non-JSON-RPC-shaped line is a protocol
    error, never silently ignored or forwarded as if it were legitimate.
    """
    if not isinstance(obj, dict):
        raise McpProtocolError(f"expected a JSON object, got {type(obj).__name__}")
    if obj.get("jsonrpc") != "2.0":
        raise McpProtocolError("not a JSON-RPC 2.0 message")
    return obj


class _RequestIdCounter:
    def __init__(self) -> None:
        self._next = 1

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value


class StdioMcpClient:
    """One MCP server, spawned as a local subprocess.

    Newline-delimited JSON-RPC: one complete JSON value per line on stdin
    (client -> server) and stdout (server -> client), per the current spec.
    stderr is drained and kept (bounded) for error reporting, never parsed
    as protocol traffic.
    """

    def __init__(self, command: str, args: tuple[str, ...] = (),
                 env: dict[str, str] | None = None) -> None:
        self._command = command
        self._args = list(args)
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._ids = _RequestIdCounter()
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []
        self._closed = False

    async def connect(self, timeout: float = 10.0) -> dict[str, Any]:
        self._process = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await asyncio.wait_for(self._initialize(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise McpTimeoutError("timed out during the MCP handshake") from exc
        return result

    async def _initialize(self) -> dict[str, Any]:
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        await self._notify("notifications/initialized", {})
        return result

    async def list_tools(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        result = await asyncio.wait_for(self._request("tools/list", {}), timeout=timeout)
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 30.0) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._request("tools/call", {"name": name, "arguments": arguments}),
                timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpTimeoutError(f"'{name}' did not respond in time") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpProtocolError("connection closed"))
        self._pending.clear()
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()

    @property
    def alive(self) -> bool:
        return (self._process is not None and self._process.returncode is None
                and not self._closed)

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-20:])

    # -- wire-level plumbing ----------------------------------------------
    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise McpProtocolError("not connected")
        request_id = self._ids.next()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._process.stdin.write(line)
        await self._process.stdin.drain()
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            return
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._process.stdin.write(line)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                self._handle_line(raw)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - a malformed line must not kill the reader
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpProtocolError(f"reader failed: {exc}"))

    def _handle_line(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
            message = _validate_message(obj)
        except (json.JSONDecodeError, McpProtocolError):
            # A malformed line from the server - untrusted output that
            # failed validation. Dropped, not forwarded, not raised into a
            # pending future that may have nothing to do with it.
            return
        message_id = message.get("id")
        if message_id is None:
            return  # a notification from the server - nothing to correlate it to yet
        future = self._pending.get(message_id)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"] or {}
            future.set_exception(McpProtocolError(
                str(error.get("message") or "the server reported an error")))
            return
        future.set_result(message.get("result") or {})

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                raw = await self._process.stderr.readline()
                if not raw:
                    break
                self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
                del self._stderr_tail[:-20]
        except asyncio.CancelledError:
            pass


class HttpMcpClient:
    """One MCP server reached over Streamable HTTP.

    A single endpoint, POSTed JSON-RPC requests, and a response that is
    either a plain JSON-RPC message (``application/json``) or one
    server-sent event carrying it (``text/event-stream``). Server-initiated
    messages on a long-lived SSE stream are not implemented in Phase 1 -
    every call here is a single request/response round trip, which is
    sufficient for tools/list and tools/call against every Streamable HTTP
    server tested so far. See the architecture doc's known-limitations note.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._session_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._ids = _RequestIdCounter()
        self._closed = False

    async def connect(self, timeout: float = 10.0) -> dict[str, Any]:
        self._client = httpx.AsyncClient(timeout=timeout)
        result, session_id = await self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        if session_id:
            self._session_id = session_id
        await self._notify("notifications/initialized", {})
        return result

    async def list_tools(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        result, _ = await self._post("tools/list", {}, timeout=timeout)
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 30.0) -> dict[str, Any]:
        result, _ = await self._post(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.aclose()

    @property
    def alive(self) -> bool:
        return self._client is not None and not self._closed

    # -- wire-level plumbing ----------------------------------------------
    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, method: str, params: dict[str, Any],
                    timeout: float | None = None) -> tuple[dict[str, Any], str | None]:
        if self._client is None:
            raise McpProtocolError("not connected")
        request_id = self._ids.next()
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            response = await self._client.post(
                self._url, json=body, headers=self._request_headers(),
                timeout=timeout or self._client.timeout)
        except httpx.TimeoutException as exc:
            raise McpTimeoutError(f"'{method}' timed out") from exc
        except httpx.HTTPError as exc:
            raise McpProtocolError(f"connection failed: {exc}") from exc
        if response.status_code >= 400:
            raise McpProtocolError(f"server returned HTTP {response.status_code}")
        session_id = response.headers.get("Mcp-Session-Id")
        content_type = response.headers.get("content-type", "")
        message = self._parse_body(response.text, content_type, request_id)
        if "error" in message:
            error = message["error"] or {}
            raise McpProtocolError(str(error.get("message") or "the server reported an error"))
        return message.get("result") or {}, session_id

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._client is None:
            return
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await self._client.post(self._url, json=body, headers=self._request_headers())
        except httpx.HTTPError:
            pass  # a notification's delivery is not awaited for correctness elsewhere

    @staticmethod
    def _parse_body(text: str, content_type: str, expected_id: int) -> dict[str, Any]:
        if "text/event-stream" in content_type:
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[len("data:"):].strip())
                    message = _validate_message(obj)
                except (json.JSONDecodeError, McpProtocolError):
                    continue
                if message.get("id") == expected_id:
                    return message
            raise McpProtocolError("no matching response in the event stream")
        try:
            obj = json.loads(text)
            return _validate_message(obj)
        except json.JSONDecodeError as exc:
            raise McpProtocolError("malformed JSON response") from exc

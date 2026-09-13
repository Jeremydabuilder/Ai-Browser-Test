"""Optional stdio-to-HTTP bridge (Phase 12, Part 11).

Some MCP clients (Claude Desktop's *local* server path is the one that
matters here - see docs/external_ai_clients.md) only know how to launch a
command over stdio; they cannot speak to a Streamable HTTP server
directly. This script is the smallest possible adapter for that case: it
reads one JSON-RPC message per line from stdin, forwards it as an
authenticated HTTP POST to the existing PyBrowser MCP server, and writes
the response back as one line on stdout.

It contains NO browser logic and NO Mission logic - every tool, every
safety check, every capability rule still lives in exactly one place,
app/mcp_server/tools.py, reached through the one existing HTTP server.
This file only adapts transport and attaches the bearer token; deleting
it changes nothing about what PyBrowser can do, only how a stdio-only
client reaches it.

Usage (as referenced from a generated claude_desktop_config.json - see
app/mcp_server/client_configs.py's claude_desktop_bridge_config):

    PYBROWSER_MCP_URL=http://127.0.0.1:8765/mcp \\
    PYBROWSER_MCP_TOKEN=<token> \\
    python -m app.mcp_server.stdio_bridge

The URL/token may also be passed as --url/--token for manual testing;
the environment variables are what a launched-by-another-app config uses,
since they never appear in a process listing the way a CLI argument would.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def forward_line(url: str, token: str, line: str, timeout: float = 30.0) -> str:
    """Forward one JSON-RPC request line to the HTTP server and return its
    response as one line of text. Never raises on a transport failure -
    it returns a JSON-RPC error message instead, so a flaky connection
    surfaces to the calling MCP client as an ordinary tool error rather
    than killing the bridge process."""
    try:
        request_id = json.loads(line).get("id")
    except json.JSONDecodeError:
        request_id = None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(url, data=line.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            return exc.read().decode("utf-8")
        except OSError:
            pass
        return json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "error": {"code": -32000, "message": f"HTTP {exc.code}"}})
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return json.dumps({"jsonrpc": "2.0", "id": request_id,
                          "error": {"code": -32000, "message": str(exc)}})


def run(url: str, token: str, in_stream=sys.stdin, out_stream=sys.stdout) -> None:
    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        out_stream.write(forward_line(url, token, line) + "\n")
        out_stream.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("PYBROWSER_MCP_URL"))
    parser.add_argument("--token", default=os.environ.get("PYBROWSER_MCP_TOKEN"))
    args = parser.parse_args(argv)
    if not args.url or not args.token:
        print("PYBROWSER_MCP_URL and PYBROWSER_MCP_TOKEN (or --url/--token) are required.",
             file=sys.stderr)
        raise SystemExit(2)
    run(args.url, args.token)


if __name__ == "__main__":
    main()

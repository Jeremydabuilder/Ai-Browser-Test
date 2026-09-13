# External AI Clients / MCP Connections

PyBrowser exposes one MCP server (Phase 11): Streamable HTTP, bound to
`127.0.0.1` only, bearer-token authenticated, least-privilege scoped. This
document is the capability matrix and per-client setup notes for Phase 12
("Connect an AI"). It is not a set of separate integrations - every client
below reaches the exact same server, the exact same 13 tools, and the exact
same auth/permission/audit machinery. Only *how a client reaches the
server* differs.

**A note on sourcing.** This environment's network egress policy blocks
direct fetches of `code.visualstudio.com`, `cursor.com`, and
`modelcontextprotocol.io`, so the rows below were built from web search
results that quote and summarize those official docs (linked per row)
rather than a direct fetch of the primary source. MCP client support is a
fast-moving area - re-verify against the linked docs before relying on any
row here for something safety-relevant.

## Capability matrix

| Client | MCP support | Localhost support | Transport | Auth/header support | Remote endpoint needed | Setup method |
|---|---|---|---|---|---|---|
| **ChatGPT** (Developer Mode custom connectors) | Yes | **No** - custom connectors must be HTTPS and publicly reachable | Streamable HTTP or SSE | OAuth, or a static bearer token via the connector's "Token" auth option | **Yes** | Settings → Apps & Connectors → Developer Mode → Create; paste the (tunneled) URL + token |
| **Claude Desktop** - remote connectors | Yes | **No** - "Add custom connector" always performs OAuth against the server's public origin; static bearer headers are an enterprise/admin-only beta feature, not available to an ordinary user pairing their own local server | Streamable HTTP | OAuth by default (no per-user static-token path) | Yes | Settings → Connectors → Add custom connector (not usable for this server - see "Local MCP servers" below) |
| **Claude Desktop** - local servers | Yes | **Yes** - this is the path PyBrowser uses | stdio (Claude Desktop spawns the command itself) | N/A (bridge attaches the bearer token to its own HTTP calls) | No | `claude_desktop_config.json`, launching `app/mcp_server/stdio_bridge.py` (see Part 11 below) |
| **Cursor** | Yes | Yes | Streamable HTTP | `Authorization` header, incl. `Bearer <token>`, directly in `mcp.json` | No | Cursor Settings → MCP → Add new MCP server, or paste generated `mcp.json` |
| **VS Code** (Copilot Chat / Agent Mode) | Yes | Yes | Streamable HTTP (`"type": "http"`) | `headers` with `Authorization: Bearer <token>` | No | Command Palette → "MCP: Open User Configuration", or `.vscode/mcp.json` |
| **Generic MCP client** | Assumed (spec-compliant) | Depends on the client | Streamable HTTP, JSON-RPC 2.0 | `Authorization: Bearer <token>` | Depends on the client | Endpoint + transport + auth scheme shown as copyable text; no config file guessed |

## Why each row is what it is

- **ChatGPT requires a tunnel.** Developer Mode's custom connectors are
  documented as connecting from OpenAI's own infrastructure to a
  server's public HTTPS URL - "no local servers on consumer ChatGPT
  without a tunnel". PyBrowser does not create, manage, or bundle a
  tunnel; Part 12's UI only explains this limitation and lets the user
  supply their own if they choose to accept that trade-off.
- **Claude Desktop's remote-connector path doesn't fit either** - it
  performs OAuth dynamic client registration against the server's origin
  by default, and static bearer/API-key headers are gated behind an
  enterprise-admin beta, not something an individual pairs on their own.
  Claude Desktop's *separate* "Local MCP servers" feature, however, is a
  perfect fit: it launches a command over stdio, same as it always has.
  PyBrowser's answer is the smallest possible adapter - see Part 11 below
  and `app/mcp_server/stdio_bridge.py`.
- **Cursor and VS Code both added first-class remote Streamable HTTP
  support with header-based auth** - no bridge, no tunnel, a direct
  `mcpServers`/`servers` entry works. The single most common mistake
  moving a config between the two is the root key: Cursor (and Claude)
  use `"mcpServers"`; VS Code uses `"servers"`.

## Tools exposed

Identical across every client - see `app/mcp_server/tools.py`:
`browser.current_page`, `browser.read_page`, `browser.list_tabs`,
`browser.search`, `browser.get_selected_text`, `browser.open_tab`,
`browser.navigate`, `mission.list`, `mission.get`, `mission.get_findings`,
`mission.get_sources`, `mission.get_plan`, `mission.create`. No client gets
a different tool set; capability scoping (see below) decides what a given
paired client may call, not which client it is.

## Authentication model

One pairing/token/revocation system for every client (Phase 11's
`app/mcp_server/auth.py` and `app/storage/mcp_server_store.py`), extended in
Phase 12 with purely descriptive metadata: `client_type` (which "Connect an
AI" card paired it) and `connection_method` (`direct_http` / `stdio_bridge`
/ `remote_tunnel`). Neither field changes what auth or permission code runs
- they only drive which text/config a card shows.

## Permission presets

See `app/mcp_server/permissions.py`. Offered in the pairing UI, least
privilege first:

| Preset | Capabilities |
|---|---|
| Read Only (default) | Read pages, Read tabs, Read Mission data |
| Research | + Open tab, Navigate |
| Mission Assistant | + Create Mission |
| Full Access (advanced) | Every capability - never a default, always an explicit opt-in |

## Connection verification

One shared engine (`app/mcp_server/verification.py`), used identically for
every client type: reachable → authenticated → `initialize` succeeds →
`tools/list` succeeds → a safe tool (`browser.list_tabs`) executes. A
missing optional capability on an otherwise-working connection is reported
as Verified with a note, not as a failure - "token created" is never
conflated with "connected", and the reverse (an unrelated tool error) does
not retroactively make a working connection look broken.

Status vocabulary: Not configured, Config generated, Requires tunnel,
Verified, Authentication failed, Unreachable, plus Revoked (from the
client's own revoked flag, checked independently of verification history).

## Known limitations

- ChatGPT and Claude Desktop's remote-connector path cannot be verified
  end-to-end from inside this environment (no live ChatGPT/Claude account
  is available here, and the ecosystem docs were read via search-result
  summaries rather than a direct fetch of the primary source - see the
  sourcing note above). Their rows describe the currently-documented
  requirements, not a confirmed live handshake.
- PyBrowser does not provide, manage, or recommend a specific tunnel
  product for ChatGPT. If the user wants ChatGPT access, they must set up
  and trust their own HTTPS tunnel; PyBrowser's UI names the requirement
  and stops there.
- The stdio bridge (`app/mcp_server/stdio_bridge.py`) is a single-request
  round trip per line, matching how the existing (Phase 11) client-side
  `StdioMcpClient` frames stdio traffic - it holds no MCP session state of
  its own beyond that.

## Sources consulted

- [ChatGPT MCP: Setup, Plans, and Limits (2026)](https://coworker.ai/blog/chatgpt-mcp)
- [MCP in 2026: which AI agents support custom connectors](https://truthifi.com/education/state-of-mcp-2026-ai-agents-custom-connectors)
- [Developer mode and MCP apps in ChatGPT | OpenAI Help Center](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
- [MCP and Connectors | OpenAI API](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [Get started with custom connectors using remote MCP | Claude Help Center](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
- [Getting Started with Local MCP Servers on Claude Desktop | Claude Help Center](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
- [Cannot configure Authorization: Bearer for custom remote MCP · Issue #112 · anthropics/claude-ai-mcp](https://github.com/anthropics/claude-ai-mcp/issues/112)
- [Authentication for connectors - Claude.ai Documentation](https://claude.com/docs/connectors/building/authentication)
- [Model Context Protocol (MCP) | Cursor Docs](https://cursor.com/docs/mcp)
- [MCP Authentication in Cursor: OAuth, API Keys, and Secure Configuration](https://www.truefoundry.com/blog/mcp-authentication-in-cursor-oauth-api-keys-and-secure-configuration)
- [MCP configuration reference (VS Code)](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
- [Add and manage MCP servers in VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers)

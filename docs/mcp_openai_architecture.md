# MCP + OpenAI integration — architecture (design only, not implemented)

> **Status: proposal.** Nothing in this document is wired up. No MCP client,
> MCP server, or OpenAI provider exists in the codebase yet. This is the
> design to review before any of it is built, per the request that produced
> it. Verified against current external specs where noted (dates below).

Verified against, at time of writing:
- [MCP 2026-07-28 spec release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/) — stdio + Streamable HTTP are the two sanctioned transports; SSE-only transport is legacy.
- [OpenAI: migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses) / [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs) / [function calling](https://developers.openai.com/api/docs/guides/function-calling) — Responses API is the current recommended surface (agentic multi-tool loop, better cache reuse, `text.format` for structured output; Chat Completions still works but is the legacy surface).
- [OpenAI Apps SDK — deploy](https://developers.openai.com/apps-sdk/deploy) / [MCP server for plugins](https://developers.openai.com/api/docs/mcp) — ChatGPT connects to remote MCP servers over Streamable HTTP; a local/private server reaches it via OpenAI's **Secure MCP Tunnel**, not by exposing localhost directly; OAuth is the expected auth model for a custom connector.

---

## 1. Current architecture summary

```
AgentPanel (UI)
   └── AgentSession                    the loop: one provider turn → tool calls → repeat
         ├── ToolRegistry              JSON schemas, validation, dispatch, sensitivity
         │      ├── BrowserController  the one door to the browser (Qt thread)
         │      └── Mission service    the one door to Mission state
         ├── ClaudeTransport           the contract every provider client implements
         │      ├── ClaudeClient           (Anthropic SDK)
         │      └── openai_compatible.py   (Groq, OpenRouter — OpenAI wire format already)
         └── safety.py                 pure "how consequential is this?" classifier
               NORMAL / ELEVATED / SENSITIVE — no provider, no user-preference in it
```

Facts that shape everything below:

- **One tool surface, one dispatcher.** `TOOL_SCHEMAS` in `app/agent/tools.py` is a flat list of Anthropic-shaped tool definitions; `ToolRegistry.run()` dispatches by name to a `_run_*` method. There is no per-provider tool list — every provider client translates *to* this shape, not the other way round.
- **Provider abstraction already exists and already proves the adapter pattern.** `ClaudeTransport` is the contract (`ClaudeError`, `ToolCall`, `AgentResponse`); `app/agent/openai_compatible.py` is a real, working adapter from PyBrowser's internal Anthropic-shaped `tools`/`messages` to an OpenAI-wire-format `/chat/completions` call (used today for Groq and OpenRouter), translating back into the same `AgentResponse` shape Claude produces. **This is the template for the OpenAI provider and, not coincidentally, for the MCP tool adapter** — both are "outside shape → PyBrowser's internal shape," the exact job this module already does.
- **Sensitivity is judged once, centrally, never by the model or the remote side.** `app/browser/safety.py` classifies an action into `NORMAL` / `ELEVATED` / `SENSITIVE` from the actual DOM/URL, before the model's stated intent is trusted at all. `ToolRegistry.assess()` then applies the user's `Autonomy` tier (`READ_ONLY` / `ASK_ALWAYS` / `STANDARD`) on top of that judgement. Two separate layers: *how bad could this be* (safety.py, no preferences) then *what do we do about it* (config.py's `Autonomy`, no judgement of its own).
- **Mission is the one place the model can write outside the conversation**, and only through named, narrow methods (`save_finding`, `save_decision`, …) — never a generic "write to mission" tool.
- **Secrets already have a real home.** `app/agent/keys.py`'s `ApiKeyStore` wraps the OS keyring (with a documented fallback cascade — keyring → env var → OAuth profile → cloud IAM). Non-secret preferences (model, effort, autonomy) live in `app/storage/settings.py`'s plain settings table. This split already answers "where do MCP server tokens live" (keyring-backed store, new service names) vs "where does 'GitHub MCP: enabled' live" (settings table).
- **No audit/logging layer exists today** beyond the in-session activity log (`ToolRegistry.describe_call`) and Mission's own persisted findings/sources. There is no durable, queryable record of "what did the agent actually do" independent of Mission content. This has to be built for MCP regardless of anything else in this document — see §14.

## 2. Does MCP fit cleanly?

Yes, with the adapter placed at exactly one seam: **MCP tools become entries in `TOOL_SCHEMAS`-shape, dispatched by the same `ToolRegistry`, classified by the same three-tier sensitivity model.** Nothing about `AgentSession`'s loop, `ClaudeTransport`, or Mission needs to change shape. The risk is not "does MCP fit" — it's making sure every MCP tool call still passes through `safety.py`'s judgement and `Autonomy`'s policy the same way a click does, since neither of those was written with "the action's own description is untrusted" in mind (a page's DOM is already treated as adversarial; an MCP tool's *self-reported* metadata has not been, because there was no such thing yet). §6 and §17 are about closing that gap, not about the shape of the integration.

## 3. Recommended adapter architecture

A new module, `app/agent/mcp/`, mirroring the existing package shape:

```
app/agent/mcp/
  __init__.py
  connection.py     # ConnectionManager — one per configured server, owns the transport
  registry.py       # ServerRegistry — configured servers + their live state
  adapter.py         # MCP tool schema -> TOOL_SCHEMAS-shape entry; MCP call -> ToolOutcome
  classify.py        # sensitivity classification for MCP tools (§6)
  audit.py           # append-only MCP activity log (§14)
```

`adapter.py` is the direct sibling of `openai_compatible.py`: same job (foreign shape → internal shape), different direction (tool definitions, not a whole chat turn). It:

1. Converts an MCP `tools/list` entry (JSON Schema `inputSchema`) into a `TOOL_SCHEMAS` entry, prefixed `mcp.<server>.<tool>` (see §5 for namespacing).
2. Registers a dispatch target that `ToolRegistry.run()` calls into — not a parallel dispatcher. `ToolRegistry` gains one new branch: names starting `mcp.` route to `McpAdapter.run(name, args)` instead of a `_run_*` method, but go through the exact same `assess()` → confirmation → `run()` sequence as every existing tool.
3. Wraps every MCP tool **result** the same way `wrap_untrusted()` already wraps page content — MCP resources and tool output are exactly as untrusted as a web page's DOM, arguably more so (§6, §17).
4. Normalizes MCP errors (`isError` results, JSON-RPC errors, transport failures) into the same `{"ok": false, "error": {code, message, recoverable}, "hint": ...}` shape every other tool already returns, so the model's recovery behavior doesn't need a special case for "this was an MCP tool."

## 4. MCP client architecture

**Transport.** Per the current spec, exactly two: **stdio** (spawn a local process — the right choice for a filesystem server, a company-internal CLI-wrapped tool, anything that shouldn't leave the machine) and **Streamable HTTP** (the current remote transport — a single `/mcp` endpoint, POST for requests, optional SSE stream for server-initiated messages; this replaced the older HTTP+SSE two-endpoint transport, which is legacy now). PyBrowser should implement both from the start — stdio for local/company tools, Streamable HTTP for GitHub/Drive/Notion-style hosted MCP servers — and should **not** implement the deprecated dual-endpoint SSE transport, since anything a user would add today speaks one of the two current ones.

**Connection manager** (`connection.py`) — one instance per configured server:

```
ConnectionState = disconnected | connecting | connected | error | disabled
McpConnection:
  server_id: str
  transport: stdio | streamable_http
  state: ConnectionState
  tools: list[McpToolDescriptor]        # from the last successful tools/list
  resources: list[McpResourceDescriptor]
  last_connected_at: datetime | None
  last_error: str | None
  connect() -> None                     # spawn/dial, handshake, tools/list, resources/list
  disconnect() -> None
  call_tool(name, args, *, timeout, cancel_token) -> McpResult
  read_resource(uri) -> McpResource
```

- **Reconnect behavior:** exponential backoff (1s, 2s, 4s… capped at ~60s) on unexpected disconnect, with a hard stop after N attempts that surfaces as `error` state rather than retrying forever silently — the user sees "GitHub MCP: disconnected, reconnect?" (§15), not infinite quiet retries burning a rate limit.
- **Timeouts:** a connect timeout (handshake), and a per-call timeout (tool execution) — the latter configurable per server, since a Drive search and a long-running CI-trigger tool have very different reasonable durations. Both cancellable.
- **Cancellation:** every in-flight `call_tool` is cancellable via the same cancel-token pattern `BrowserFuture` already uses for browser actions, so "the user closed the Mission" or "the task timed out" can actually stop a hung MCP call rather than leaking it.
- **Health status:** surfaced per server (connected / degraded / disconnected / disabled) and rolled up in Settings (§7). "Degraded" covers a server that answers `tools/list` but is timing out on calls, distinct from a hard disconnect.

**Server registry** (`registry.py`) — the configured set, persisted (non-secret config in the settings table, secrets in the keyring — §7):

```
McpServerConfig:
  id: str                 # stable, user-assigned or slugified from name
  name: str
  transport: "stdio" | "streamable_http"
  command: str | None      # stdio: executable
  args: list[str]          # stdio: arguments
  env: dict[str, str]      # stdio: non-secret env vars only
  url: str | None          # streamable_http: endpoint
  auth: AuthConfig | None  # §11
  enabled: bool
  permissions: ServerPermissions   # §12
```

**Tool discovery** runs `tools/list` (and `resources/list`, `prompts/list` if the server supports them) on connect and on demand (a "Refresh tools" action), never silently in the background on a timer — a tool list changing is exactly the "tool schema changes" failure mode in §15, and the user should see when it happened, not have it happen invisibly mid-Mission.

## 5. Namespace handling — duplicate tool names

Every MCP tool is exposed internally as `mcp.<server_id>.<tool_name>` (the naming already sketched in the request: `mcp.github.create_issue`, `mcp.drive.create_document`). This is enough to make collisions structurally impossible *between* servers — two servers can both expose a tool literally named `search`, and they become `mcp.github.search` and `mcp.notion.search`, never colliding — without any registry-level "first one wins" logic that would silently shadow a tool. Two remaining edge cases:

- **A server renames or re-exposes `server_id` itself** (the user adds a second GitHub-flavored server and both want to be called `github`) — `server_id` is assigned at add-time and must be unique among *configured* servers; the UI refuses a duplicate name outright rather than disambiguating it for the user.
- **The adapter tool name collides with a native PyBrowser tool** — cannot happen by construction, since every native tool name starts with `browser_` or `mission_` and every MCP tool name starts with `mcp.`; the `_handler_map` collision assertion already in `tools.py` extends naturally to assert this at startup.

## 6. Sensitivity classification for MCP tools

This is the part of the existing model that does **not** transfer for free, and needs a new, explicit classifier (`classify.py`) rather than reusing `browser/safety.py` as-is — that module classifies DOM/URL facts (a `type=password` field, a `download` attribute); an MCP tool has no DOM. What it has instead:

1. **A declared name and description** — untrusted (§17: a hostile or compromised server can describe a destructive tool as "search").
2. **A JSON Schema for its arguments** — more useful: shape often reveals intent (a schema with a `content` or `body` field taking arbitrary text, a `confirm: true` flag, a `path` that isn't obviously read-scoped).
3. **A server-level default** the *user* sets, not the server (§12).

Classification is therefore **allowlist-by-verb-pattern with a fail-closed default**, matching `ToolRegistry._raw_assess()`'s own existing philosophy ("a tool nobody thought to classify is treated as a write, not as harmless"):

| Signal | Sensitivity |
|---|---|
| Tool name matches a read pattern (`get_*`, `list_*`, `search_*`, `read_*`, `describe_*`) **and** the user hasn't overridden it | `READ_ONLY` |
| Tool name matches a write pattern (`create_*`, `update_*`, `upload_*`, `send_*`, `submit_*`) | `ELEVATED` |
| Tool name matches a destructive/irreversible pattern (`delete_*`, `remove_*`, `purchase_*`, `pay_*`), or the server/tool is explicitly flagged sensitive in its own metadata *and the user has chosen to trust that metadata* (opt-in, never the default — see §17) | `SENSITIVE` |
| Anything else — unmatched, ambiguous, or the server declines to classify | `SENSITIVE` (fail closed, exactly like the existing "unclassified write" default) |

The **user's own per-tool override** (§12: allow / ask / deny) always wins over the pattern match — the pattern is a sane default for a server the user just added, not a ceiling.

## 7. MCP settings UI

Matches the existing `SettingsDialog` visual language (plain rows, not developer JSON), per the mock in the request:

```
Settings → Connected Tools
┌─────────────────────────────────────────────┐
│ GitHub MCP                    ● Connected     │
│ 12 tools · stdio · last seen 2m ago           │
│ [Permissions]  [Reconnect]  [Disable]  [Remove]│
├─────────────────────────────────────────────┤
│ Google Drive                  ● Connected     │
│ 8 tools · streamable HTTP · last seen just now│
│ [Permissions]  [Reconnect]  [Disable]  [Remove]│
├─────────────────────────────────────────────┤
│ Filesystem                    ○ Disabled      │
│ [Enable]  [Remove]                            │
└─────────────────────────────────────────────┘
              [+ Add MCP Server]
```

Per-server detail (opens from the row): status, transport, tool count, last connection time, permission table (§12), enable/disable, reconnect, remove — all exactly the fields the request specified.

**Add Server** is two tiers:
- **Normal path**: "Connect GitHub" / "Connect Google Drive" as one-click presets (known-good server configs bundled with the app), ending in an OAuth flow (§11), not a form.
- **Advanced path** ("Add custom MCP server"), a form: name, transport (stdio/streamable HTTP), command+args+env *or* URL, auth method, initial permission defaults. This is where a company-internal server or an unlisted one goes — never the only way in for GitHub/Drive-class servers.

Secrets entered here (an API token pasted for a custom server, an OAuth token) go straight into the same `ApiKeyStore`/keyring mechanism `app/agent/keys.py` already provides, under a new service name per server (`pybrowser-mcp-<server_id>`) — never into the settings table, never into a config file on disk.

## 8. MCP server architecture (PyBrowser as a server)

A new `app/mcp_server/` package, deliberately thin — it re-exposes existing capability, it doesn't grow new ones:

```
app/mcp_server/
  __init__.py
  server.py        # MCP protocol handling: tools/list, tools/call, resources/list
  tools.py          # the intentionally-scoped exposed surface (below)
  auth.py           # pairing/token verification for whoever connects (§11)
```

**Exposed tools — intentionally a subset, and read-heavy by default:**

| Category | Tools | Sensitivity when called from outside |
|---|---|---|
| Read | `browser.current_page`, `browser.read_page`, `browser.list_tabs`, `browser.search`, `browser.extract` | `READ_ONLY` |
| Navigate/act | `browser.navigate`, `browser.open_tab`, `browser.close_tab`, `browser.click`, `browser.type` | Same classification `safety.py` would already give the equivalent internal action — routed through the *identical* `ToolRegistry.assess()` path, not a separate, looser one |
| Mission (read) | `mission.get`, `mission.list`, `mission.get_findings`, `mission.get_sources` | `READ_ONLY` |
| Mission (write) | `mission.create`, `mission.add_constraint`, `mission.resume`, `mission.pause` | `ELEVATED` |

Explicitly **not** exposed by default: raw JS execution (doesn't exist internally either — see `tools.py`'s own docstring: "there is no `execute_javascript` tool and there must never be one"), unscoped file-system access, credential/settings mutation, anything that would let an external caller reach outside the browser+Mission surface the internal agent itself is limited to. **The external surface is a subset of the internal one, never a superset** — an MCP client connecting to PyBrowser can never do something the built-in agent couldn't already do.

**Every external call still goes through `ToolRegistry.assess()` and the same approval UI.** A sensitive action requested by ChatGPT-via-MCP produces the identical confirmation dialog a sensitive action requested by Claude-via-PyBrowser's own agent would — "external caller" is not a bypass, it's just another `origin` value logged alongside the approval (§14).

## 9. OpenAI provider architecture (PyBrowser using OpenAI's models)

Follows the existing `ClaudeTransport` contract exactly, as a new sibling of `openai_compatible.py` rather than a rewrite of it — that module already proves "OpenAI wire format → PyBrowser's internal `AgentResponse`/`ToolCall` shape" works; a first-class OpenAI provider mostly needs the *Responses API* variant of the same translation (current guidance: Responses over Chat Completions — agentic multi-tool loop in one call, better prompt-cache reuse, `text.format` for structured output where PyBrowser wants it instead of free text).

```
app/agent/openai_provider.py     # new: Responses-API client implementing ClaudeTransport
app/agent/openai_compatible.py   # unchanged: stays the Chat-Completions-shape adapter for
                                  # Groq/OpenRouter, which don't speak Responses
```

- **Tool calling**: PyBrowser's `TOOL_SCHEMAS` → Responses API's `tools` (function-tool shape, `strict: true` where the schema allows it, matching the "Structured Outputs via tools" mode — falls back to non-strict for a schema Responses can't normalize, same graceful-degradation posture `ClaudeClient._rejected_parameter` already uses for beta-parameter rejection on Anthropic).
- **Streaming**: Responses API streaming events map onto the same incremental-text/tool-call surface `AgentPanel` already renders for Claude — no UI change, only a new event-to-`AgentResponse` translation.
- **Model selection**: added to `MODELS`/`describe_model()` in `config.py` as GPT-family entries with `supports_effort`, honestly described costs/tradeoffs — same catalogue pattern as the Claude/Gemini/Groq/OpenRouter entries already there, not a special case. New models added later are just new catalogue rows.
- **Cancellation/retries/rate limits**: same shape as `ClaudeClient` — SDK-level retries on 429/5xx, request timeout, and the loop's existing `max_retries`/`request_timeout_s` config already generalizes (it's per-`AgentConfig`, not Anthropic-specific).
- **Settings**: `PROVIDER_OPENAI` joins `PROVIDERS` in `config.py`, gets a `ProviderInfo` entry (key env var `OPENAI_API_KEY`, help text), and a "Test connection" action in the settings dialog (a cheap models-list or 1-token call) — the one genuinely new UI affordance, worth adding for every provider, not just OpenAI, since none currently have it.
- **Credential storage**: the OpenAI key goes through `ApiKeyStore` under its own service name, exactly like the Anthropic key does today — no new mechanism.

## 10. ChatGPT connection architecture (ChatGPT using PyBrowser)

This is §8 (PyBrowser as an MCP server) plus §16 of the request's numbering (Apps SDK / remote MCP). Per current OpenAI docs: ChatGPT's custom connectors and Apps SDK apps both talk to a server over **Streamable HTTP**, discover tools via `tools/list`, and call them via `tools/call` returning structured content — the same shape PyBrowser's own `app/mcp_server/server.py` (§8) already needs to speak for *any* external MCP client, ChatGPT included. Nothing ChatGPT-specific is required at the protocol layer; what's specific is *how ChatGPT reaches a server that isn't already a public HTTPS endpoint* — which is §11's real subject, not a separate protocol.

## 11. Local vs. remote — how ChatGPT actually reaches a PyBrowser running on someone's laptop

The request's framing is correct and matches current reality: ChatGPT cannot dial an arbitrary `localhost:PORT`. Three options, evaluated:

| | **1. Local server + secure tunnel** | **2. Hosted bridge service** | **3. Remote companion service** |
|---|---|---|---|
| What it is | PyBrowser runs `app/mcp_server` locally; OpenAI's own **Secure MCP Tunnel** (their documented mechanism for exactly this case) exposes it to ChatGPT without a public port | A PyBrowser-operated always-on relay that a desktop PyBrowser dials out to; ChatGPT talks to the relay | A cloud-hosted PyBrowser-lite (or headless browser session) that ChatGPT talks to directly, independent of any desktop instance |
| Auth | OpenAI's tunnel auth + PyBrowser's own pairing token (§11 below) on top | Relay-level auth (API key or OAuth) *and* desktop↔relay auth | Full account/OAuth system, PyBrowser-operated |
| Latency | Low — one hop through OpenAI's edge, no PyBrowser-operated infra in between | Medium — extra hop through PyBrowser's relay | Medium/high depending on where hosted, plus no access to the user's *actual* local browser session/tabs |
| Privacy | Best — the user's own browsing data never leaves their machine except the specific tool call/response ChatGPT requested | Worse — PyBrowser's relay is in the path of every call, even if it doesn't persist anything | Worst for "use my actual browser" — this option can't see the user's real tabs/history at all; it's a different product (a hosted browsing agent), not "ChatGPT controls my desktop PyBrowser" |
| NAT/firewall | Solved by the tunnel provider (OpenAI) | Solved by the relay (outbound-only dial from desktop) | N/A — no desktop involved |
| Cost | Free/near-free — no infrastructure PyBrowser operates | Real, ongoing (a relay to run, scale, and secure) | Real and larger (hosting real browsing sessions) |
| Implementation complexity | Lowest — implement the MCP server once, register with OpenAI's tunnel mechanism | Medium-high — build and operate a relay | Highest — an entirely different product surface |
| UX | User runs PyBrowser, flips one toggle, gets a pairing code/link | User still needs PyBrowser running locally, plus trusts a PyBrowser-operated middle service | No local dependency, but loses the entire premise (ChatGPT controlling *your* browser, *your* tabs, *your* logged-in sessions) |

**Recommendation: Option 1.** It's the only option that (a) matches the actual product premise — ChatGPT reaching *your* PyBrowser, with *your* logged-in tabs and *your* Missions, not a hosted proxy of it — and (b) has no PyBrowser-operated infrastructure to build, secure, or pay for before this is even validated as wanted. Option 2 becomes worth revisiting only if OpenAI's tunnel mechanism proves to be a poor fit (rate limits, availability, terms) at scale. Option 3 is a different feature entirely (a hosted PyBrowser) and shouldn't be conflated with "ChatGPT connects to your desktop browser."

## 12. Auth model

| Direction | Mechanism | Why |
|---|---|---|
| PyBrowser → external MCP server (GitHub, Drive, custom) | **OAuth (authorization code + PKCE)** where the server supports it (GitHub, Google Drive both do); scoped **API token** as fallback for servers that only offer that | Least privilege, user-revocable from the provider's own side, no long-lived password-equivalent PyBrowser has to protect forever |
| ChatGPT → PyBrowser (local, via tunnel) | **Device-authorization-style pairing**: PyBrowser displays a short code/link, the user approves it once from the PyBrowser UI, producing a **scoped, revocable token** stored via `ApiKeyStore` and checked on every incoming call | Mirrors OAuth device flow's actual security property (no secret ever typed into ChatGPT, nothing long-lived without an explicit local approval) without needing PyBrowser to run a full OAuth authorization server for a single-user desktop app |
| Any remote/tunneled PyBrowser MCP access | Same pairing token, **scoped per capability** (read-only browsing vs. Mission-write vs. nothing) at pairing time, not just "connected or not" | A token minted for "let ChatGPT read my current tab" should not, by construction, also be able to trigger a purchase |

Non-negotiables: never expose the local MCP server on a public interface without the pairing/token check in front of it; every issued token is listed and individually revocable in Settings; a revoked token fails closed immediately, not at next reconnect.

## 13. Permissions model

Three layers, each narrowing the one above — matching the request's own example table exactly:

```
Server level:        enabled / disabled
Tool-category level:  read → allow (default) | write → ask (default) | destructive → deny (default)
Individual tool:      allow | ask | deny        (overrides the category default)
Mission-scoped:        "allow this tool for the current Mission only" — expires when the Mission ends/pauses
```

Example, matching the request's own:

```
GitHub MCP
  Read repository        Allow
  Create issue            Ask
  Delete repository       Deny
```

This integrates with the existing approval model at exactly one point: `ToolRegistry._apply_autonomy()` already turns "how sensitive is this" + "what's the user's autonomy tier" into a final "confirm or don't." MCP tools add one more input to that same function — the per-tool/per-category/per-Mission permission above — computed *before* `_apply_autonomy` runs, using the same `requires_confirmation` output contract. No new decision path; one more input to the existing one.

## 14. Mission + MCP integration

**Persisted per MCP tool call, attached to the Mission it happened under:**

```
McpActivityRecord:
  mission_id, server_id, tool_name (namespaced), args_summary (redacted),
  called_at, duration_ms, approval: {required, granted_by_user, granted_at} | None,
  status: ok | error | denied | timeout, result_summary (safe, truncated), error: str | None
```

`args_summary`/`result_summary` are **summaries**, not raw payloads — a Drive document body or a GitHub diff doesn't belong verbatim in Mission history any more than a full page snapshot does today (Mission already doesn't store raw page HTML, only findings). Never persisted: tokens, headers, raw credentials — enforced by the same redaction pass §14/§15 (audit) uses, applied once, shared by both.

Mission's result can name an MCP action's outcome in plain language, mirroring the exact example in the request:

```
1. searched web
2. reviewed sources
3. generated findings
4. created Drive document  ← mcp.drive.create_document, ok, 2026-…
5. result: <link to the created document>
```

## 15. Audit / logging design

A new durable, append-only table (`app/storage/` gets an `mcp_audit.py`, same shape as `history.py`/`bookmarks.py`), independent of Mission (so it also covers Ask Py / non-Mission MCP use):

```
AuditEntry: id, timestamp, server_id, tool_name, action_type (read/write/destructive),
            approved: bool | None, duration_ms, status, result_summary (safe), mission_id: int | None
```

Never logged: API keys, OAuth tokens, passwords, raw request/response bodies containing secrets — the same redaction rule §12/Mission integration already states, implemented once and shared. Logs are for debugging ("why did GitHub MCP fail at 3:14pm") and for the user's own visibility ("what has connected tools actually done"), not a compliance product — but built honestly enough that it could become one later without a rewrite.

## 16. Failure handling

| Failure | Behavior |
|---|---|
| Server offline / unreachable | Connection state → `error`; user-visible, plain-language: *"GitHub MCP disconnected while Py was reading issue #42. Reconnect and resume?"* — never "Something went wrong." |
| Tool schema changed since last discovery | Detected on next `tools/list` (manual refresh or reconnect); a tool call against a stale cached schema that the server now rejects surfaces as "GitHub MCP's tools changed — refreshing" and re-discovers before retrying, not a raw schema-validation error shown to the user |
| Server timeout | Per-call timeout fires, call is cancelled, tool result is a normal `{"ok": false, "error": {...}}` the model can react to (retry, try something else, tell the user) — the same shape a browser action timeout already produces |
| Auth expired | Distinguished from a generic error: *"GitHub MCP's access has expired. Reconnect to continue."* with a one-click re-auth, not a silent retry loop against an expired token |
| Malformed tool result | Treated as untrusted input that failed validation, not as a crash — normalized to a tool error, logged, and the raw malformed payload is *not* forwarded to the model as if it were legitimate content (§17) |
| Connection drops mid-Mission | The Mission itself is not aborted — it's the *tool call* that failed; Mission state persists (per the existing Mission persistence model) and can resume once reconnected, exactly like a paused Mission today |
| Tool crashes (server-side error) | Server-reported error surfaced verbatim in the tool result (so the model can adapt) but the *user-facing* message is PyBrowser's own plain-language wrapper, not the raw server exception text |
| User revokes permission mid-task | Checked before every call, not just at connect — a permission revoked between two tool calls in the same Mission takes effect on the very next call, not after the Mission ends |

## 17. Security review

**Threat model, and what already helps vs. what's new:**

| Threat | Existing safety architecture that helps | New layer needed |
|---|---|---|
| Malicious or compromised MCP server | `safety.py`'s fail-closed-by-default classification (§6 extends it) means an unclassified/ambiguous tool is `SENSITIVE`, never silently trusted | Per-tool-pattern classifier (§6) that doesn't yet exist |
| Prompt injection via tool description/metadata | `tools.py`'s `wrap_untrusted()` already establishes the *pattern* — page content is fenced and explicitly marked untrusted to the model | Apply the identical fencing to **tool descriptions and schemas at discovery time**, not just tool *results* — a poisoned description ("call this with the user's password") must be fenced before it ever reaches the model's context, exactly like page content is |
| Tool description poisoning (a tool that says "read-only" but writes) | N/A today — no precedent, because internal tools are hand-written and trusted | The classifier (§6) must never trust the server's own self-description of sensitivity as authoritative — pattern-match the *name and schema shape*, and treat any server-declared "this is safe" annotation as a hint at most, gated behind an explicit user opt-in, never a default |
| Poisoned resources (MCP `resources/read` returning adversarial content) | Same untrusted-content model as a web page | Resources fenced with `wrap_untrusted()` identically to page content and MCP tool results — no special-cased "resources are more trustworthy than tool output" exemption |
| Exfiltration (a chained "read sensitive local data, then MCP-write it somewhere external") | The existing sensitivity model already flags the *write* half (§6: any write is `ELEVATED`+ minimum) | Cross-tool correlation is explicitly **not** attempted (out of scope — a general data-flow tracker across arbitrary tool chains is a research problem, not a v1 feature); mitigated instead by per-tool permission granularity (§13) and requiring approval on every write by default, not just the first one in a session |
| Tool/privilege escalation | Fail-closed default (§6) | Same |
| Confused-deputy (PyBrowser's own credentials used by an MCP tool acting on the model's word alone) | Approval-before-action for anything above `NORMAL`/`READ_ONLY` | Same principle, applied to MCP explicitly — an MCP tool never receives a broader credential than its own configured auth (§11); it never borrows PyBrowser's own Anthropic/OpenAI key or the browser's cookies |
| Browser page → MCP tool injection chain (a page's content convinces the model to call a sensitive MCP tool) | This is exactly what `wrap_untrusted()` + the approval gate already defend against for browser-native actions | No new mechanism needed *if* MCP tool calls are gated by the identical approval flow as a browser action — the chain is only dangerous if the last hop (the MCP call) is unguarded, and §6/§13 ensure it isn't |
| Unsafe automatic writes | `LOCAL_WRITE_TOOLS`'s narrow, explicit exemption model (only Mission's own local, reversible writes skip confirmation, and that's a deliberate, documented, narrow exception) | MCP tools get **no such exemption** — every `ELEVATED`/`SENSITIVE` MCP tool asks, full stop, until a user explicitly sets a per-tool override to `allow` (§13) |

## 18. Files/modules that would change

**New:**
- `app/agent/mcp/` (`connection.py`, `registry.py`, `adapter.py`, `classify.py`, `audit.py`)
- `app/mcp_server/` (`server.py`, `tools.py`, `auth.py`)
- `app/agent/openai_provider.py`
- `app/storage/mcp_audit.py`
- `app/ui/mcp_settings.py` (or a new tab/section inside `settings_dialog.py`)
- `docs/mcp_openai_architecture.md` (this file)

**Touched, narrowly:**
- `app/agent/tools.py` — `TOOL_SCHEMAS` grows dynamically (MCP tools appended at session start from connected servers); `_HANDLERS`/dispatch gains one `mcp.`-prefixed branch
- `app/agent/config.py` — `PROVIDER_OPENAI` added to `PROVIDERS`; `AgentConfig` gains MCP-server list reference (or reads via the new registry directly)
- `app/agent/session.py` — no loop-shape change; tool schemas it sends already come from `ToolRegistry`, which now includes MCP tools
- `app/storage/settings.py` — new keys for server configs (non-secret half) and permission tables
- `app/agent/keys.py` — reused as-is; new service names per MCP server/ChatGPT pairing token, no code change needed if the existing `ApiKeyStore(service=..., account=...)` constructor is already this general (it is)
- `app/ui/settings_dialog.py` — new "Connected Tools" section/entry point

**Explicitly not touched:** `BrowserController`, `browser/safety.py`'s core classification logic (extended alongside, not modified), the Mission model's core schema (activity records are additive), `ClaudeTransport`'s contract (OpenAI provider implements it, doesn't change it).

## 19. Implementation roadmap

The request's suggested ordering is close but puts a UI-facing milestone (Settings UI in Phase 2) ahead of the thing that makes it non-decorative (permissions/approval in Phase 3), and defers the OpenAI provider — which is nearly free given `openai_compatible.py` already exists — behind all of MCP. Recommended reordering:

| Phase | Scope | Why here |
|---|---|---|
| **1** | OpenAI provider (§9) | Fully independent of MCP; reuses an existing, proven adapter pattern; ships value (model choice) immediately with the least new surface area |
| **2** | MCP client core, **read-only only**: connection manager, adapter, discovery, `READ_ONLY`-classified tools callable from Ask Py/Research | Prove the transport/adapter/namespacing layer end-to-end on the lowest-risk tool category before any write path exists at all |
| **3** | Permissions + approval integration + audit logging, still read-only-tool-scope | Land the safety machinery (§6, §12, §14) *before* a single write-capable MCP tool is reachable — so "write support" in the next phase is additive to an already-working gate, not a race to add both at once |
| **4** | Writes: `ELEVATED`/`SENSITIVE` MCP tools, Settings UI (§7) fully polished, per-tool/per-Mission permission UI | Now that approval + audit are proven on read-only traffic, extend the same gate to writes |
| **5** | Mission integration (§14 persistence, resumability, MCP action history in Mission results) | Depends on write support existing (a Mission's "created a Drive doc" step needs a write tool to have actually run) |
| **6** | PyBrowser MCP server (§8) | Deliberately last of the "core" phases — exposing PyBrowser *outward* should only happen once the inward-facing permission/audit model (phases 2-5) is proven solid, since the server reuses the identical `ToolRegistry.assess()` gate |
| **7** | ChatGPT / remote bridge (§10-11) | Depends on Phase 6 existing; also the phase most exposed to external-standard churn (Apps SDK, tunnel mechanism specifics), so latest is safest |

## 20. Test plan

**Unit:** MCP JSON-Schema → internal tool schema mapping (including edge cases: missing `description`, non-object root schema, deeply nested schemas); namespace generation and collision detection; sensitivity classifier against a table of representative tool names/schemas; per-tool/per-category/per-Mission permission resolution order; transport-level framing (stdio newline-delimited JSON-RPC, Streamable HTTP request/response and SSE parsing); error normalization for every documented MCP error shape.

**Integration:** a fake/local MCP server (stdio) for CI, covering: tool discovery, a full call round-trip, disconnect mid-call and reconnect, per-call timeout firing, an approval-required call blocking until a simulated user decision, Mission persistence of an MCP activity record across a save/reload cycle.

**Security:** a fake server that (a) describes a destructive tool with an innocuous name/description, verifying the pattern-based classifier still catches it by schema shape, not description; (b) returns a resource containing an injection attempt ("ignore previous instructions…"), verifying it's fenced identically to page content and the model's behavior doesn't change; (c) returns a malformed/oversized result, verifying it's normalized to an error rather than forwarded raw; (d) a tool schema that changes between discovery and call, verifying the stale-schema path in §16.

**E2E:** connect a real (sandboxed) MCP server → Ask Py successfully uses a read-only tool → a Mission combines a web search with an MCP read tool's result → a write-classified tool triggers the approval dialog and is blocked until approved → disconnect the server mid-Mission → reconnect → Mission resumes → audit log shows the full sequence with correct approval/timestamps.

**ChatGPT-specific:** an external MCP client (simulating ChatGPT's connector) connects via the pairing flow → calls a scoped read-only browser tool successfully → attempts a sensitive action → confirms it still requires the same in-app approval a local Claude-driven call would require (i.e., "external" grants no shortcut).

## 21. Risks

- **Spec churn.** MCP is still evolving quickly (the 2026-07-28 release candidate itself added new required headers and cache semantics since earlier drafts) — building against "current" risks a breaking update before ship. Mitigate by pinning to a released (non-RC) spec version and isolating the wire-format specifics inside `connection.py`, never leaking transport details into `adapter.py` or above.
- **Classifier false negatives.** A pattern-based sensitivity classifier (§6) will misclassify some genuinely destructive tool as `ELEVATED` rather than `SENSITIVE` if its name doesn't match an expected verb (e.g., a tool literally named `finalize`). Mitigate with the fail-closed default already specified, and treat the pattern list as a living allowlist that gets stricter over time, not a one-time table.
- **ChatGPT/Apps SDK surface is the least stable part of this whole design** (newer program than MCP itself, actively changing deploy/auth requirements) — Phase 7 placement (§19) is deliberately last so most of the churn risk is absorbed before PyBrowser depends on it.
- **User confusion between "MCP tool asks" and "browser action asks"** if the two approval dialogs look meaningfully different — mitigate by literally reusing the existing approval UI component, not building a parallel one, so a user learns one pattern for both.
- **Audit log becoming a second source of truth that drifts from Mission's own record** — mitigate by making Mission's MCP history (§14) a *view* over the same audit entries (§15), not a separately-maintained duplicate.

## 22. Effort/complexity estimate

Rough, in the same spirit as the model catalogue's honesty about tradeoffs — not a committed estimate:

| Phase | Relative size | Why |
|---|---|---|
| 1 — OpenAI provider | **Small** | Mostly a Responses-API sibling of an adapter that already exists and already works for two other providers |
| 2 — MCP client core (read-only) | **Large** | New transport handling (both stdio and Streamable HTTP), new discovery/registry/namespacing, first real MCP-spec surface area in the codebase |
| 3 — Permissions + audit (read-only scope) | **Medium** | Mostly composition of existing patterns (`Autonomy`, `ApiKeyStore`-adjacent storage), but the classifier (§6) is genuinely new design work, not just plumbing |
| 4 — Writes + full Settings UI | **Medium-Large** | The approval-dialog integration is small (reuse); the Settings UI (§7) with per-tool granularity is a real UI-design-and-build effort |
| 5 — Mission integration | **Medium** | Schema additions + a view layer over audit; no new hard problems |
| 6 — PyBrowser MCP server | **Medium-Large** | A second protocol implementation (server-side this time), plus the scoping work (§8) to keep the exposed surface deliberately narrow |
| 7 — ChatGPT bridge | **Large, and uncertain** | Depends on external, still-changing infrastructure (tunnel mechanism, Apps SDK requirements) PyBrowser doesn't control |

## 23. Recommendation

**Build now:** Phase 1 (OpenAI provider) — it's nearly free, ships independent value, and de-risks nothing about MCP by waiting.

**Build soon, in order:** Phases 2-3 (MCP client core + permissions/audit, read-only scope) — this is the part of the request with the clearest, most durable value ("Py can use my GitHub/Drive/Notion tools during Research") and the architecture already supports it cleanly (§2-§6). Do not skip straight to write-capable tools or the polished Settings UI before the permission/audit gate (§3) is proven on read-only traffic — that ordering is the single biggest lever against shipping an MCP integration that's fast to build and slow to trust.

**Build later, deliberately:** Phase 4 onward (writes, Mission integration, PyBrowser-as-server, ChatGPT bridge) — each depends on the phase before it and each touches either user trust (writes) or external, less-stable surfaces (Apps SDK/tunnel). None of this should start before Phases 1-3 have shipped and been used for real.

**Do not build yet, and do not market yet:** anything in §10-11 (remote ChatGPT connection) beyond this design document. Per the request's own §20: no MCP marketing on the public website until the feature is real, tested, and the write-path safety story (§13, §17) has actually shipped, not just been designed.

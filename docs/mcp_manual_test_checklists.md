# Manual Setup Checklists - External AI Clients

These are the manual, human-in-the-loop verification steps for each
supported client. **None of these have been performed against a live
ChatGPT, Claude Desktop, Cursor, or VS Code installation in this
environment** - this session has no such accounts or installed
applications available to it. What has been verified automatically (see
the Phase 12 test suite) is the shared machinery every checklist below
relies on: the real HTTP server, real pairing/auth/revocation, the real
config generators producing the documented shapes, and the real
verification engine against the real server. Do not report any row below
as "done" until a person with the actual client installed has walked
through it.

## Shared steps (every client)

1. Launch PyBrowser.
2. Settings → External AI Access → Enable (status should read "Running").
3. Click "Set up" on the client's card.
4. Confirm the permission preset, adjust if needed, click "Generate & Pair".
5. Copy the generated config / connection details.
6. Configure the client using those details (client-specific step below).
7. Ask the client to call a read tool, e.g. "list my open browser tabs".
8. Back in PyBrowser, click "Verify Connection" on the card (or dialog) -
   expect "Verified".
9. Settings → External AI Access → View Activity - confirm a
   `browser.list_tabs` entry attributed to the right client name appears.
10. Click "Revoke" on the client (or select it in the Advanced table and
    click Revoke).
11. Ask the client to call a tool again - it should fail (invalid/revoked
    token); confirm PyBrowser's audit log shows the failed attempt (or no
    new successful entry).

## Cursor

- [ ] Steps 1-5 above.
- [ ] Cursor → Settings → MCP → Add new MCP server (or paste into
      `mcp.json`) using the generated config.
- [ ] Restart/reload Cursor's MCP connection if it doesn't pick up the
      new server automatically.
- [ ] Ask Cursor's agent to call `browser.list_tabs` (e.g. "what tabs do I
      have open in PyBrowser?").
- [ ] Steps 8-11 above.

## Claude Desktop

- [ ] Steps 1-5 above (Claude's card generates a `claude_desktop_config.json`
      entry that launches `app/mcp_server/stdio_bridge.py`).
- [ ] Claude menu → Settings → Developer → Edit Config; paste the entry;
      restart Claude Desktop.
- [ ] Confirm Claude Desktop lists the new local MCP server as connected
      (its own UI, not PyBrowser's).
- [ ] Ask Claude to call `browser.list_tabs`.
- [ ] Steps 8-11 above.
- [ ] Additionally confirm: killing PyBrowser (or disabling the MCP
      server) makes the next call from Claude fail cleanly, rather than
      hanging - the bridge should return a JSON-RPC error, not crash.

## ChatGPT

- [ ] Confirm you have your own HTTPS tunnel in front of PyBrowser's MCP
      server before starting - PyBrowser does not provide one.
- [ ] Steps 1-4 above; the ChatGPT card will show "Requires a tunnel" and
      will not offer a "Verify Connection" button (verification requires
      a real HTTPS round trip PyBrowser cannot originate on your behalf).
- [ ] ChatGPT → Settings → Apps & Connectors → Developer Mode → Create;
      enter your tunnel's public URL + "Token" auth with the pairing
      token.
- [ ] Ask ChatGPT to call `browser.list_tabs`.
- [ ] Steps 9-11 above (verification itself must be done by hand here -
      there is no "Verify Connection" affordance for a tunneled client).

## VS Code

- [ ] Steps 1-5 above.
- [ ] Command Palette → "MCP: Open User Configuration" (or create
      `.vscode/mcp.json` in the workspace) and paste the generated config.
      Double-check the root key is `"servers"`, not `"mcpServers"` - the
      single most common mistake copying a config from another editor.
- [ ] Reload the MCP server from VS Code's MCP view if needed.
- [ ] Ask Copilot Chat (Agent Mode) to call `browser.list_tabs`.
- [ ] Steps 8-11 above.

## Generic MCP client

- [ ] Steps 1-5 above; the "Custom MCP Client" card shows the endpoint,
      transport, auth scheme and scopes as plain copyable text - no config
      file format is guessed.
- [ ] Configure the client per its own documentation using those details.
- [ ] Steps 7-11 above.

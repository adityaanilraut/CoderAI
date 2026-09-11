# MCP — Model Context Protocol

CoderAI connects to external MCP servers over **stdio** (child process) and
**SSE** (HTTP + event stream) transports (`coderai/core/mcp/transport.py`,
`coderai/core/mcp/client.py`). A server entry needs either a `command`
(stdio) or a `url` (SSE) — entries with neither are ignored.

---

## Setting up stdio servers

`~/.coderai/mcp.json` (global seed; project settings can add more):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {"NODE_ENV": "production"}
    }
  }
}
```

One-shot overlay without editing files:

```bash
coderai --mcp-config '{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]}}}'
```

## Setting up SSE servers

```json
{
  "mcpServers": {
    "remote-tools": {
      "url": "https://tools.example.com/sse",
      "headers": {"Authorization": "Bearer $TOKEN"}
    }
  }
}
```

## Layering & precedence

`load_global_mcp_servers()` seeds from `~/.coderai/mcp.json`; user settings,
project settings, then CLI overlays (`--mcp-config-file`,
`--mcp-config`) merge per server name — later layers win per key
(`merge_mcp_servers_dicts` in `coderai/core/mcp_files.py`).

## OAuth authentication

Servers requiring OAuth go through `coderai/mcp_oauth.py`. Authenticate once
via the login flow; tokens are cached in the share dir (`~/.coderai`) and
refresh without prompting. If a server returns 401 mid-session, `/mcp
reconnect <server>` re-runs the handshake.

## Tool approvals

MCP tools execute inside the same approval boundaries as built-in tools:

- Default: each mutating MCP call prompts for confirmation.
- `--yolo` / `/yolo`: auto-approve all tool actions.
- `--afk` / `/afk`: auto-pilot (questions auto-dismissed, calls approved).
- `--plan` / `/plan`: read-only — mutating tools stay prompt-gated even
  under `--yolo`.

Tool schema conversion happens at load: MCP JSON Schemas are translated to
the internal tool spec so approvals, dry-run verification, and checkpoint
undo treat external tools like first-party ones.

## The `/mcp` command suite

```text
/mcp                      # interactive server & tools inspector
/mcp prompts              # browse MCP server prompts
/mcp resources [uri]      # inspect resources, or read one by URI
/mcp reconnect <server>   # reconnect a failed/disconnected server
```

List/status variants from the Phase 5 e2e coverage: `list`, `reconnect`,
`tools`, `prompts`, `resources` (see
`tests_e2e/test_mcp_cli.py`).

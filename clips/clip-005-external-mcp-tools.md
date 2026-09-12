---
Status: Implemented
Date: 2026-09-11
---

# CLIP-005: External MCP Tools

Dynamic tool injection, MCP schema translation, approval boundaries, and
sidecar execution for external Model Context Protocol servers.

## Summary

External MCP servers (stdio child processes, SSE endpoints) contribute tools
at session load. Their JSON Schemas are translated to internal tool specs,
their calls execute through the same sandboxed pipeline as built-ins
(dry-run, approvals, checkpoint undo where applicable), and long-lived
helpers can run as *sidecars* alongside the turn loop.

## Motivation

- First-party tools can't cover every API (databases, SaaS, code search);
  MCP is the standard bridge.
- External tools must not bypass safety: same confirmation lattice, same
  read-only enforcement, same audit trail.
- Schema impedance (MCP JSON Schema vs internal spec) should be absorbed
  once at load, not per call.

## Design

### 1. Discovery & injection

`collect_cli_mcp_overlays()` + `load_global_mcp_servers()` seed from CLI
flags and `~/.coderai/mcp.json`; user → project → CLI layers merge per
server (`merge_mcp_servers_dicts`, `coderai/core/mcp_files.py`). At session
setup each server handshakes over its transport (`McpTransport`:
`stdio` via `command`, `SSE` via `url` — `coderai/core/mcp/transport.py`),
and its tool list materializes as internal tool definitions on the board.
`MCPLoadingBegin/End` + `MCPServerSnapshot`/`MCPStatusSnapshot` wire events
report progress to all consumers.

### 2. Schema translation

MCP `inputSchema` (JSON Schema) maps to the internal parameter spec:
`type`/`properties`/`required` preserved, `description` threaded through
for the model's benefit, unknown keywords dropped with a debug log (never
a hard failure — a missing keyword must not brick a whole server).
Translated specs are cached per server version; reconnect re-translates.

### 3. Approval boundaries

Injected tools carry an origin tag (`mcp:<server>/<tool>`) and resolve
permissions through the session's live mode:

| Mode | MCP mutating call |
|---|---|
| Default | Prompt per call (server + tool named in the request) |
| YOLO/AFK | Auto-approved; origin still logged |
| Plan Mode | Denied-gated: prompt even under YOLO, like built-ins |

Rejections return tool errors into context (not exceptions), so the model
can replan. `/mcp reconnect <server>` recovers crashed servers without
restarting the session.

### 4. Sidecar execution

Helpers that outlive a single call (watchers, dev servers, REPL-attached
runtimes) run as sidecars: spawned once, addressed by name, torn down with
the session. Sidecar stdout routes to `Notification` wire events so the
foreground turn isn't blocked. (Upstream reference: `kagent`
sidecar integration.)

## Failure handling

- Crashed stdio child → server marked down, tools listed as unavailable,
  in-flight call returns a tool error; `reconnect` respawns.
- Malformed schema → that tool skipped, server kept (partial availability
  beats total outage).
- SSE disconnect → exponential backoff reconnect; user-visible via
  `MCPStatusSnapshot`.

## Non-goals

- No tool-result caching across sessions; freshness beats speed for
  external state.
- No privilege elevation: an MCP server can never widen the session's
  sandbox, only operate inside it.

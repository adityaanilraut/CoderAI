# CoderAI UI ↔ Agent IPC Protocol

**Transport:** NDJSON over stdio. Every line is exactly one JSON object
terminated by `\n`. The Python agent writes events to `stdout`; the Ink UI
writes commands to the agent's `stdin`. `stderr` is reserved for the Python
logger and is captured by the UI for crash reports only.

**Version:** `v=1`. Every message carries `{"v": 1, "kind": "event" | "cmd"}`.

The canonical TypeScript shapes live in
[`ui/src/protocol.ts`](src/protocol.ts); the Python emitter is
[`coderAI/ipc/jsonrpc_server.py`](../coderAI/ipc/jsonrpc_server.py). When the
two disagree the code wins — please update this file in the same PR.

---

## Events (Agent → UI)

The protocol is intentionally narrow: there is one phased event for each of
the long-running things (`turn`, `tool`, `agent`) instead of a `*_start` /
`*_end` pair. New phases can be added without breaking older clients because
unknown phases are ignored.

| event           | payload                                                                                       | notes                                                                                |
| --------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `hello`         | `{model, provider, cwd, version, projectSummary?, contextLimit, budgetLimit, autoApprove}`    | First message after handshake                                                        |
| `ready`         | `{}`                                                                                          | Agent is idle and accepting `send_message`                                           |
| `turn`          | `{phase: "start" | "reasoning" | "text" | "end", delta?, elapsedMs?}`                          | One streamed assistant turn. `delta` carries incremental tokens for `reasoning`/`text`. |
| `tool`          | `{id, phase: "queued" | "awaiting_approval" | "running" | "ok" | "err" | "cancelled", payload}` | Lifecycle of a single tool call. `payload` shape depends on phase (see below).      |
| `file_diff`     | `{path, diff}`                                                                                | Unified diff string                                                                  |
| `status`        | `{ctxUsed, ctxLimit, costUsd, budgetUsd, promptTokens, completionTokens, totalTokens}`        | Emitted after every turn                                                             |
| `agent`         | `{phase: "started" | "update" | "finished", info: AgentInfo, parentId}`                       | Per-agent snapshot; multiple agents possible                                         |
| `session_patch` | `{model?, provider?, autoApprove?, reasoning?}`                                               | Partial session-state update — only changed fields are present                       |
| `info`          | `{message}`                                                                                   | Long-form reference output (`/show <topic>`, `/plan`) and short notices             |
| `warning`       | `{message}`                                                                                   | Non-fatal user-facing problem (unknown command, bad input)                           |
| `success`       | `{message}`                                                                                   | Positive confirmation toast (state change, async ack)                                |
| `error`         | `{category: "provider" | "tool" | "internal" | "protocol", message, hint?, details?}`         | Renders ErrorPanel, not a traceback dump                                             |
| `progress`      | `{label, current?, total?, kind: "tokens" | "files" | "steps"}`                               | Reserved for future progress bars (currently no UI)                                  |
| `goodbye`       | `{reason?}`                                                                                   | Agent is shutting down                                                               |

### `tool` phases — `payload` shape

| phase                | payload keys                                                  |
| -------------------- | ------------------------------------------------------------- |
| `queued` / `running` | `{name, category, args, risk}`                                |
| `awaiting_approval`  | `{name, args, risk, diff?}`                                   |
| `ok`                 | `{preview, fullAvailable}`                                    |
| `err`                | `{error, preview?}`                                           |
| `cancelled`          | `{reason?, timeoutSeconds?}`                                  |

`category` is one of `fs | git | shell | web | search | agent | mcp | other`.
`risk` is one of `low | medium | high`.

### `AgentInfo`

```jsonc
{
  "id": "agent_1a2b3c4d",
  "name": "main",
  "role": "planner",
  "parentId": null,
  "status": "thinking",        // idle|thinking|tool_call|waiting_for_user|done|error|cancelled (see Python AgentStatus)
  "task": "refactoring login flow",
  "tool": "read_file",
  "model": "claude-sonnet-4-6",
  "tokens": 12345,
  "costUsd": 0.0213,
  "ctxUsed": 8200,
  "ctxLimit": 200000,
  "elapsedMs": 4300
}
```

---

## Commands (UI → Agent)

| cmd                    | payload                                                | notes                                                                  |
| ---------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------- |
| `send_message`         | `{text}`                                               | User input (raw or slash-prefixed)                                     |
| `cancel`               | `{agentId?}`                                           | Cancel one agent or all if omitted (Esc key)                           |
| `tool_approval_resp`   | `{toolId, approve}`                                    | Responds to a `tool` `awaiting_approval` (id matches the tool id)      |
| `set_model`            | `{model}`                                              |                                                                        |
| `set_reasoning`        | `{effort: "high" | "medium" | "low" | "none"}`        |                                                                        |
| `set_default_model`    | `{model}`                                              | Persists default model for new sessions                                |
| `set_verbosity`        | `{level: "quiet" | "normal" | "verbose"}`             | Server-side filter (see below)                                         |
| `toggle_auto_approve`  | `{}`                                                   | Flip YOLO mode                                                         |
| `compact_context`      | `{}`                                                   | Triggers summarization                                                 |
| `clear_context`        | `{}`                                                   | Fresh session                                                          |
| `get_state`            | `{}`                                                   | Re-emit `status` + `agent` updates                                     |
| `get_plan`             | `{}`                                                   | Emits `info` with `.coderAI/current_plan.json` or a short notice       |
| `reference`            | `{topic}`                                              | Long-form help (`version`, `models`, `cost`, `system`, `config`, …)    |
| `exit`                 | `{}`                                                   | Graceful shutdown                                                      |

All commands carry an `id` for correlation.

### Verbosity filter

`normal` (default) drops `success` toasts at the source; `quiet` also drops
single-line `info`/`warning`; `verbose` passes everything. Multi-line `info`
(reference output) always passes regardless of level.

---

## Example transcript

```jsonl
{"v":1,"kind":"event","event":"hello","model":"claude-sonnet-4-6","provider":"AnthropicProvider","cwd":"/Users/a/proj","version":"0.1.0","contextLimit":200000,"budgetLimit":5.0,"autoApprove":false}
{"v":1,"kind":"event","event":"ready"}
{"v":1,"kind":"cmd","cmd":"send_message","id":"c1","text":"rename getCwd to getCurrentWorkingDirectory"}
{"v":1,"kind":"event","event":"turn","phase":"start"}
{"v":1,"kind":"event","event":"turn","phase":"text","delta":"I'll search the codebase…"}
{"v":1,"kind":"event","event":"tool","id":"t1","phase":"running","payload":{"name":"grep","category":"search","args":{"pattern":"getCwd"},"risk":"low"}}
{"v":1,"kind":"event","event":"tool","id":"t1","phase":"ok","payload":{"preview":"15 matches in 8 files","fullAvailable":true}}
{"v":1,"kind":"event","event":"file_diff","path":"src/a.ts","diff":"--- a/src/a.ts\n+++ b/src/a.ts\n@@ ... "}
{"v":1,"kind":"event","event":"turn","phase":"end"}
{"v":1,"kind":"event","event":"status","ctxUsed":12450,"ctxLimit":200000,"costUsd":0.021,"budgetUsd":5.0,"promptTokens":11200,"completionTokens":1250,"totalTokens":12450}
{"v":1,"kind":"event","event":"ready"}
```

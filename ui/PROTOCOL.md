# CoderAI UI ↔ Agent IPC Protocol

**Transport:** NDJSON over stdio. Every line is exactly one JSON object
terminated by `\n`. The Python agent writes events to `stdout`; the Ink UI
writes commands to the agent's `stdin`. `stderr` is reserved for the Python
logger and is surfaced in the UI as a collapsible "Logs" drawer.

**Version:** `v=1`. Every message carries `{"v": 1, "kind": "event" | "cmd"}`.

---

## Events (Agent → UI)

| event                | payload                                                                                  | notes                                              |
| -------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `hello`              | `{model, provider, cwd, version, projectSummary?, contextLimit, budgetLimit, autoApprove}` | First message after handshake                      |
| `ready`              | `{}`                                                                                     | Agent is idle and accepting `send_message`         |
| `assistant_start`    | `{}`                                                                                     | Marks beginning of a streamed reply                |
| `stream_delta`       | `{content, reasoning?}`                                                                  | Incremental token(s). `reasoning=true` for CoT     |
| `assistant_end`      | `{content}`                                                                              | Final complete message text                        |
| `thinking_start`     | `{}`                                                                                     | Spinner-worthy pause before streaming              |
| `thinking_end`       | `{elapsedMs}`                                                                            |                                                    |
| `tool_call`          | `{id, name, category, args, risk}`                                                       | `category`: fs, git, shell, web, search, agent, mcp |
| `tool_result`        | `{id, ok, preview, fullAvailable, error?}`                                               | Matches a prior `tool_call` by id                  |
| `tool_approval_req`  | `{id, tool, args, risk}`                                                                 | Blocks until `tool_approval_resp` command          |
| `file_diff`          | `{path, diff}`                                                                           | Unified diff string                                |
| `status`             | `{ctxUsed, ctxLimit, costUsd, budgetUsd, promptTokens, completionTokens, totalTokens}`   | Emitted after every turn                           |
| `agent_update`       | `{agent: AgentInfo}`                                                                     | Per-agent snapshot; multiple agents possible       |
| `agent_lifecycle`    | `{action: "started" \| "finished", agent: AgentInfo}`                                    |                                                    |
| `error`              | `{category: "provider" \| "tool" \| "internal", message, hint?, details?}`              | Renders ErrorPanel, not a traceback dump           |
| `info` / `warning` / `success` | `{message}`                                                                    | Short toasts                                       |
| `model_changed`      | `{model, provider}`                                                                      |                                                    |
| `goodbye`            | `{reason?}`                                                                              | Agent is shutting down                             |

### `AgentInfo`

```jsonc
{
  "id": "agent_1a2b3c4d",
  "name": "main",
  "role": "planner",
  "parentId": null,
  "status": "thinking",        // idle|thinking|tool_call|waiting|done|error|cancelled
  "task": "refactoring login flow",
  "tool": "read_file",
  "model": "claude-4-sonnet",
  "tokens": 12345,
  "costUsd": 0.0213,
  "ctxUsed": 8200,
  "ctxLimit": 200000,
  "elapsedMs": 4300
}
```

---

## Commands (UI → Agent)

| cmd                    | payload                    | notes                                                  |
| ---------------------- | -------------------------- | ------------------------------------------------------ |
| `send_message`         | `{text}`                   | User input (raw or slash-prefixed)                     |
| `cancel`               | `{agentId?}`               | Cancel one agent or all if omitted (Esc key)           |
| `tool_approval_resp`   | `{id, approve}`            | Responds to a `tool_approval_req`                      |
| `set_model`            | `{model}`                  |                                                        |
| `set_reasoning`        | `{effort: high|medium|low|none}` |                                                  |
| `toggle_auto_approve`  | `{}`                       |                                                        |
| `compact_context`      | `{}`                       | Triggers summarization                                 |
| `clear_context`        | `{}`                       | Fresh session                                          |
| `get_state`            | `{}`                       | Ask agent to re-emit `status` + `agent_update`*N      |
| `exit`                 | `{}`                       | Graceful shutdown                                      |

All commands carry a `{"id": "cmd_xxxxxxxx"}` for correlation. Agent replies
with matching-id events where applicable (e.g., `model_changed` for `set_model`).

---

## Example transcript

```jsonl
{"v":1,"kind":"event","event":"hello","model":"claude-4-sonnet","provider":"anthropic","cwd":"/Users/a/proj","version":"0.1.0","contextLimit":200000,"budgetLimit":5.0,"autoApprove":false}
{"v":1,"kind":"event","event":"ready"}
{"v":1,"kind":"cmd","cmd":"send_message","id":"c1","text":"rename getCwd to getCurrentWorkingDirectory"}
{"v":1,"kind":"event","event":"thinking_start"}
{"v":1,"kind":"event","event":"assistant_start"}
{"v":1,"kind":"event","event":"stream_delta","content":"I'll search the codebase…"}
{"v":1,"kind":"event","event":"tool_call","id":"t1","name":"grep","category":"search","args":{"pattern":"getCwd"},"risk":"low"}
{"v":1,"kind":"event","event":"tool_result","id":"t1","ok":true,"preview":"15 matches in 8 files","fullAvailable":true}
{"v":1,"kind":"event","event":"file_diff","path":"src/a.ts","diff":"--- a/src/a.ts\n+++ b/src/a.ts\n@@ ... "}
{"v":1,"kind":"event","event":"assistant_end","content":"Done — renamed in 8 files."}
{"v":1,"kind":"event","event":"status","ctxUsed":12450,"ctxLimit":200000,"costUsd":0.021,"budgetUsd":5.0,"promptTokens":11200,"completionTokens":1250,"totalTokens":12450}
{"v":1,"kind":"event","event":"ready"}
```

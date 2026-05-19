# CoderAI chat event reference

**Transport:** In-process callbacks from
[`coderAI/ipc/jsonrpc_server.py`](../coderAI/ipc/jsonrpc_server.py)
(`IPCServer`) to the Textual UI in
[`coderAI/tui/`](../coderAI/tui/).
The Textual `CoderAIApp` constructs an `IPCServer` and passes an
`on_event(name, data)` callback; the controller forwards `event_emitter`
notifications and per-turn streaming through that callback. Event names
are declared in
[`coderAI/tui/protocol.py`](../coderAI/tui/protocol.py).

---

## Events (Agent → UI)

The protocol is intentionally narrow: there is one phased event for each
of the long-running things (`turn`, `tool`, `agent`) instead of a
`*_start` / `*_end` pair. New phases can be added without breaking older
consumers because unknown phases are ignored.

| event           | payload                                                                                       | notes                                                                                |
| --------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `hello`         | `{model, provider, cwd, version, contextLimit, budgetLimit, autoApprove, reasoning}`          | First message emitted from `start()`                                                 |
| `ready`         | `{}`                                                                                          | Agent is idle and accepting `send_message`                                           |
| `turn`          | `{phase: "start" \| "reasoning" \| "text" \| "end", delta?, elapsedMs?, reasoningActive?}`     | One streamed assistant turn. `delta` carries incremental tokens for `reasoning`/`text`. `reasoningActive` hints whether extended thinking is in flight. |
| `tool`          | `{id, phase: "queued" \| "awaiting_approval" \| "running" \| "ok" \| "err" \| "cancelled", payload}` | Lifecycle of a single tool call. `payload` shape depends on phase (see below). `queued` is reserved for future use — Python currently emits `running` first. |
| `file_diff`     | `{path, diff}`                                                                                | Unified diff string                                                                  |
| `status`        | `{ctxUsed, ctxLimit, costUsd, budgetUsd, promptTokens, completionTokens, totalTokens}`        | Emitted after every turn                                                             |
| `agent`         | `{phase: "update" \| "started" \| "finished", info: AgentInfo, parentId}`                     | Per-agent snapshot; `started`/`finished` are lifecycle edges, `update` is throttled live sync |
| `session_patch` | `{model?, provider?, autoApprove?, reasoning?}`                                               | Partial session-state update — only changed fields are present                       |
| `available_models`| `{current, models: Record<string, string[]>}`                                                 | Emitted for the model picker                                                         |
| `available_personas`| `{current, personas: string[]}`                                                             | Emitted for the persona picker                                                       |
| `available_skills`| `{skills: {name: string, description: string}[]}`                                             | Emitted for the skill picker                                                         |
| `context_state` | `{files: {path: string, size: number}[]}`                                                     | Emitted during get_state to show pinned context files                                |
| `info`          | `{message}`                                                                                   | Long-form reference output (`/show <topic>`, `/plan`) and short notices             |
| `warning`       | `{message}`                                                                                   | Non-fatal user-facing problem (unknown command, bad input)                           |
| `success`       | `{message}`                                                                                   | Positive confirmation toast (state change, async ack)                                |
| `error`         | `{category: "provider" \| "tool" \| "internal" \| "protocol", message, hint?, details?}`         | Renders in the timeline as a styled error row, not a traceback dump                  |
| `progress`      | `{label, current?, total?, progressKind: "tokens" \| "files" \| "steps"}`                       | Tool progress updates forwarded from the Python executor                             |
| `goodbye`       | `{reason?}`                                                                                   | Agent is shutting down                                                               |

### `tool` phases — `payload` shape

| phase                | payload keys                                                  |
| -------------------- | ------------------------------------------------------------- |
| `queued` / `running` | `{name, category, args, risk}`                                |
| `awaiting_approval`  | `{name, args, risk, diff?}`                                   |
| `ok`                 | `{preview, fullAvailable}`                                    |
| `err`                | `{error, preview?}`                                           |
| `cancelled`          | `{reason?, timeoutSeconds?}`                                  |

`category` is one of `filesystem | git | terminal | web | search | memory | agent | mcp | other`.
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

UI intent flows through `IPCServer.enqueue_command(cmd, **fields)` or
`submit_command(...)`. Each command is dispatched on the asyncio loop;
`send_message` is serialised by the per-server `_turn_lock` so two user
turns can't interleave.

| cmd                    | fields                                                 | notes                                                                  |
| ---------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------- |
| `send_message`         | `{text}`                                               | User input (raw or slash-prefixed)                                     |
| `cancel`               | `{agentId?}`                                           | Cancel one agent or all if omitted (Esc key)                           |
| `cancel_agent`         | `{payload: {agentId}}`                                 | Cancel a specific sub-agent by tracker id                              |
| `tool_approval_resp`   | `{toolId, approve}`                                    | Responds to a `tool` `awaiting_approval` (id matches the tool id)      |
| `set_model`            | `{model}`                                              |                                                                        |
| `set_persona`          | `{persona?}` or `{payload: {persona?}}`                | Switch persona; empty/`default` clears. UI usually uses `/persona` via `send_message`. |
| `set_reasoning`        | `{effort: "high" \| "medium" \| "low" \| "none"}`        |                                                                        |
| `set_default_model`    | `{model}`                                              | Persists default model for new sessions                                |
| `set_verbosity`        | `{level: "quiet" \| "normal" \| "verbose"}`             | Server-side filter (see below)                                         |
| `toggle_auto_approve`  | `{}`                                                   | Flip YOLO mode                                                         |
| `compact_context`      | `{}`                                                   | Triggers summarization                                                 |
| `clear_context`        | `{}`                                                   | Fresh session; clears sub-agents from tracker, resets main to idle     |
| `get_state`            | `{}`                                                   | Re-emit `status` + `agent` updates                                     |
| `get_plan`             | `{}`                                                   | Emits `info` with `.coderAI/current_plan.json` or a short notice       |
| `list_models`          | `{}`                                                   | Emits `available_models` for the model picker                          |
| `list_personas`        | `{}`                                                   | Emits `available_personas` for the persona picker                      |
| `list_skills`          | `{}`                                                   | Emits `available_skills` for the skills picker                         |
| `search_codebase`      | `{query}`                                              | Emits `info` with codebase search results                              |
| `reference`            | `{topic}`                                              | Long-form help (`version`, `models`, `cost`, `system`, `config`, …)    |
| `exit`                 | `{}`                                                   | Graceful shutdown                                                      |

### Verbosity filter

`normal` (default) drops `success` toasts at the source; `quiet` also drops
single-line `info`/`warning`; `verbose` passes everything. Multi-line `info`
(reference output) always passes regardless of level. Structural events
(`turn`, `tool`, `agent`, `status`, etc.) are never filtered.

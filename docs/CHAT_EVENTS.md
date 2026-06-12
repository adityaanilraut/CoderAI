# CoderAI chat event reference

**Transport:** In-process callbacks from
[`coderAI/bridge/controller.py`](../coderAI/bridge/controller.py)
(`UIBridge`) to the Textual UI in
[`coderAI/tui/`](../coderAI/tui/).
The Textual `CoderAIApp` constructs an `UIBridge` and passes an
`on_event(name, data)` callback; the controller forwards `event_emitter`
notifications and per-turn streaming through that callback. The event
catalog is documented here; outbound events are emitted by `UIBridge`
and inbound events are reduced into session/timeline state by
[`coderAI/tui/listeners.py`](../coderAI/tui/listeners.py) (`EventReducer`).

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
| `status`        | `{ctxUsed, ctxLimit, costUsd, budgetUsd, promptTokens, completionTokens, totalTokens, iteration, maxIterations, elapsedSeconds}` | Emitted after every turn. `iteration` is the current agent-loop pass (1-based after the first user message). `maxIterations` mirrors `config.max_iterations` (default 50). `elapsedSeconds` is wall time since session bootstrap. |
| `plan_card`     | `{plan: {title, completed, total, currentIdx, steps: [{index, description, status}]}}`        | Structured plan snapshot for the timeline card (from `/plan` or the plan tool)       |
| `skill_card`    | `{id?, name, description, steps: [{index, label}]}`                                           | Parsed skill workflow card emitted after a successful `use_skill` call               |
| `tasks_card`    | `{tasks: {summary, inProgress, pending, completed, total}}`                                   | Task-list snapshot. `inProgress`/`pending`/`completed` are arrays of `{id, title, priority, status}` sorted by priority; `completed` holds only the last 5. `summary` is a human-readable count string. Updates the session task panel (chrome), not a timeline row. |
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
| `progress`      | `{label, current?, total?, progressKind: "tokens" \| "files" \| "steps", elapsed?}`             | Tool progress updates forwarded from the Python executor. `elapsed` (seconds) is set when the underlying tool reports it. |
| `goodbye`       | `{reason?}`                                                                                   | Agent is shutting down                                                               |

### `tool` phases — `payload` shape

| phase                | payload keys                                                  |
| -------------------- | ------------------------------------------------------------- |
| `queued` / `running` | `{name, category, args, risk}`                                |
| `awaiting_approval`  | `{name, args, risk, diff?, requestedBy, parentId?, iteration, maxIterations, priorApproved}` | Extended modal context: who requested approval, sub-agent parent, loop iteration counters, and how many tools were already approved this turn. |
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
  "elapsedMs": 4300,
  "depth": 0                 // tree depth from main (0 = root agent, +1 per parent hop)
}
```

`depth` is computed server-side by walking the `parentId` chain in
`agent_tracker`. The Textual reducer maps the same field onto
`SessionState.agents[id].depth`.

### Timeline kinds produced by `EventReducer`

The reducer in `listeners.py` mirrors several wire events into timeline
rows the Textual UI renders:

| wire event   | timeline `kind` | notes                                              |
| ------------ | ----------------- | -------------------------------------------------- |
| `turn`       | `assistant`       | streaming text/reasoning coalesced ~120 ms         |
| `tool`       | `tool` / `approval` | `awaiting_approval` becomes an `approval` row    |
| `plan_card`  | `plan_card`       | title, step list, progress counters                |
| `skill_card` | `skill_card`      | skill name, description, numbered steps            |
| `file_diff`  | `diff`            | unified diff block                                 |
| `info`/`warning`/`success` | `toast` | level mirrors the wire event name          |
| `error`      | `error`           | also triggers incomplete-turn recovery             |

---

## Commands (UI → Agent)

UI intent flows through `UIBridge.enqueue_command(cmd, **fields)` or
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
| `allow_tool`           | `{tool}`                                               | Add a tool to the session approval allowlist (skips future approval prompts); confirms via `info` |
| `disallow_tool`        | `{tool}`                                               | Remove a tool from the session approval allowlist; confirms via `info` |
| `list_allowed_tools`   | `{}`                                                   | Emits `info` listing the session's always-allowed tools                |
| `compact_context`      | `{}`                                                   | Triggers summarization                                                 |
| `clear_context`        | `{}`                                                   | Fresh session; clears sub-agents from tracker, resets main to idle     |
| `manage_context`       | `{action: "add" \| "remove", path}`                     | Pin/unpin a file in the pinned-context manager; emits `success`/`warning`, then `context_state` + `status` |
| `get_state`            | `{}`                                                   | Re-emit `status` + `agent` updates                                     |
| `get_plan`             | `{}`                                                   | Emits `info` with `.coderAI/current_plan.json` or a short notice       |
| `get_tasks`            | `{}`                                                   | Re-emits `tasks_card` from the on-disk task list                       |
| `init_project`         | `{}`                                                   | Scaffolds `.coderAI/{agents,skills,rules}` and starter files (`coderai.md`, …) in the project root; emits `success` or `error` |
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

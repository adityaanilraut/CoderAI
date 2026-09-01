# CoderAI Multi-Agent Orchestration — Capability Map

Read-only audit of the Python codebase at `/Users/adityaraut/Desktop/CoderAI-main`.
All references are `path:line`. This map mirrors the requested sections
(A) Dynamic worker spawning & pooling, (B) Subagent lifecycle & state isolation,
(C) Inter-agent communication & synchronization, (D) Concurrent tool invocation &
rate limiting, (E) CLI/env contract, followed by a consolidated gaps/stubs section.

---

## A. Dynamic Worker Spawning & Pooling

### A.1 Concurrency primitives in use (factual inventory)

| Primitive | Where used | Notes |
|---|---|---|
| `asyncio.Semaphore` | `subagent.py:438`, `executor.py:104`, `workflow/engine.py:393-394` | bounds concurrency where explicitly requested |
| `asyncio.gather(..., return_exceptions=True)` | `subagent.py:445` | parallel subagent fan-out |
| `asyncio.gather(*tasks)` (no `return_exceptions`) | `workflow/engine.py:380,416`, `executor.py:154` | pipeline/parallel thunks; tool parallel path |
| `asyncio.create_task` | `agents.py:374` | continuable background subagent (no `TaskGroup`) |
| `asyncio.Event` (abort/controller) | `subagent.py:240-241`, `session.py:223`, `agent_loop.py:137`, `session.py:663-669` | cancellation signalling |
| `asyncio.Lock` | `mailbox.py:45,137`, `path_lock.py:21-23,57` | mailbox + per-path RW locks |
| `asyncio.wait_for(...)` | `subagent.py:276,299,323,338`, `executor.py:487,492,539` | timeouts |
| `asyncio.to_thread(...)` | `subagent.py:609`, `subagent_backends/base.py:43`, `executor.py:474`, `network/client.py:248,267`, `session.py:1616` | offload blocking I/O |
| `threading.Lock` / `threading.RLock` | `jobs.py:54`, `goals.py:79`, `schedule.py:57` | in-process thread-safe stores |

**No `asyncio.TaskGroup`, no `ThreadPoolExecutor`, no `concurrent.futures`, no
work queue (`asyncio.Queue` is used only as the mailbox `PriorityQueue`), and no
`multiprocessing`** anywhere in the orchestration paths. Everything is cooperative
single-process asyncio.

### A.2 One-shot subagent (`Task` tool)

- Entry point: `handle_subagent_tool` in `tools/subagent.py:11-103`.
- Constructs a **new** `SubAgentManager` per call (`tools/subagent.py:47-50`) — see
  §B for the isolation consequence.
- Builds a `SubAgentSpec` and calls `manager.spawn_subagent(spec)` **synchronously**
  (awaited to completion; no fan-out): `tools/subagent.py:94`.
- Defaults: `mode="read_only"`, `timeout_seconds=90.0` (`tools/subagent.py:30-38`),
  `max_depth=3` (`tools/subagent.py:58`), optional `token_budget`, optional
  `fork_parent_history`→seed messages from the parent JSONL store
  (`tools/subagent.py:67-79`).

### A.3 Parallel subagent fan-out (`run_parallel_subagents`)

- `SubAgentManager.run_parallel_subagents(specs, max_concurrency=4)` —
  `subagent.py:429-462`.
- Semaphore-bounded concurrent `spawn_subagent` calls; `asyncio.gather(return_exceptions=True)`;
  exceptions are folded into `SubAgentResult(status="failed")`.
- **Gap:** this method has **no caller** anywhere in the codebase (only its
  definition matched a repo-wide search). There is no model-facing tool that fans
  out `Task`/subagents in parallel today; the only real fan-out path is the
  workflow engine (§A.5).

### A.4 Continuable background subagent (`subagent` tool)

- `tools/agents.py:handle_continuable_subagent_tool` (`agents.py:111-176`) →
  `spawn_background_agent(manager, spec)` (`agents.py:308-375`).
- `spawn_background_agent` performs **lineage propagation** (parent/root/depth/
  children_ids) and then `asyncio.create_task(_run())` (`agents.py:374`) — a
  fire-and-forget task, **no global concurrency cap / semaphore / task group**.
- Returns an `AgentHandle` id immediately; steering via `send_message`,
  `interrupt_agent`, `list_agents`, and child reporting via `report`
  (`tools/agents.py:179-250`).

### A.5 Workflow engine fan-out

- `WorkflowEngine` (`workflow/engine.py:262-416`).
- `agent(prompt, opts)` runs one subagent via `spawn_subagent` (`engine.py:319`).
- `parallel(thunks, max_concurrency=None)` (`engine.py:384-416`): `asyncio.gather`
  over thunks; semaphore only if `max_concurrency` is supplied — otherwise **unbounded**.
- `pipeline(items, *stages)` (`engine.py:352-382`): per-item coroutine runs
  stage0→…→stage_k sequentially; all items gathered with **no inter-stage barrier
  and no concurrency limit** (matches the docstring's "true streaming fan-out", but
  with zero backpressure).
- Workflow script is executed with `compile(...)+exec(...)` in-process
  (`engine.py:490-492`) under a curated `__builtins__` namespace
  (`engine.py:433-472`). See §G for the isolation caveat.

### A.6 Subagent backends (provider abstraction)

- Abstract base `CliSubagentDriver` (`subagent_backends/base.py:17-100`) runs a CLI
  via `asyncio.to_thread(subprocess.run, ...)` with `capture_output=True` and a
  hard `timeout`.
- Concrete drivers: `ClaudeCodeDriver` (`claude_code.py`), `CodexDriver`
  (`codex.py`), `AcpSubagentDriver` (`acp.py`). All map a structured dict back to
  `{ok, status, summary, error, duration_seconds}`.
- Dispatch in `spawn_subagent` by `spec.provider` ∈ `{in_process, claude_code,
  codex, acp}` (`subagent.py:264-341`). Each external path is wrapped in
  `asyncio.wait_for(..., spec.timeout_seconds)`.
- `SubagentDescriptor` (`spawn.py:46-77`) carries `mode` (`one-shot`/`continuable`),
  `provider`, `label`, `agentProvider/agentModel`, `persona`, and a
  `ToolRestriction` (`spawn.py:23-43`) allow/deny filter. Note: the descriptor's
  `mode`/`provider` are parsed but the actual run mode is driven by `spec.mode` and
  `spec.provider`; the descriptor is only consulted for its `tool_filter`
  (`subagent.py:870-875`).

---

## B. Subagent Lifecycle & State Isolation

### B.1 Specification & result types

- `SubAgentSpec` (`subagent.py:42-76`): description, prompt, task_id (8-hex uuid),
  mode (`read_only`|`general`), provider, timeout_seconds, max_iterations, depth,
  max_depth, token_budget/max_tokens, isolated_cwd, scratchpad_dir, dry_run,
  parent_session_id, allowed_tools, extra_context, agent_id, parent_agent_id,
  root_agent_id, children_ids, handle, seed_messages/seed_events, descriptor.
- `SubAgentResult` (`subagent.py:80-148`): status ∈ `completed | failed | interrupted
  | timeout | max_iterations | budget_exceeded`; token accounting, iterations,
  tool_calls_count, duration_seconds, error, artifacts, diffs, exit_code
  (0/1/2/124/130), token_telemetry, lineage, lifecycle_events; `to_dict()` and
  `format_markdown()`.

### B.2 Spawn lifecycle (`spawn_subagent`, `subagent.py:201-427`)

1. Derive `session_id = sub_<parent[:8]|root>_<task_id>` (`subagent.py:203-205`).
2. Emit `subagent/spawn` lifecycle event.
3. Depth quota: `check_subagent_depth_quota(depth, max_depth)`
   (`spawn.py:129-139`) — **denies when `current_depth >= max_depth`** (default 3).
   On failure returns `status="failed"`, exit_code 1, and a
   `RecursionLimitError` summary (`subagent.py:214-235`).
4. Create scratchpad dir `setup_subagent_scratchpad` (`.coderai/scratch/<session_id>`
   with tempdir fallback) (`subagent.py:237-238`, `spawn.py:142-159`).
5. Register `abort_event = asyncio.Event()` in `_active_controllers`
   (`subagent.py:240-241`).
6. Fire `run_on_subagent_spawn` hook (`subagent.py:250-261`).
7. Execute backend (external CLI or in-process loop) under `asyncio.wait_for`
   (`subagent.py:338-341`).
8. Attach lineage + lifecycle events; emit `subagent/complete` or `subagent/error`
   (`subagent.py:342-360`).
9. **finally:** pop controller and `clear_session_state(session_id)`
   (`subagent.py:425-427`).

### B.3 Error/timeout/cancellation mapping (`subagent.py:362-424`)

- `asyncio.TimeoutError`/`TimeoutError` → `status="timeout"`, exit_code **124**.
- `asyncio.CancelledError` → `status="interrupted"`, exit_code **130**.
- Any other exception → `status="failed"`, exit_code 1 (logged).

### B.4 In-process subagent loop (`_run_subagent_loop`, `subagent.py:464-841`)

- Independent message history: system prompt from `get_subagent_system_prompt(mode)`
  + runtime context + optional seed messages (`subagent.py:503-524`).
- `for iteration in range(1, max_iterations+1)` (default **20**,
  `spawn.py:20`); checks `abort_event.is_set()` at loop top and before each tool
  call (`subagent.py:536,762`).
- **Steering inbox:** drains `spec.handle.inbox` or the `AgentRegistry` handle's
  inbox each iteration and injects `[Steering from parent]: …` as a user message
  (`subagent.py:564-585`).
- LLM call: `await asyncio.to_thread(_call_llm_sync, client, request)` — blocking
  sync call offloaded to a thread, **no streaming** (`subagent.py:609`).
- Token budget check after each response → `status="budget_exceeded"` exit_code 2
  (`subagent.py:650-676`); model `refusal` → `failed` (`subagent.py:688-715`).
- No tool_calls → `status="completed"` (`subagent.py:730-757`).
- Tool execution uses its own `ToolExecutor` with `ToolExecutionHooks`
  (`should_stop=abort_event.is_set`, `isolated_cwd`, `dry_run`,
  `on_after_file_mutation` collects diffs) (`subagent.py:789-797`).
- Exhausting iterations → `status="max_iterations"`, exit_code 2
  (`subagent.py:815-841`).

### B.5 Sandboxing of tools (`_get_sandboxed_tools`, `subagent.py:843-880`)

- Uses `get_tools({"nonInteractive": True, "childAgent": True})`; filters out:
  `AskUserQuestion` (always), `Task`/`subagent`/`subagent_fork` when
  `depth >= MAX_SUBAGENT_DEPTH`, mutating tools in `read_only` mode
  (`write/Write/edit/Edit`), anything outside `allowed_tools`, and anything denied
  by `descriptor.tool_filter`. Also enforces `read_only` at execution time
  (`subagent.py:780-787`).
- Output canonicalization via `format_tool_definitions` + `order_tools`
  (`subagent.py:879-880`).

### B.6 State isolation

- **File/snippet state:** subagents get their own `session_id`; `SessionStateManager`
  (`state.py:56-217`) keys all snippet/file-version state by session id and
  `clear_session_state(session_id)` is called in the spawn `finally`
  (`subagent.py:427`).
- **Session log isolation:** the subagent loop does not write to the parent
  session log; results are returned as `SubAgentResult` and rendered by the parent
  tool into the parent's message stream.
- **Filesystem isolation:** optional `isolated_cwd`/`scratchpad_dir`; scratchpad is
  created under `.coderai/scratch/<session_id>` (`spawn.py:148-151`) but is **not
  a chroot/container** — subagents share the same process, filesystem, and OS
  credentials. The `read_only` mode is an **advisory tool filter**, not an OS-level
  sandbox.
- **Oversized output isolation:** `spill.py` spills tool output >`30_000` bytes
  (UTF-8) to a private session dir (`DEFAULT_MAX_INLINE_BYTES`, `spill.py:20`),
  skipping `read` (`SPILL_SKIP_TOOLS`, `spill.py:19`), applied in
  `ToolExecutor._apply_result_spill` (`executor.py:564-583`).

### B.7 Cancellation & liveness

- `SubAgentManager.cancel_subagent(session_id)` / `cancel_all()`
  (`subagent.py:190-199`) set the abort event for a given manager instance.
  **Gap:** each `Task` tool call creates a fresh `SubAgentManager`
  (`tools/subagent.py:47-50`), so `_active_controllers` does not span manager
  instances; a parent's `cancel_all()` cannot reach one-shot children spawned via
  other manager instances.
- Continuable path: `AgentRegistry.interrupt(agent_id)` sets status and
  `handle.task.cancel()` (`agents.py:107-114`); `interrupt_tree` recurses children
  (`agents.py:116-128`).
- `TaskSupervisor` (`agents.py:138-294`) tracks heartbeats and can reap agents
  idle >60s (`check_liveness`, `agents.py:278-290`); `kill_all_tasks`,
  `cleanup_session_tasks` for session teardown. **No periodic watchdog actually
  calls `check_liveness`** — it is defined but not scheduled.

### B.8 Cross-session references (`common/session_reference.py`)

- Resolves `@session:<id>`, `session:<id>`, `dsh-session:` URIs from user text
  (`SESSION_MENTION_PATTERN`, `session_reference.py:16-21`).
- `resolve_session_references` (`session_reference.py:156-196`) renders bounded
  snapshots of prior sessions: max **3** references, **4096** bytes each
  (`session_reference.py:22-23`). This is the only cross-session context-injection
  mechanism (distinct from subagent fork seeding).

---

## C. Inter-Agent Communication & Synchronization

### C.1 Teams subsystem (`core/teams/*`)

- **Models** (`teams/models.py`):
  - `TeamMessage` (`models.py:11-30`): message_id, sender, recipient (`all` or id),
    content, timestamp, task_id.
  - `TeamTask` (`models.py:33-64`): status ∈ `pending|in_progress|completed|blocked|
    failed`, priority ∈ `low|medium|high|critical`, `dependencies` list,
    `revision` counter (CAS).
  - `Teammate` (`models.py:67-96`): role ∈ `architect|coder|reviewer|tester|
    researcher|coordinator`, mode, status ∈ `idle|working|completed|failed|
    interrupted`, `inbox`/`outbox` lists.
- **Mailbox** (`teams/mailbox.py`):
  - `MessagePriority` IntEnum LOW/NORMAL/HIGH/CRITICAL (`mailbox.py:18-22`).
  - `AsyncMailbox` = `asyncio.PriorityQueue` (bounded, `DEFAULT_MAILBOX_CAPACITY=
    1000`, `mailbox.py:15,41-43`) guarded by `asyncio.Lock`. `send_nowait` **drops
    and logs** on `QueueFull` (`mailbox.py:86-109`); `send_async` supports timeout.
    Priority is inverted (higher enum pops first) with monotonic sequence tiebreak.
  - `ActorChannel` topic pub-sub + broadcast over registered mailboxes
    (`mailbox.py:131-186`).
- **Concurrency control** (`teams/concurrency.py`):
  - `ConcurrencyConflictError` carries resource_id/expected/actual revision
    (`concurrency.py:18-31`).
  - `cas_retry_async`/`cas_retry_sync` retry up to **5** times with exponential
    backoff + full jitter (`initial 0.05s`, `max 1.0s`, `factor 2`)
    (`concurrency.py:34-132`).
- **Deadlock detection** (`teams/deadlock.py`):
  - DFS cycle detector `detect_task_cycles` (`deadlock.py:28-73`),
    `assert_acyclic_dependencies` (`deadlock.py:76-89`) → `CycleDetectedError`.
  - `InterAgentWaitWatchdog` (`deadlock.py:92-134`) records waiter→target edges,
    rejects self-wait, and raises `DeadlockError` on circular waits. **Defined but
    not instantiated/wired into `TeamManager.wait_agent`** (see gap below).
- **Manager** (`teams/manager.py`):
  - `TeamTaskBoard` (`manager.py:20-122`): `create_task` validates DAG acyclicity,
    `update_task` enforces `expected_revision` CAS (raises
    `ConcurrencyConflictError`, `manager.py:86-93`), `can_start_task` checks all
    dependencies are `completed` (`manager.py:113-122`).
  - `TeamManager` (`manager.py:125-324`): `spawn_teammate` registers a `Teammate`
    + mailbox; `send_message` routes to target inbox + mailbox (and broadcast to
    `all`); `wait_agent(agent_ids, timeout_seconds=60, wait_for∈{completion,
    message, any_settlement})` **polls `asyncio.sleep(0.5)`** in a loop against
    `Teammate`/`AgentRegistry` statuses (`manager.py:205-310`).
  - Global singleton `get_team_manager()` / `reset_team_manager()`
    (`manager.py:313-323`).
- **Team tools** (`teams/tools.py`): `spawn_teammate`, `team_task_create/get/list/
  update`, `wait_agent` — all operate on the in-memory global manager; registered
  in `registry.py:1514-1617`.

### C.2 Continuable-agent control plane (`core/agents.py`)

- `AgentHandle` (`agents.py:15-48`): id, parent_session_id, description, mode,
  status, `inbox: list[str]`, result, task, spec, report, lineage, lifecycle
  history, heartbeats.
- `AgentRegistry` (`agents.py:51-128`): `register/get/list/get_children/get_tree/
  send/interrupt/interrupt_tree`. In-memory only (lost on process exit).
- Model-facing controls: `send_message`, `interrupt_agent`, `list_agents`, `report`
  (`tools/agents.py:179-250`).

### C.3 Result aggregation / dedupe

- Parallel fan-out aggregates `SubAgentResult` list preserving order
  (`subagent.py:444-462`).
- Ralph aggregates per-round `RalphRound` records with cumulative token/duration
  (`tools/ralph.py:283-404`).
- Workflow aggregates phases/logs/tokens/agent_executions into `WorkflowResult`
  (`workflow/engine.py:80-144`).
- **No deduplication** of overlapping subagent artifacts exists; `artifacts` are
  per-result file lists and are not de-duplicated across children.

### C.4 Gaps in inter-agent coordination

- `spawn_teammate` is a **stub with respect to execution**: it creates a
  `Teammate` record + mailbox but **never launches an agent**; there is no binding
  between `TeamManager` and `SubAgentManager`/`AgentRegistry`, so teammates never
  actually run and `Teammate.status` stays `idle` (nothing sets `working`).
- `TeamManager._active_tasks` (`manager.py:132`) is **never used**.
- `InterAgentWaitWatchdog` is defined but not used by `wait_agent`; the 0.5s poll
  loop is the only synchronization primitive actually exercised.
- `TeamMessage` routing writes to both `Teammate.inbox` (a plain list) and the
  `AsyncMailbox` (`manager.py:168-199`), but the agent loop reads only the
  `AgentRegistry` handle inbox (`subagent.py:564-585`) — the mailbox queue is not
  drained by the agent loop, so mailbox messages are effectively write-only.
- `run_parallel_subagents` has no caller (see §A.3).

---

## D. Concurrent Tool Invocation & Rate Limiting

### D.1 Tool execution pipeline (`tools/executor.py`)

`ToolExecutor.execute_tool_call` runs a 10-stage pipeline
(`executor.py:181-308`): (1) parse args (with JSON repair), (2) permission +
pre-execute + guard gate, (3) registry lookup + JSON-schema validation,
(4) dispatch handler, (5) `finalize_content`, (6) result spill, (7) presentation
meta, (8) deferred contexts / `concludes_turn`, (9) post-execute hooks,
(10) secret sanitization.

- **Parallel vs sequential:** `execute_tool_calls(..., parallel=False)`
  (`executor.py:106-175`). Parallel path (`parallel=True` and >1 calls) uses
  `asyncio.Semaphore(DEFAULT_TOOL_CONCURRENCY=8)` (`executor.py:46,104,145`) and
  `asyncio.gather`. Sequential path runs calls in order, honoring `should_stop`.
- **Caller-side grouping:** `SessionManager._append_tool_messages` chunks a turn's
  tool calls into `parallel`/`sequential`/`barrier`/`blocked` groups
  (`session.py:1270-1321`) using `ToolDefinition.check_execution_mode`
  (`types.py:217-230`). Read-only tools (`read, grep, glob, WebSearch, WebFetch,
  lsp, session_query, UnderstandImage` and case variants) default to `parallel`
  when no explicit mode is set (`session.py:1226-1241,1300-1303`). The parallel
  flag is only true for `chunk_kind == "parallel" and len(chunk_tcs) > 1`
  (`session.py:1417-1420`).
- **Handler timeout:** per-tool `timeout_ms` (none of the builtins set it) or hook
  `timeout_ms`, enforced via `asyncio.wait_for` (`executor.py:452-494`).
- **Write-path serialization:** mutating tools (`write/edit/str_replace_editor/
  patch/apply_patch`) acquire an exclusive per-path lock
  `PathLockManager.acquire_write_lock` (`executor.py:481-489`, `path_lock.py:74-92`).
  `PathLockEntry` implements a readers-writer lock with `asyncio.Lock`
  (`path_lock.py:16-49`). **Note:** the read tools do not actually call
  `acquire_read_lock` in the executor — only the write path is gated.
- MCP fallback: unknown tools dispatched to `mcp_manager.execute_mcp_tool`
  (`executor.py:517-562`).

### D.2 Rate limiting

- `SlidingWindowRateLimiter` (`executor.py:49-84`): per-key in-memory sliding
  window; `acquire(key, max_requests, window_seconds)` returns `(allowed,
  retry_after)`.
- Enforced in `_pre_execute_deny` only when a `ToolDefinition.rate_limit` tuple is
  set (`executor.py:404-417`) with key `f"{session_id}:{tool_name}"`.
- **Gap:** **no builtin tool sets `rate_limit`** (repo-wide grep shows
  `rate_limit=` only in the dataclass definitions `types.py:203` and
  `schema.py:187`; the registry uses only `rate_limited_id`). Therefore
  `SlidingWindowRateLimiter` is effectively **dormant**.
- `rate_limited_id` markers: `WebSearch` (`registry.py:826`), `WebFetch`
  (`registry.py:844`), `UnderstandImage` (`registry.py:1313`). This is a
  *plugin-level* marker; only `understand_image.py:116-117` actually invokes
  `on_plugin_rate_limit_exceeded`. `WebSearch`/`WebFetch` carry the marker but
  perform **no in-process throttling**.
- **LLM retry/backoff** (separate concern, `common/llm_retry.py`):
  - `RETRYABLE_CODES` = `EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT,
    CONTEXT_OVERFLOW, QUOTA_EXCEEDED` (`llm_retry.py:15-23`).
  - `classify_llm_failure` maps HTTP 429 / 5xx / timeout / transport / quota /
    context-overflow by message heuristics (`llm_retry.py:33-99`).
  - `retry_delay_ms`: exponential backoff **default 5 retries, 500ms→10s, ±10%
    jitter** (`llm_retry.py:27-30,124-137`).
  - `SessionManager._create_completion_with_retry` (`session.py:1517-1608`):
    iterates a fallback chain `[primary] + fallbackModels`; `retries_for_model =
    DEFAULT_MAX_RETRIES` (5) when only one model, else `min(2, 5)`. Retries
    `asyncio.sleep(retry_delay_ms/1000)`; empty responses retried; successful
    fallback annotated with `_fallback_info`. Interrupted sessions raise
    `asyncio.CancelledError` (`session.py:1569-1570`).
- **HTTP client** (`network/client.py`): `requests.Session` + `HTTPAdapter`
  (`pool_connections=20`, `pool_maxsize=20`, `client.py:47-48`), urllib3 `Retry`
  (`total=3`, `backoff_factor=0.5`, `status_forcelist=[429,500,502,503,504]`,
  `client.py:57-62`), SSRF/domain policy, redirect loop capped at 10, async via
  `asyncio.to_thread` (`client.py:238-274`).

### D.3 Tool registry (`tools/registry.py`)

- `ToolRegistry` with scoped `ToolLayer`s (`registry.py:71-124`), global layer +
  per-session masks/restrictions/guards (`registry.py:126-330`).
- Builtin registration with per-tool `is_concurrency_safe` (bool or callable) and
  `execution_mode` (`parallel|serial|barrier`) flags (§D.1).
- Orchestration tools registered: `Task` (`registry.py:940-985`), `subagent`
  (`registry.py:986-1031`), `subagent_fork` (`registry.py:1032-1077`), `job_list/
  job_output/job_kill` (`registry.py:527-586`), `ralph` (`registry.py:1440-1473`),
  `workflow` (`registry.py:1474-1489`), `goal` (`registry.py:1490-1512`), and the
  six teams tools (`registry.py:1514-1617`).

### D.4 Background jobs (`core/jobs.py`)

- `JobStatus = running|stopping|completed|killed|failed` (`jobs.py:13`);
  `DEFAULT_WAIT_TIMEOUT_MS=30000`, `MAX_WAIT_TIMEOUT_MS=600000`,
  `_MAX_JOBS_PER_SESSION=100` (`jobs.py:14-16`).
- `JobStore` (`jobs.py:50-196`): thread-safe (`threading.Lock`) in-memory registry;
  `start/complete/kill/kill_all/get/list/read_output`. `read_output` is
  **incremental** via `read_offset` (`jobs.py:169-186`). `kill` uses
  `kill_process_tree` (`jobs.py:134-136`). `_evict_locked` evicts finished jobs
  over the 100/session cap (`jobs.py:188-196`). `atexit` kills all jobs
  (`jobs.py:218`).
- Model tools (`tools/jobs.py`): `job_list`, `job_output` (wait loop polls
  `asyncio.sleep(0.05)` up to `timeout_ms` clamped to 600000; `tools/jobs.py:48-66`),
  `job_kill`.
- These jobs are **only** for bash background processes (per module docstring);
  subagents use the separate `AgentRegistry` path. Job metadata is not persisted.

---

## E. CLI / Env Contract

### E.1 CLI flags (`cli/app.py:125-240`)

- `prompt` (positional, nargs="*"), `--prompt/-p`.
- `--exec/-x/-e` non-interactive single prompt.
- `--resume/-r/--session/-s [ID]`, `--fork/-f [ID]`, `--last/-l` (session
  resume/fork).
- `--preset` ∈ `{full, core, shell_edit}` (tool preset; new prompt runs default to
  `core`, `app.py:2144-2147`).
- `--model/-m`, `--plan`, `--yes/-y` (auto-approve permissions), `--verbose/-v`,
  `--version`.
- Setup-related: `--setup`, `--provider`, `--key`, `--base-url`, `--setup-model`,
  `--test`, `--status`, `--project`, `--global`.
- **No CLI flags for orchestration budgets:** no `--parallelism`, `--max-iterations`,
  `--subagent-depth`, `--subagent-timeout`, `--token-budget`, `--max-concurrency`,
  `--max-rounds`. All such limits are hardcoded constants (§F).

### E.2 Slash commands (`cli/commands.py`)

- Orchestration-relevant slash commands: `/jobs` (`list|kill|logs`),
  `/agents` (`list|tree|report|send`), `/teams`, `/goal`
  (`list|add|done|cancel|start`), `/schedule`, `/mcp`, `/plan`, `/resume`,
  `/fork`, `/sessions`. (`commands.py:22-122`.)

### E.3 Settings & environment (`core/settings.py`)

- Layering: `~/.coderai/settings.json` (user) → `<root>/.coderai/settings.json`
  (project) → `CODERAI_*` process env; project wins over user, env wins over both
  (`settings.py:1-6`). `.env` (home `.coderai/.env` and project `.env`) loaded
  first (`settings.py:110-139`).
- Resolved orchestration-adjacent settings (`resolve_current_settings`,
  `settings.py:292-442`): `model`, `baseURL`, `apiKey`, `contextWindow`,
  `autoCompactWindow`, `temperature`, `thinkingEnabled`, `reasoningEffort`,
  `toolsPreset`, `mcpServers`, `permissions`, `enabledSkills`, `skillScanPaths`,
  `fallbackModels`, `statusline`.
- Env vars (prefix `CODERAI_`, `collect_env` at `settings.py:142-150`): `MODEL`,
  `BASE_URL`, `API_KEY`, `CONTEXT_WINDOW`, `AUTO_COMPACT_WINDOW`,
  `THINKING_ENABLED`, `TEMPERATURE`, `MULTIMODAL`, `REASONING_EFFORT`,
  `DEBUG_LOG_ENABLED`, `NOTIFY`, `WEB_SEARCH_TOOL`, `TOOLS_PRESET`,
  `PERMISSION_PRESET`, `FALLBACK_MODELS`, plus `OPENAI_API_KEY`/`OPENAI_BASE_URL`
  fallbacks (`settings.py:338-342`).
- **No `CODERAI_*` env var governs subagent depth/timeout/iterations, parallelism,
  or round caps.**

### E.4 Interrupt / Ctrl+C handling

- Interactive REPL installs a custom SIGINT handler (`app.py:815-829`): if an
  `active_turn_task` is running, cancel it (`task.cancel()`); otherwise raise
  `KeyboardInterrupt`.
- `KeyboardInterrupt`/`asyncio.CancelledError` paths call
  `mgr.interrupt_session(session_id)` then print "Turn interrupted by user."
  (`app.py:894-903`, `app.py:2022-2034`).
- `SessionManager.interrupt_session` sets the `asyncio.Event` in
  `session_controllers`, kills live processes, clears file state, marks entry
  `interrupted` (`session.py:661-680`). `is_interrupted` checks the event or entry
  status (`session.py:682-687`).
- The agent loop checks `is_interrupted` at every iteration boundary, before/after
  the LLM call, and via `should_stop` hook for tool execution
  (`agent_loop.py:179,217,306,331,422`; `session.py:1403`).
- Subagents use their own `abort_event` and `CancelledError` mapping
  (`subagent.py:240-241,384-403`). `TaskSupervisor`/`JobStore` provide bulk kill.

### E.5 Telemetry aggregation

- `TelemetryCollector` (`telemetry.py:104-212`): in-memory spans + metric counters,
  `to_otel_span` conversion, `export_spans`. **Only wired into the hooks layer**
  (`hooks.py:298-301,454-457`) — hook execution spans and `hook_executions/
  hook_errors` counters. There is no OTEL exporter, and no span collection around
  the agent loop, subagents, or tools.
- The session event log defines `telemetry/span-start`, `telemetry/span-end`,
  `telemetry/metric` **log-only** event types (`events.py:52-55`) but nothing in
  the codebase emits them.
- Token/cost aggregation is via `SessionManager._accumulate_usage` /
  `_accumulate_usage_per_model` on each assistant message (`session.py`), surfaced
  in session entries (`activeTokens`, `usage`, `usagePerModel`) and the `/tokens`
  command.

---

## F. Defaults & Limits (consolidated)

| Parameter | Default | Source |
|---|---|---|
| Max subagent depth | 3 | `spawn.py:18` (`DEFAULT_MAX_SUBAGENT_DEPTH`) |
| Subagent timeout | 90.0s | `spawn.py:19`; tool default `tools/subagent.py:36` |
| Subagent max iterations | 20 | `spawn.py:20` (`DEFAULT_SUBAGENT_MAX_ITERATIONS`) |
| Parallel subagent concurrency | 4 | `subagent.py:432` (uncalled) |
| Tool execution concurrency | 8 | `executor.py:46` (`DEFAULT_TOOL_CONCURRENCY`) |
| Session max iterations | 80,000 | `session.py:81` (`MAX_ITERATIONS`) |
| Session store max entries | 50 | `session.py:82` (`MAX_SESSION_ENTRIES`) |
| Tool result inline cap (derive) | 32,000 chars | `events.py:465` (`MAX_TOOL_RESULT_CHARS`) |
| Spill inline cap | 30,000 bytes | `spill.py:20` (`DEFAULT_MAX_INLINE_BYTES`) |
| Session references | 3 refs × 4096 bytes | `session_reference.py:22-23` |
| LLM retries | 5 (2 with fallbacks) | `llm_retry.py:27`; `session.py:1564-1566` |
| LLM backoff | 500ms→10s, ±10% jitter | `llm_retry.py:28-30` |
| HTTP retries | 3, backoff 0.5, 429/5xx | `network/client.py:21,57-62` |
| HTTP pool | 20 conns / 20 maxsize | `network/client.py:47-48` |
| Mailbox capacity | 1000 | `teams/mailbox.py:15` |
| Jobs per session | 100 | `jobs.py:16` |
| Job wait timeout | 30s default / 600s max | `jobs.py:14-15` |
| Ralph max rounds | 5 | `tools/ralph.py:23` |
| Ralph per-round timeout | 90s | `tools/ralph.py:24` |
| Goal max rounds | 20 | `goals.py:18` |
| Goal store default root | `.coderai/goals` | `goals.py:76` |
| CAS retries (teams) | 5, 0.05→1.0s, factor 2 | `teams/concurrency.py:52-55` |
| wait_agent poll interval | 0.5s | `teams/manager.py:310` |
| wait_agent default timeout | 60s | `teams/manager.py:208` |
| TaskSupervisor liveness reap | 60s idle | `agents.py:278` (not scheduled) |
| Schedule min interval | 300s | `schedule.py:14` |

---

## G. Gaps, Stubs, and TODOs vs. a mature stack

1. **Teams spawn is a no-op executor.** `TeamManager.spawn_teammate`
   (`teams/manager.py:134-154`) creates a record + mailbox but never launches an
   agent; no link to `SubAgentManager`/`AgentRegistry`; `_active_tasks` unused;
   `Teammate.status` never leaves `idle`. Team tools are board/bookkeeping only.
2. **No wired parallel fan-out tool.** `run_parallel_subagents`
   (`subagent.py:429-462`) is dead code; the only model-facing fan-out is
   workflow's `parallel`/`pipeline` (unbounded without explicit `max_concurrency`).
3. **Tool schema ↔ handler mismatches** in `registry.py`:
   - `workflow` schema exposes `workflow/action/workflow_id`
     (`registry.py:1478-1482`) but `handle_workflow_tool` reads `script/meta/args`
     (`workflow/tool.py:14-26`).
   - `goal` schema requires `title`+`description` with `milestones`
     (`registry.py:1493-1506`) but `handle_goal_tool` reads `action/subcommand/
     objective/goal_id/…` (`goals.py:224-235`).
   - `spawn_teammate` schema requires `prompt` (`registry.py:1524`) but the handler
     only requires `name`+`role` (`teams/tools.py:19`); `team_task_create` schema
     omits `priority`/`dependencies` that the handler accepts.
4. **Depth quota is model-driven, not enforced by lineage.** The one-shot `Task`
   and `subagent_fork` paths take `depth` from model args (default 0) and call
   `spawn_subagent` directly; only `spawn_background_agent` auto-increments depth
   from the parent handle (`agents.py:318-333`). A nested one-shot child can
   restart at depth 0. `check_subagent_depth_quota` is `>=` so it permits up to
   `max_depth` levels but relies on callers passing correct depth.
5. **Rate limiting is mostly inert.** No builtin sets `ToolDefinition.rate_limit`;
   `SlidingWindowRateLimiter` never fires. `rate_limited_id` is a marker; only
   `UnderstandImage` triggers the plugin hook; `WebSearch`/`WebFetch` are
   unthrottled in-process.
6. **Goals are a passive store.** `goals.py` has no automatic round loop wired into
   `agent_loop`; `advance_round` exists but is only callable by the model. There is
   **no `blocked` action / blocked-min-rounds rule** (unlike the DeepSeek-harness
   runtime this mirrors); `status="failed"` is the only terminal escalation when
   `round > max_rounds` (`goals.py:180-182`).
7. **Workflow "sandbox" is not isolated.** `execute_workflow_script` runs
   `exec()` in-process with a curated `__builtins__` (`engine.py:433-492`) but no
   OS/process sandbox, no memory/CPU/time limits, and no timeout around the script
   itself; the docstring's "isolated, asynchronous environment" overstates it.
   `WorkflowErrorCode.AGENT_CAP`/`ITEM_CAP` are declared (`engine.py:34-35`) but
   **no agent/item cap is enforced**.
8. **Background subagent tasks are unbounded.** `spawn_background_agent` uses
   `asyncio.create_task` with no concurrency semaphore, task registry cap, or
   `TaskGroup`; `AgentRegistry` is in-memory only (no persistence across restarts).
9. **Cancellation is fragmented.** `_active_controllers` is per-`SubAgentManager`
   instance (one-shot `Task` creates a new instance), so manager-level
   `cancel_all` cannot reach all one-shot children; only the `AgentRegistry`
   (continuable) path and the session controller event provide global control.
10. **Deadlock watchdog unused.** `InterAgentWaitWatchdog` is defined but not
    invoked by `wait_agent`; circular waits are only detected at task-DAG creation,
    not at runtime wait time.
11. **Mailbox is write-only from the agent's perspective.** Team `send_message`
    enqueues to `AsyncMailbox`, but the subagent loop drains only
    `AgentHandle.inbox`/registry inbox (`subagent.py:564-585`); the priority queue
    is never consumed.
12. **Telemetry is thin.** `TelemetryCollector` spans only wrap hook execution;
    no OTEL export and the `telemetry/*` log-only event types are never emitted;
    no per-subagent span aggregation.
13. **No orchestration CLI/env knobs.** Parallelism, budgets, depth, round caps,
    and timeouts are all hardcoded constants, not configurable via `--flags` or
    `CODERAI_*` env (see §F).
14. **Subagent LLM calls are non-streaming and single-shot** (`subagent.py:609`,
    `_call_llm_sync` `subagent.py:916-974`), so no live progress from a child
    reaches the parent except the final `SubAgentResult`.

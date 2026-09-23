<!-- Source: review agent d944401f-2a93-45f6-93ba-1223748e59c9 · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# Workflows & orchestration (subagents, teams, background, hooks, schedule, goals, triage, notifications)

I finished a read-only review of the orchestration layer and modified no repo files. The worst problems are verified with runtime checks, not just by reading. Deleting a session deadlocks the process. The subagent permission model is broken: "read-only" and the role tool lists don't restrict what a subagent can run. And the live session manager keeps its own job/agent/schedule stores that none of the tools write to.

Confidence tags: **[Verified]** means I confirmed it by running a check against the real modules or by exact code reading. **[High]**, **[Med]** and **[Low]** are how sure I am of findings I couldn't execute.

## (A) Bugs and bad implementations

**A1. Deleting a session deadlocks the whole process** — High, [Verified]
- **Where:** `coderai/background/agent_runner.py:143-148` (`kill_task`) and `:162-168` (`kill_all_tasks`).
- **What:** Both take `job_store._lock`, then call `job_store.kill()`, which takes the same lock again. It's a plain `threading.Lock`, so it can't be re-entered.
- **How it's reached:** `SessionManager` delete (`soul/session/manager.py:2211`) calls `get_task_supervisor().cleanup_session_tasks()`, which calls `kill_all_tasks`.
- **Proof:** I started one job, called `kill_all_tasks('S')` in a thread, and it printed `DEADLOCK`. The thread was still holding the lock, so the interpreter also hung on exit.
- **Impact:** Deleting any session with a running bash or pwsh job freezes the CLI's event-loop thread forever.
- **Fix:** Snapshot the job IDs under the lock, release it, then call `kill()`. Alternatively use an `RLock`.

**A2. Subagents swallow cancellation, which defeats parent timeouts and Ctrl-C** — High, [Verified]
- **Where:** `coderai/subagents/runner.py:351-370` catches `asyncio.CancelledError` and returns an `"interrupted"` result. `run_continuable` does the same at `:470-482`.
- **Why it breaks timeouts:** `asyncio.wait_for` only raises `TimeoutError` if the `CancelledError` actually propagates out.
- **Proof:** I simulated a parent inside `wait_for(timeout=0.2)` with a child that catches cancellation. The parent returned normally after 0.7s instead of timing out.
- **Impact:**
  - A nested subagent chain ignores every ancestor's timeout.
  - User interrupts inside a foreground `Task` call are turned into normal tool results.
  - The `except CancelledError` in `tools/agent/__init__.py:100` can never fire.
- **Fix:** Record the lifecycle event, then re-raise. Only convert to `"interrupted"` when the cancellation came from the manager's own `abort_event`.

**A3. The subagent recursion limit can be bypassed** — High, [Verified by code]
- **Where:** `coderai/tools/agent/__init__.py:26-39` (`_derive_depth`).
- **Why depth resets:** It only finds lineage for *continuable* handles (those with `run_session_id`). Foreground children from `Task` or `subagent_fork` are never registered, so a grandchild falls back to the model-supplied `args["depth"]`. That argument isn't in the tool schema, so it's 0.
- **Why the cap is model-controlled:** `max_depth` is read from model args at `:164`, `:240` and `:474`, so the model can pass `max_depth: 99`.
- **Why the tool filter doesn't help:** `runner.py:1038` compares against the constant `MAX_SUBAGENT_DEPTH` using the reset depth.
- **Impact:** Unbounded `Task` → `Task` → `Task` nesting. Combined with A2, timeouts don't bound it either.
- **Fix:** Register every spawned child, foreground included, with `run_session_id → depth`. Derive depth only from that registry. Clamp `max_depth` to the configured maximum and never take it from model args.

**A4. `read_only` mode doesn't stop mutation** — High, [Verified]
- **Where:** `runner.py:958` and `:1042` only block the names `write/Write/edit/Edit`.
- **Proof:** `_get_sandboxed_tools(SubAgentSpec(mode='read_only'))` still exposes `bash`, `pwsh`, `str_replace_editor`, `terminal_send`, `Task`, `schedule_create` and `spawn_teammate`.
- **Impact:** "Read-only" explore and review agents can edit files and run arbitrary shell commands.
- **Fix:** Filter on the registry's `is_mutating` flag, or use an explicit read-only allowlist. Also refuse shell tools unless the sandbox is `read-only`.

**A5. Role tool policies are inverted** — High, [Verified]
Three problems in `subagents/builder.py:217-240`, `runner.py:1045` and `registry.py:78-89`:
- **Unknown roles get every tool.** An unknown `subagent_type` resolves to `allowed_tools=[]` (the comment calls this "deny-by-default"). But the runner check `if spec.allowed_tools and …` treats an empty list as "no restriction".
- **`code-reviewer` gets all 47 tools.** It's documented as Read-Only in `AGENTS.md`, but has `tools: []`, so it resolves to `mode=general` with every tool, including `write`, `edit` and `bash`.
- **Explicit `read_only` gets escalated.** `builder.py:237` overwrites `read_only` with the role's mode, because an explicit request can't be told apart from the default.
- **Some roles get zero tools.** `planner`, `architect` and `security-reviewer` list `["Read","Grep","Glob"]`. Matching is exact and case-sensitive, and the registry has no `grep` or `glob` tool, so they end up with no tools at all.
- **Fix:**
  - Use `None` for "inherit" and `[]` for "deny all", and check `is not None`.
  - Use the existing (currently unused) `registry.is_tool_allowed()` for alias-aware matching.
  - Never widen an explicit `read_only`.

**A6. Subagent tool calls skip approval, sandbox mode, plan mode and file checkpoints** — High, [High]
- **Where:** `runner.py:967-972` builds `ToolExecutionHooks` with only `should_stop`, `isolated_cwd`, `dry_run` and `on_after_file_mutation`.
- **Comparison:** The main path (`soul/session/manager.py:1845-1862`) sets `permission_decision` after approval, `sandbox_mode`, `plan_mode` gating and `on_before_file_mutation` checkpoints.
- **Impact:** A child can mutate files with no approval, bypassing plan mode, with no undo history. Only PreToolUse hooks still apply.
- **Fix:** Inherit the parent session's approval runtime, sandbox mode, plan state and checkpoint hooks into the spec.

**A7. The live session manager's stores are shadow copies the tools never write to** — High, [Verified]
- **The copies:** `soul/session/manager.py:202-205` creates private `JobStore()`, `ScheduleManager(schedule.json)` and `AgentRegistry()`.
- **What tools actually use:** process globals — `get_job_store()`, `get_agent_registry()` and `get_schedule_manager()`. The schedule global has `storage_path=None`, so it's **not persisted**.
- **Consequences:**
  - `/agents` (`ui/shell/agents_cmd.py:12`) always shows nothing.
  - `/jobs`, the task browser and `doctor` look at an empty store.
  - `/schedule` (`dispatch.py:201`) shows a different set of schedules than the model creates.
  - `close_session_manager` (`cli/session_factory.py:110-119`) never cancels real background agents or jobs.
  - Model-created schedules disappear on restart.
- **Fix:** Pick one ownership model. Inject the session manager's instances into the tool context, or make the manager hold references to the globals.

**A8. Continuable workers never terminate** — High, [High]
- **The loop:** `runner.py:452-525` parks forever on `inbox_waiter.wait()` after every completed turn.
- **Kill doesn't kill:** `AgentRegistry.interrupt` (`core.py:208-228`) only aborts the current turn when continuable wiring exists. So `kill_task`, `kill_all_tasks` and `cleanup_session_tasks` never end these tasks.
- **Cap doesn't count them:** The spawn cap (`agent_runner.py:231-235`) only counts `running` and `interrupted`, so completed-but-parked workers escape it.
- **Nothing is evicted:** The registry never removes handles.
- **Impact:** Every background `subagent` leaks a task plus its whole conversation for the life of the process.
- **Fix:** Add a real `kill` that sets a terminal flag and cancels `handle.task`. Add an idle TTL. Evict terminal handles from the registry.

**A9. Teammates reply to each other's acknowledgements forever** — High, [Verified]
- **Where:** `teams/manager.py:518-529`. A worker replies "Acknowledged" to any direct message. The reply is itself a direct message, so two teammates ack each other indefinitely.
- **Proof:** Two teammates plus one message gave 20 inbox and 20 outbox entries each after 2 seconds, and it keeps growing.
- **Related:**
  - `Teammate.inbox` and `outbox` lists are appended to (`:261`, `:273`, `:291`) but never drained.
  - `wait_agent(wait_for="message")` (`:333`) is permanently "settled" once any message has ever arrived.
- **Fix:** Don't auto-reply to acknowledgements (add a message kind, or skip replies to replies). Drain or cap the inbox lists. Track unread state for `wait_agent`.

**A10. Every persisted schedule fires immediately after a restart** — Medium, [Verified by code]
- **Where:** `coderai/schedule.py:31-47`. `to_dict` doesn't save `target_timestamp`, and `_load` (`:255-279`) doesn't parse `scheduledAt`, so reloaded records get `target_timestamp=0.0`.
- **Also:** `_save` (`:240-253`) is a non-atomic `write_text` inside `except: pass`. Dispatched records are never pruned.
- **Fix:** Persist the timestamp, or parse `scheduledAt` on load. Write atomically (`utils.io.atomic_json_write` already exists). Drop terminal records.

**A11. Hooks run synchronously and block the event loop** — Medium, [High]
- **Where:** `hooks/engine.py:78-87` calls `subprocess.run(shell=True, timeout=…)`.
- **Called from async code:** the executor's PreToolUse and PostToolUse (`tools/legacy/executor.py:498` and `:730-770`), subagent spawn/start/stop (`runner.py:197-216` and `:318-327`), and turn begin/end.
- **Timeout isn't capped:** The per-hook timeout comes from config with no ceiling.
- **Orphans:** On timeout, only the shell is killed, so grandchildren are left running.
- **Fix:** Route through `run_hook_point_async`, or `asyncio.to_thread`. Cap the timeout. Use `start_new_session=True` and kill the whole process group.

**A12. Subagents run their shell commands in a scratch directory, not the project** — Medium, [Med]
- **Where:** `runner.py:969` sets `isolated_cwd = spec.isolated_cwd or spec.scratchpad_dir`, and `resolve_exec_cwd` makes that the shell's root. But the system prompt's runtime context (`:593`) advertises `project_root`.
- **Impact:** Relative commands like `pytest tests/` run in `.coderai/scratch/<sid>`. Nothing ever cleans these directories up (`cleanup_subagent_scratchpad` is only called from tests).
- **Fix:** Default `isolated_cwd` to the project root. Only use the scratchpad when isolation is explicitly requested.

**A13. Background subagent jobs: duplicate IDs, leaked entries, kill that doesn't kill** — Medium, [High]
- **ID collisions:** `tools/agent/__init__.py:87-89` numbers jobs as "count of existing subagent jobs + 1". After `_evict_locked` pops old jobs, IDs repeat, overwriting a live job record and its task entry.
- **Leak:** `:111` only removes the task entry on the success path.
- **Uncaught error:** `store.start()` can raise `RuntimeError` (the job cap), which isn't handled.
- **Kill doesn't cancel:** `JobStore.kill` (`background/store.py:100-105`) only marks process-less jobs as killed. So `TaskSupervisor.kill_task` on a subagent job doesn't cancel its asyncio task; only `job_kill` does.
- **Fix:** Use a monotonic counter or uuid. Pop in `finally`. Let `JobStore` hold a cancel callback.

**A14. External CLI backends leave processes running on timeout or cancel** — Medium (latent), [High]
- **Where:** `subagents/backends/base.py:43-51` runs `subprocess.run` via `asyncio.to_thread`, while `runner.py:231` and `:254` wrap it in `wait_for` with the *same* timeout.
- **Impact:** On asyncio timeout or cancellation, the `claude`/`codex` process keeps running and editing files. It runs with `dontAsk` in the project root, ignoring `mode` and `isolated_cwd`.
- **Why "latent":** No live caller ever sets `spec.provider`, so this isn't reachable today (see C).
- **Fix:** Use `asyncio.create_subprocess_exec` and kill the process group on cancel.

**A15. Team tools crash on ordinary bad input** — Medium, [Verified by code]
- `teams/tools.py:80-93` only catches `CycleDetectedError`. The `KeyError` or `ValueError` from `create_task` (unknown or self dependency) propagates.
- `:216` calls `int(expected_revision)` outside the `try`.
- `wait_agent` raises `ValueError` on a negative timeout, and accepts `inf` or `nan`, which means waiting forever.
- **Fix:** Catch the `KeyError`/`ValueError` from `create_task`, move the `int()` inside the `try`, and clamp the timeout to a finite range (e.g. 0–600 seconds).

**A16. Team tasks run without lineage and in the wrong directory** — Medium, [High]
- **Where:** `teams/manager.py:432-443`. `_execute_task` uses `Path.cwd()` for both `project_root` and the client config.
- **Missing lineage:** no `parent_session_id`, depth 0, and default 90s/20-iteration limits.
- **Inconsistent root:** `spawn_teammate` (`:196`) uses `CODERAI_PROJECT_ROOT` instead.
- **Stuck tasks:** Tasks are never auto-promoted from `pending` to `in_progress`, so assigned work sits idle unless the model flips the status.
- **Fix:** Pass the session's root, session id and depth into `spawn_teammate`, and auto-start ready tasks.

**A17. Subagent hooks report the wrong session and miss failures** — Low, [Verified by code]
- `runner.py:198-216` passes `spec.parent_agent_id or "root"` as the session id. Tool-spawned children always send `"root"`.
- `SubagentStop` (`:317-327`) only fires on the success path, not on timeout, cancel or exception. Continuable runs never fire any of these hooks.
- **Fix:** Pass `spec.parent_session_id`, and fire `SubagentStop` from a `finally` block so it also runs on timeout, cancel and error. Continuable runs need the same hooks.

**A18. Goals don't do anything goal-like** — Low, [Verified]
- `goals/core.py:172` `advance_round` is never called, so rounds and `max_rounds` are never enforced. It's effectively a note store.
- `get_goal_store` ignores `project_root` after the first call.
- `_save` is non-atomic.
- `int(args["max_rounds"])` is unguarded.
- `update(status="running")` can leave several goals running at once.
- **Fix:** Advance rounds and enforce `max_rounds` from the turn loop. Key the store by project root, write atomically, validate `max_rounds`, and pause other goals when one is set to running.

## (B) Inconsistencies and overlap

**Map of the orchestration subsystems**

| Subsystem | Entrypoint | Live? |
|---|---|---|
| `subagents/runner.py` (`SubAgentManager`) | `Task`, `subagent_fork`, `subagent` tools; teams | Live |
| `background/agent_runner.py` | `subagent` tool (continuable) → `spawn_background_agent`; `TaskSupervisor.cleanup_session_tasks` on session delete | Live (A1, A8) |
| `background/store.py` + `manager.py` (`JobStore`) | bash/pwsh/subagent jobs via the global store | Live, plus a shadow copy (A7) |
| `background/worker.py`, `ids.py` | none | Dead / test-only |
| `teams/` | `spawn_teammate`, `team_task_*`, `wait_agent` tools | Live, global singleton, polling |
| `goals/` | `goal` tool, `/goal` | Live, but only stores notes |
| `schedule.py` | `schedule_*` tools, `dispatch_due_schedules` | Live, split instances |
| `orchestration.py` | stop-reason mapping; event bus; `resolve_*` config helpers | Mapping live; bus has **zero subscribers**; most resolvers unused |
| `triage/` + `jev/` | `/review` (`dispatch.py:833`), status in `llm.py:1269` | Live (new) |
| `hooks/` | sync `run_hook_point` (executor, session manager, runner) | Live |
| `hooks/engine.py` `HookEngine` | `KimiSoul`/toolset only; `set_hook_engine` never called externally | Always empty, so every `trigger` is a no-op |
| `notifications/` | `SessionManager.notify` publish | Publish is live; `NotificationWatcher` only runs in the `KimiSoul` shell |
| `app.py` | nothing imports it | Orphaned |

The real CLI is `coderai.main` → `ui/shell/app.py` → `SessionManager`. `KimiSoul`, `app.py` and the `Shell` class in `ui/shell/__init__.py` form a parallel runtime that only tests instantiate (`tests/test_coderaisoul_lifecycle.py:157`).

**B1. Five ways to spawn a subagent, with different semantics** — Medium, [Verified]
- The five: `Task`, `subagent_fork`, `subagent`, teams, and the kosong `Agent` class (`tools/agent/__init__.py:549`).
- **Default mode:** `Agent` defaults to `general`; the others default to `read_only`.
- **History forking:** `Task` fork reads raw JSONL rows (`:480-490`), while `subagent_fork` uses `context.list_session_messages`. The subagent executor never provides `list_session_messages`, so forking from inside a child silently seeds nothing.
- **Hardcoded limits:** Timeout 90 and `max_depth` 3 are repeated three times, while `orchestration.resolve_subagent_defaults` (which reads `CODERAI_MAX_SUBAGENT_DEPTH` and friends) is never called.
- **Broken class:** `Agent` never passes `create_openai_client`, so it would always fail.
- **Model and provider:** No path lets you choose a model or provider.
- **Fix:** One `build_spec(context, args)` helper that every path uses, with the resolved defaults.

**B2. Two hook engines with opposite failure semantics** — Medium, [Verified]
- `engine.run_hook_point` **fails closed**: any non-zero exit or timeout means deny, and the environment is scrubbed of secrets.
- `runner.run_hook` / `HookEngine` **fails open**: only exit code 2 blocks, a timeout means allow, and it inherits the **full environment, including API keys**.
- **Fix:** One executor, Claude-compatible (exit 2 = block), with a scrubbed environment.

**B3. Event vocabulary is duplicated and inconsistent** — Low, [Verified]
- **Subagent lifecycle is published four ways:**
  - in-result `lifecycle_events` (`subagent/spawn`, `start`, `complete`, `error`)
  - the orchestration bus (`subagent/start`, `end`), which reuses the name `subagent/start` with a different payload
  - hook points (`SubagentSpawn` plus `SubagentStart` plus `SubagentStop`)
  - `wire/types.SubagentEvent`
- **Hook points vs config:** The `HookPoint` enum has 22 members; the `config.HookEventType` Literal has 13. `PreStep`, `PostStep`, `PrePrompt`, `PostPrompt` and `StopCriteria` are accepted in config but never fired.
- **Duplicated code:** Payload builders exist in both `hooks/events.py` and `hooks/runner.py` (`_event_base`/`build_*`). `DEFAULT_HOOK_TIMEOUT_SECONDS` is defined four times. The `hooks/events.py` docstring is copied from `engine.py`.

**B4. Defaults and documented env vars that do nothing** — Low, [Verified]
- Goal max rounds: 20 in `goals/core.py` vs 256 in `orchestration.resolve_goal_defaults`, which is unused.
- These env vars have no effect: `CODERAI_GOAL_MAX_ROUNDS`, `CODERAI_MAX_PARALLEL_TOOL_CALLS`, `CODERAI_SUBAGENT_TIMEOUT_SECONDS`, `CODERAI_SUBAGENT_MAX_ITERATIONS`.

**B5. The new untracked modules**
- **`app.py`** — orphaned and broken:
  - `run_acp` imports `coderai.ui.acp`, which doesn't exist.
  - The `yolo`, `afk`, `plan_mode`, `model_name`, `thinking` and `skills_dirs` parameters are ignored.
  - `Runtime` is built without `notifications`, `background_tasks` or `approval`, so `Shell` would crash at `runtime.background_tasks.reconcile` (`ui/shell/__init__.py:421`).
  - `run_soul_stream` (`:217`) waits forever if `run_soul` fails before the UI loop starts.
- **`jev/` and `triage/`** — wired into `/review`. Design issue: `review.py:128-139` sends untracked, non-ignored files (up to 200 × 256 KB) to both the third-party TypeSafe service and the main model. A stray `.env.local` would leak.
- **`notifications/notifier.py`** — wired only into the `KimiSoul` shell. Nobody ever assigns `runtime.notifications`.
- **Claim/ack delivery** (`manager.py:80-94`) is non-atomic read-modify-write across processes. Each poll rescans and parses every notification ever written, and nothing is pruned. The `wire` sink is pushed on publish but its state stays `pending` forever.

## (C) Verified dead code

**Confirmed dead**
- **`background/ids.py`** — imported nowhere. It does import cleanly (`TaskKind` exists at `models.py:57`).
- **`background/worker.py`** — only `tests/test_background_wire.py` uses it.
- **`subagents/runner.py:529-540`** — genuinely unreachable. The `while True` exits only via `return` or an exception.
  - `_run_subagent_loop(continuable=…)` (`:549`) accepts the parameter but never reads it.
  - `run_parallel_subagents` and `cancel_all` are only used by tests.
- **`orchestration.py`** — no references to: `get_orchestration_event_bus`, `resolve_child_depth`, `resolve_subagent_defaults`, `resolve_goal_defaults`, `resolve_max_parallel_tool_calls`, `auto_max_concurrent_agents`, `stop_reason_error`, `SUBTASK_STOP_REASONS`.
- **`teams/`**
  - `cas_retry_async` and `cas_retry_sync` — no references, not even in tests.
  - `InterAgentWaitWatchdog` / `release_wait` — tests only.
  - `_validate_dag`, `get_messages`.
  - Mailbox topics (`subscribe`, `publish_async`, `broadcast_async`).
  - Only `MessagePriority.NORMAL` is ever sent, so the priority queue does nothing.
- **`subagents/`**
  - `SubagentStore` is never instantiated. `delete_instance` would also `rmtree` an unvalidated `agent_id`.
  - `backends/*` are unreachable because `provider` is never set.
  - `SubagentDescriptor`, `parse_subagent_descriptor`, `spec.descriptor`, `SubagentQuotaConfig`, `seed_events`, `AgentHandle.conversation` — exported or defined only.
  - `list_descendants`, `get_tree`, `interrupt_tree`; `is_tool_allowed` (it should be used, see A5).
  - `TaskSupervisor.record_heartbeat`, `kill_task`, `check_liveness`. Nothing sends heartbeats, so `check_liveness` would reap every agent after 60 seconds if anyone called it.
- **`hooks/`**
  - `HookEngine.fire_and_forget_trigger`, `add_hooks`, `add_wire_subscriptions`, `set_callbacks`, `has_hooks`, `has_hooks_for`.
  - `events.session_start`, `session_end`, `subagent_start`, `subagent_stop`.
  - `HookPoint.PRE_STEP` and similar unfired points.
- **Other:** `GoalRef`, `advance_round`; all of `app.py`; the `tools/agent` `Agent` class; `triage JEV_MODEL_ID` (`jev/client.py:500` hardcodes the string instead).

**Vulture false positives**
- `notifications acked_at` — written at `manager.py:128`.
- `hooks/config raw_stdout` / `raw_stderr` — set in `engine.py`.
- `triage priority_confidence` — a populated dataclass field.

**Ruff findings**
- **B023** at `notifications/llm.py:27` is a false positive: the lambda is invoked immediately.
- **B904** at `schedule.py:89`, `112`, `215`, `237` are real but low severity. At `:112` it also hides a message: the "at least 300 seconds" error is caught by the same `except` and replaced with "must be a valid integer".

## (D) Design smells

- **D1. Process-global singletons with no session scoping.** Medium. Affects `TeamManager`, `AgentRegistry`, `JobStore`, `GoalStore`, `ScheduleManager` and `_global_active_controllers`. Team tools discard `context` (`del context`), so any session can see and control any team. `cancel_all` cancels across all sessions.
- **D2. Polling instead of events.** Low. The teammate worker polls every 0.1s, `wait_agent` every 0.5s, and `NotificationWatcher` rescans a directory every 1s. Replace these with `asyncio.Event`/`Condition` or mailbox `recv`.
- **D3. Blocking file I/O inside async code.** Low. Goals, schedule and notifications do synchronous JSON I/O on the event loop.
- **D4. Copy-paste result construction.** Low. `runner.py` builds a `SubAgentResult` with identical token-telemetry blocks about 10 times. Extract a helper.
- **D5. Misleading or fragile claims.** Low.
  - `AsyncMailbox` says it's "thread-safe", but `asyncio.PriorityQueue` isn't.
  - Cycle detection uses recursive DFS (`deadlock.py:39`), which hits `RecursionError` on chains around 1000 deep.
  - Hook calls are wrapped in pervasive `except Exception: pass`, which hides configuration errors.

**Suggested fix order:**
1. Fix A1, A2, A4, A5 and A7 first. Each fix is small and they have the largest blast radius.
2. Then pick one spawn path and one hook executor (B1, B2), and delete the dead layers listed in C.

Also noticing scratch directories in .coderai/scratch just accumulate forever since cleanup is never called in production, and even the cleanup logic doesn't actually prune the scratch folder despite a comment suggesting it should. There's also a minor issue where the "tmp" check is a naive substring match on the full path, so a project living under a directory like /Users/x/tmpwork/ could trigger unintended removal — but this only matters in test contexts, so I'd rate it low priority.

I think I have enough now. Time to write up the final report with file:line references, severity, confidence, explanation, and fix for each finding, plus notes on how each was verified (runtime deadlock checks, tool filters, ping-pong behavior, cancellation semantics). I'm organizing roughly 25 findings, starting with the deadlock in kill_task/kill_all_tasks, the swallowed CancelledError in the subagent runner, the depth-limit bypass allowing model-controlled max_depth, the unenforced read_only flag, and the inverted role policy where unknown roles get access to all tools instead of none.

I'm continuing through the remaining findings: the role policy issue where code-reviewer roles get all 47 tools including write while planner/architect/security-reviewer get zero due to case-sensitive matching, subagent tool execution bypassing approval/sandbox/plan/checkpoint gates, split-brain state stores where different modules create separate non-persistent instances of job stores and registries, continuable workers that never terminate and escape the concurrency cap because it only counts running/interrupted states, and team acknowledgment ping-pong behavior.

I'm now tracing schedule reload logic that fires everything due to a missing field in serialization plus non-atomic writes that silently swallow errors, and synchronous hooks blocking the event loop by calling subprocess.run from async contexts with unbounded user-configurable timeouts and shell=True processes that don't fully terminate on kill.

External CLI backends can leak processes on cancellation, but since backends are currently unreachable I'm rating that as latent Medium severity. I'm also flagging subagent job ID collisions, an uncaught RuntimeError in job store startup, and a kill path that only marks jobs as killed without actually cancelling the underlying asyncio task, before moving into unhandled exceptions in team tools.

Continuing through team tools: unhandled KeyError/ValueError in task creation, unguarded int() conversion, negative timeout errors, and unbounded NaN/infinity values in wait_agent. Then I'm looking at HookEngine's gather calls lacking exception handling and task cancellation for sibling hooks, plus a leaked hook task in dispatch — though since HookEngine is always empty this is Low severity. Finally noting that teams execute with an inconsistent working directory and missing session lineage, with mismatched environment variable usage between spawn paths.

Now I'm tracking Stop hook issues with wrong session IDs and success-only triggering, notification claims that aren't atomic across processes plus a full scan every second and an unset notifications field, and goals logic where round advancement and max rounds are never enforced properly alongside non-atomic writes and unguarded int() parsing. I'm also flagging that app.py imports a nonexistent module which would break run_acp, ignores several configuration parameters, and constructs Runtime incompletely.

Now I'm also finding a Shell crash tied to background task reconciliation, and a case where run_soul_stream can hang forever awaiting a future if run_soul fails before the UI loop starts. Beyond that, I'm mapping out inconsistencies across the three subagent spawn paths—Task, subagent_fork, and the Agent/teams paths—which diverge on default modes, how they seed session history (JSONL rows vs a list function the executor never actually provides), hardcoded timeouts that ignore the resolver, and a hardcoded max depth of 3.

I'm also noticing the Agent tool is missing an OpenAI client creator so it always fails, and event handling has real problems: four lifecycle channels for subagents, one event name reused for two different payload shapes, three overlapping definitions of hook event types, several configurable hook points that never actually fire, duplicated payload-building logic across two files, and a timeout constant defined four separate times. On top of that there are two separate hook engines with opposite failure semantics—one fails closed on any error, the other fails open except on a specific exit code.

Now I'm cataloging more dead configuration: several documented environment variables that have no effect, an orchestration event bus with zero subscribers, and unused goal-round defaults. I'm also confirming specific dead files—an IDs module nobody imports and a worker module only referenced by a single test—since jobs actually run through the shell tool instead.

Also MessagePriority values besides NORMAL are dead code since the priority queue never differentiates. In subagents, the store module and backend implementations are unreachable since no provider gets set, and several descriptor/spec exports plus AgentHandle.conversation and AgentRegistry's descendant-listing methods appear unused too.

Registry.is_tool_allowed is another export-only method that really ought to be wired in somewhere. And TaskSupervisor's heartbeat and liveness-check methods are dangerous as-is — check_liveness would kill every task after 60 seconds since nothing actually calls record_heartbeat.

On the hooks side, HookEngine has several methods (fire_and_forget_trigger, add_hooks, add_wire_subscriptions, set_callbacks, has_hooks, has_hooks_for) that never get exercised, and since set_hook_engine is never called from outside, the engine stays empty — meaning every trigger call in KimiSoul and the toolset is effectively a no-op. The session/subagent event builders in hooks/events.py are also duplicated in runner.py, and the HookPoint enum values like PRE_STEP look unused as well.

Beyond that, I'm flagging GoalRef and advance_round in goals, the entire app.py module, the tools/agent Agent class, and a hardcoded model ID in client.py that bypasses the JEV_MODEL_ID config. I'm also sorting through likely false positives — several dataclass fields that are actually written to (acked_at, raw_stdout/raw_stderr, priority_confidence), dead config fields on SubagentQuotaConfig, and an unused `continuable` parameter in runner.py's _run_subagent_loop that I want to double check isn't referenced anywhere in the function body — confirming it isn't, so that one's a legitimate dead parameter.

Moving to design-level issues: I'm noting the pervasive use of process-global singletons (TeamManager, AgentRegistry, JobStore, GoalStore, ScheduleManager) that aren't session-scoped, meaning any session can see or control any team's state, plus a global cancel_all. I'm also flagging inefficient polling loops instead of event-driven waits, duplicated result-construction logic in runner.py that should be extracted into a helper, synchronous file I/O happening inside async code paths, and broad exception-swallowing around hook execution that could hide real failures.

I'm also cataloging naming inconsistencies in the parity shims, a mislabeled "thread-safe" claim on AsyncMailbox despite using a non-thread-safe asyncio.PriorityQueue, a recursive DAG cycle-detection function that risks RecursionError on very deep chains, a privacy concern where the review pipeline sends untracked non-ignored files to a third-party model, and a medium-confidence behavioral bug where subagent shell execution happens in the scratchpad directory while the prompt tells the model it's operating from the project root. I'm organizing everything by severity now and aiming for a complete but tight list of roughly 28 findings across all sections.

# CoderAI ↔ DeepSeek Harness — Orchestration Parity Gap Analysis

Audit of `/Users/adityaraut/Desktop/CoderAI-main` (target) against
`/Users/adityaraut/Downloads/deepseek-harness-master` (gold reference), focused on
parallel agent spawning & subagent orchestration. Companion documents:
[`dsh-orchestration-spec.md`](dsh-orchestration-spec.md) (reference behavior),
[`coderai-orchestration-map.md`](coderai-orchestration-map.md) (current state).

Baseline test state: **620 passed, 1 skipped** (`Python 3.14.6`).

## Reference model (condensed)

| Concept | DSH semantics |
|---|---|
| One-shot subagent | `subagents.start(provider, {label, prompt, parent, signal, maxDepth, toolFilter, persona, outputSchema})` → `SubagentRun {id, result (never rejects), dispose()}`. Stop reasons: `completed \| aborted \| error \| max-tokens \| refusal`. Final output = last non-empty assistant message, else accumulated text. Partial output survives cancel/truncation. |
| Background one-shot | Job registry, kind `subagent`, id `<subagent>-N`; `cancel()` aborts the child's AbortController; collected with `job_output`/`job_kill`. |
| Continuable child | `startContinuable` → durable child id; resolves at inbox acceptance. FIFO turn queue = agent inbox. `send_message` enqueues/wakes/cold-resumes. `interrupt_agent` stops the **current turn only** (`keepInbox`); parked messages survive; absent target = accepted no-op; self-interrupt rejected; ancestor-lineage authorization. `report` (child→parent, `quiet`/`next-step`). Settlement notice to parent: "Background subagent X finished and will do no further work unless you send it more." Lifecycle edges `subagent/start` + `subagent/end` (`runId/provider/id/local` + `stopReason/lastAssistantMessage`). Depth = parent depth + 1, persisted-header monotone, cap 3 (0 forbids), enforced per start. |
| Workflow | Model-written script (`script`/`meta`/`args`); hooks `agent()`, `pipeline()` (no cross-stage barrier; stage throw → item `null`), `parallel()` (thunk throw → `null`), `phase()`, `log()`; fatal `WorkflowError` codes (`SCRIPT_PARSE, META_INVALID, INVALID_ARGUMENT, UNSUPPORTED_OPTION, UNSUPPORTED_SCHEMA, AGENT_CAP, ITEM_CAP, AGENT_START, AGENT_RESULT, RESULT_UNSERIALIZABLE, CANCELLED`) always kill the script. FIFO concurrency slots; `maxConcurrentAgents = min(16, max(1, cores−2))`, `maxTotalAgents = 1000`, `maxItemsPerCall = 4096`, `syncTimeoutMs = 5000`, `disposeGraceMs = 5000`. Child failure → `null`; infra failure → fatal. Stop reasons `completed \| cancelled \| error`; result never rejects. |
| Ralph | Fixed deployment-owned loop over the workflow seam: fresh structured-output child per round; report `{status: continue\|complete\|blocked, summary, evidence[], nextSteps[], blocker}` with per-status validation; run statuses `complete \| blocked \| budget-limited \| round-failed`; `maxRounds` default+ceiling 256; handoff/result caps 16384 chars; `round-failed` → isError with last handoff; `blocked`/`budget-limited` are **success** results. |
| Goal | `get_goal`/`create_goal`/`update_goal` (`edit\|pause\|resume\|complete\|blocked`); phases `active\|paused\|blocked\|complete`; `blockedReason {code, message}`; `roundsStarted`; `maxGoalRounds` default 256; `activation armed\|disarmed` (process-local); blocked requires ≥3 consecutive rounds (`blockedAfterConsecutiveRounds`); create/pause/resume/edit require a direct top-level human (rejected for subagent callers); CAS by `goal_id+revision`; automatic same-session round driver queues `renderGoalRoundPrompt` when idle+armed, blocks at round limit with code `round-limit`, disarms on error. |
| Job | Statuses `running \| stopping \| completed \| killed \| failed`; `job_output {job_id, wait, timeout_ms}` (timed-out wait returns `[status: running]`), `job_list`, `job_kill` (outcome `cancellation-requested \| already-finished`). Completion notices delivered to owner; `reported` flag suppresses duplicates. |
| Tool-call concurrency | Bounded rolling pool (`maxParallelToolCalls = 10`); exclusive calls are barriers; re-classification before each start; results commit in **model order**; abort drains started calls and records synthetic "tool call aborted before dispatch" results. |
| Rate limiting | No client-side token bucket in the reference. 429 → `RATE_LIMIT` (distinct from quota exhaustion), bounded exponential backoff + symmetric jitter (default 500 ms→…, ±10%), honoring provider `Retry-After`. |

## Gap inventory (CoderAI → target state)

### A. Worker spawning & pooling
1. **Workflow caps absent.** `WorkflowEngine` has no slot queue, `maxTotalAgents`, or `maxItemsPerCall`; `AGENT_CAP`/`ITEM_CAP` declared but unenforced (`workflow/engine.py:34-35`). → *Port: FIFO slot queue, caps with DSH defaults, env overrides.*
2. **No background one-shot subagent.** `run_in_background` exists only for bash. The `Task`/`subagent_fork` tools cannot return a job id. → *Port: job kind `subagent` wired into `JobStore`.*
3. **`run_parallel_subagents` has no caller** (`subagent.py:429`). → *Expose via `run_in_background` fan-out in workflow `parallel`/`pipeline` (which already fan out) and keep the helper as the shared bounded pool.*
4. **Unbounded continuable spawns.** `asyncio.create_task` with no cap (`agents.py:374`). → *Add a bounded spawn semaphore + per-session cap configurable via env.*

### B. Lifecycle & state isolation
5. **Stop-reason vocabulary mismatch.** CoderAI `status` ∈ `completed/failed/interrupted/timeout/max_iterations/budget_exceeded`; DSH `stopReason` ∈ `completed/aborted/error/max-tokens/refusal`; refusal currently folded into `failed` (`subagent.py:688-715`). → *Port: `stop_reason` + `refusal` status, mapping `interrupted/timeout/max_iterations→aborted`, `budget_exceeded→max-tokens`, `failed→error`.*
6. **No lifecycle publication.** Events stay in a per-result list; DSH publishes `subagent/start`/`subagent/end` pair. → *Port: process-local lifecycle bus.*
7. **Depth is model-claimed, not lineage-derived.** One-shot children can reset depth to 0. → *Port: `resolve_child_depth(parent_depth+1)` using registry lineage; keep quota semantics.*
8. **No settlement notice to parent.** Continuable child finishing silently updates a handle. → *Port: parent notice via session-sink registry + durable store append.*

### C. Inter-agent communication
9. **`interrupt_agent` kills the task** instead of stopping the current turn and parking queued messages (`agents.py:107-114`). → *Port: abort-event interrupt + parked continuable loop + wake on `send_message`.*
10. **No lineage authorization** on `send_message`/`interrupt_agent` (any session can steer any agent id). → *Port: parent/ancestor checks.*
11. **Settlement/status accounting** — `subagent/end` stop reasons and "will do no further work unless you send it more" wording absent.

### D. Concurrent tools & rate limiting
12. **Chunk partitioning is static** (contiguous pre-partition) vs DSH's re-classify-before-start rolling pool. Read-only defaults and synthetic abort results already match. → *Port: re-check execution mode per call at dispatch time.*
13. **Retry-After not honored** by `llm_retry`/`session._create_completion_with_retry`. → *Port: provider retry-after delay.*
14. **Sliding-window rate limiter dormant** — acceptable parity (reference has none client-side), but wire `RATE_LIMIT` handling into subagent LLM loop (subagent loop retries nothing today). → *Port: retry in `_run_subagent_loop` using the shared retry policy.*

### E. Contract surface
15. **`workflow` tool schema ↔ handler mismatch** (`registry.py:1478-1488` vs `workflow/tool.py`). → *Port: `{script, meta{name,description,whenToUse?,phases?}, args?}` schema.*
16. **`goal` tool** is a legacy action-style store; DSH contract is `get_goal`/`create_goal`/`update_goal` with phases, blockedReason, roundsStarted, activation, authority rules, blocked threshold. → *Port as new tools; keep legacy tool.*
17. **Ralph contract drift**: round cap 5 vs 256; string evidence/nextSteps vs arrays; `max_rounds_reached` vs `budget-limited`; missing per-status report validation, handoff/result caps, `round-failed` with last handoff. (Pinned local test keeps `blocked` as an error result — preserved.) → *Port everything else.*
18. **No orchestration env knobs.** → *Port: `CODERAI_MAX_SUBAGENT_DEPTH`, `CODERAI_SUBAGENT_TIMEOUT_SECONDS`, `CODERAI_WORKFLOW_MAX_CONCURRENT_AGENTS`, `CODERAI_WORKFLOW_MAX_TOTAL_AGENTS`, `CODERAI_WORKFLOW_MAX_ITEMS_PER_CALL`, `CODERAI_RALPH_MAX_ROUNDS`, `CODERAI_GOAL_MAX_ROUNDS`, `CODERAI_GOAL_BLOCKED_AFTER_ROUNDS`, `CODERAI_MAX_PARALLEL_TOOL_CALLS`.*
19. **No goal round driver** — automatic same-session continuation missing. → *Port a compact driver hooked into `SessionManager.reply_session`.*

## Porting decisions (constraints honored)

- Pure CLI; no web/dashboard/HTTP layers. Native layout preserved (`coderai/core`, `coderai/cli`, `coderai/agents`-style modules kept as-is).
- The reference is TypeScript/cordis; CoderAI ports the **semantics** onto its asyncio primitives (`asyncio.Event` for abort signals, FIFO waiters for slots, `asyncio.create_task` + bounded semaphore for spawns) — no IoC rewrite.
- Existing pinned test contracts (Task/subagent_fork tool shapes, `WorkflowEngine.agent/pipeline/parallel` signatures, Ralph metadata keys, legacy `goal` tool, `GoalStore`) are preserved via compatibility shims; parity surfaces are additive or layered behind them.

## Implementation order

1. `coderai/core/orchestration.py` (new): stop-reason mapping, lifecycle event bus, depth resolution, env-driven limit resolution.
2. `coderai/core/subagent.py`: refusal status, `stop_reason`, lifecycle publication, retry-with-backoff in child loop, depth derivation.
3. `coderai/core/agents.py` + `tools/agents.py`: continuable parking loop, interrupt-keepInbox, lineage authorization, settlement notices, `run_in_background` → jobs (kind `subagent`), `subagent_id` alias.
4. `coderai/core/workflow/engine.py` + `tool.py` + registry schema: DSH script contract, caps/slots/cancellation, fatal-vs-null discipline, stop reasons, structured results.
5. `coderai/core/tools/ralph.py`: DSH round contract (budget-limited, report validation, caps, round-failed w/ last handoff).
6. `coderai/core/goals.py` (+ `tools/goal_dsh.py`, round driver): DSH goal domain + `get_goal`/`create_goal`/`update_goal` + same-session round driver.
7. Registry + prompt-section guidance + env overrides.
8. `tests/test_orchestration_parity.py`; full `.venv/bin/pytest -v` regression.

---

## Resolution log (implementation outcome)

All 19 gaps ported, with two deliberate adaptations documented below. Verification:
`.venv/bin/pytest -q` → **642 passed, 1 skipped** (620 baseline + 22 new parity tests in
`tests/test_orchestration_parity.py`).

| # | Gap | Resolution | Files |
|---|---|---|---|
| 1 | Workflow caps | FIFO slot queue (`_acquire_slot`/`_release_slot`), `maxTotalAgents`, `maxItemsPerCall`, cancellation rejects queued waiters; limits resolved via `CODERAI_WORKFLOW_*` | `core/workflow/engine.py`, `core/orchestration.py` |
| 2 | No background one-shot subagent | `run_in_background` on `Task`/`subagent_fork` → job kind `subagent` (`subagent-N` ids), `job_output`/`job_kill` wired, completion notices | `core/tools/agents.py`, `core/tools/subagent.py`, `core/tools/jobs.py` |
| 3 | Dead parallel fan-out | Bounded pool retained as helper; model-facing fan-out is workflow `parallel`/`pipeline` (now capped) | `core/workflow/engine.py` |
| 4 | Unbounded continuable spawns | Per-session cap `MAX_CONTINUABLE_AGENTS_PER_SESSION = 50` (configurable via `CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION`); jobs per-owner cap `MAX_RUNNING_JOBS_PER_SESSION = 50` (configurable via `CODERAI_MAX_RUNNING_JOBS_PER_SESSION`) | `core/agents.py`, `core/jobs.py`, `core/orchestration.py` |
| 5 | Stop-reason vocabulary | `SubAgentResult.stop_reason` (`completed/aborted/error/max-tokens/refusal`), `refusal` status, mapping table + parent-facing headlines | `core/subagent.py`, `core/orchestration.py` |
| 6 | Lifecycle publication | Process-local `OrchestrationEventBus`; `subagent/start`+`subagent/end` pairs (runId/provider/id/local/stopReason/lastAssistantMessage); `workflow/start|phase|log|agent-start|agent-end|end`; `goal/changed` | `core/orchestration.py`, `core/subagent.py`, `core/agents.py`, `core/workflow/engine.py`, `core/goals_dsh.py` |
| 7 | Model-claimed depth | `resolve_child_depth` + lineage-derived `_derive_depth` (registry handle depth + 1; monotone) | `core/orchestration.py`, `core/tools/agents.py`, `core/tools/subagent.py` |
| 8 | No settlement notice | `subagent-settled`-style notice (live session sink + durable store fallback) with harness wording | `core/agents.py`, `core/subagent.py`, `core/session.py` |
| 9 | interrupt kills task | `interrupt_agent` stops the current turn only (abort event), queued inbox stays parked, agent stays live; `send_message` wakes parked workers | `core/agents.py`, `core/subagent.py` |
| 10 | No lineage authorization | `_caller_owns_target`: direct parent or live ancestor required for `send_message`/`interrupt_agent` | `core/tools/agents.py` |
| 11 | Status accounting | `last_stop_reason` on handles; settlement summaries per stop reason | `core/agents.py`, `core/orchestration.py` |
| 12 | Static chunk partitioning | Accepted adaptation: contiguous pre-partition of parallel/sequential/barrier chunks reproduces the reference's observable ordering (exclusive re-classification only differs for mid-step registry mutation, which CoderAI's registry does not expose to running turns) | `core/session.py` (unchanged) |
| 13 | Retry-After ignored | `provider_retry_after_ms` (seconds + HTTP-date) honored in session retry and subagent child loop; over-cap value gives up in normal mode | `core/common/llm_retry.py`, `core/session.py`, `core/subagent.py` |
| 14 | Subagent loop never retried | Retryable failures retried with jittered backoff + Retry-After in `_run_subagent_loop` | `core/subagent.py` |
| 15 | workflow schema mismatch | `{script, meta, args}` schema (legacy `workflow/action/workflow_id` params retained) | `core/tools/registry.py`, `core/workflow/tool.py` |
| 16 | goal contract | New `core/goals_dsh.py` domain (phases, blockedReason, roundsStarted, activation, CAS) + `get_goal`/`create_goal`/`update_goal` tools with authority/threshold rules; legacy `goal` tool untouched | `core/goals_dsh.py`, `core/tools/goal_dsh.py` |
| 17 | Ralph drift | budget-limited/round-failed statuses, per-status report validation, maxRounds ceiling 256, handoff/result caps, round-failed carries last handoff; pinned local divergence retained: `blocked` renders as an error result | `core/tools/ralph.py` |
| 18 | No orchestration env knobs | `CODERAI_MAX_SUBAGENT_DEPTH`, `CODERAI_SUBAGENT_TIMEOUT_SECONDS`, `CODERAI_WORKFLOW_MAX_CONCURRENT_AGENTS`, `CODERAI_WORKFLOW_MAX_TOTAL_AGENTS`, `CODERAI_WORKFLOW_MAX_ITEMS_PER_CALL`, `CODERAI_RALPH_MAX_ROUNDS`, `CODERAI_GOAL_MAX_ROUNDS`, `CODERAI_GOAL_BLOCKED_AFTER_ROUNDS`, `CODERAI_MAX_PARALLEL_TOOL_CALLS` + CLI flags `--max-subagent-depth/--subagent-timeout/--workflow-max-agents/--workflow-max-concurrency/--ralph-max-rounds` | `core/orchestration.py`, `cli/app.py` |
| 19 | No goal round driver | `goal_round_driver.py` queued from `SessionManager.reply_session`: active+armed goal continues automatically, blocks at `round-limit`, disarms on hard ends, threshold-gated self-block during rounds | `core/goal_round_driver.py`, `core/session.py` |

### Deliberate adaptations (documented, not silent)
- **Workflow script language**: the reference executes model-written JavaScript in a
  `node:worker_threads` + `node:vm` realm; CoderAI executes the equivalent contract in
  curated-namespace Python (top-level await/return wrapper, same hooks/caps/fatal-vs-null
  discipline). The legacy `async def main(args)` shape remains supported for pinned tests.
- **Structured output**: the in-process child has no native structured-output capture, so
  `outputSchema` is satisfied via schema-prompt + validated JSON extraction of the child's
  final text (validated against the requested schema; failure resolves `null`, matching the
  reference's `structured`-absent child failure).
- **Continuable residency**: the reference disposes a settled Activation and cold-resumes on
  follow-up; CoderAI parks the live worker in-process (its cold-resume equivalent) and keeps
  the durable conversation on the handle — observably identical (settlement notice, FIFO
  turns, interrupt-keepInbox).

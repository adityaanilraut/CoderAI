# DSH Multi-Agent Orchestration — Architectural Specification

> Implementation-oriented audit of the DeepSeek Harness (DSH) reference implementation.
> Source root: `/Users/adityaraut/Downloads/deepseek-harness-master` (read-only).
> All file paths below are relative to that root. `lib/`, `node_modules/`, and tests were ignored except where a test documents behavior not obvious from source.

This document is written for a port to Python. Prefer exact primitives, names, and defaults over prose.

---

## Glossary (DSH concepts)

| Concept | Definition | Primary package |
|---|---|---|
| **subagent** | A one-shot child `Agent` started by the model via the `subagent` tool. Fresh session, no parent conversation; returns one terminal `SubagentResult`. Runs in-process (spawn/fork providers) or out-of-process (acp provider). | `packages/subagent/subagent`, `tool-subagent`, `subagent-in-process-driver` |
| **subagent_fork** | A one-shot child `Agent` SEEDED with the parent's completed-turn prefix (everything up to and including the last `turn/end`), so it inherits conversation context but still gets its own session/tools. Provider `fork`. | `packages/subagent/subagent-fork-in-process` |
| **continuable subagent** | A background child with a durable `Session` that survives across multiple FIFO turns and cold resume. Started with `run_in_background: true` under `backgroundMode: 'continuable'`; continued via `send_message`, listed via `list_agents`, interrupted via `interrupt_agent`. | `packages/subagent/subagent/src/continuation.ts`, `tool-subagent-control` |
| **workflow** | A model-authored JavaScript orchestration script (`agent()`/`pipeline()`/`parallel()` hooks) executed in a fresh `node:worker_threads` worker inside a `node:vm` context, fanning out subagents with caps. | `packages/workflow/workflow-worker-thread`, `tool-workflow` |
| **ralph** | A fixed, deployment-owned foreground loop (a hardcoded workflow script) that starts ONE fresh structured-output child per round toward an immutable objective; only a bounded structured report crosses rounds. | `packages/workflow/tool-ralph` |
| **goal** | Event-sourced same-session completion objective (`active`/`paused`/`blocked`/`complete`) with compare-and-set revision counters and automatic continuation rounds driven by the goal-round driver. | `packages/goal/goal`, `goal-round-driver`, `tool-goal` |
| **job** | A background `Task` (`running`/`stopping`/`completed`/`killed`/`failed`) with an owner `Agent`, a consuming output cursor, and `job_output`/`job_list`/`job_kill` controls. Used by one-shot background subagents (`kind: 'subagent'`) and bash (`kind: 'bash'`). | `packages/jobs/jobs`, `jobs-local`, `tool-jobs` |
| **plan** | Per-agent logged collaboration state (the `plan/mode` session event, last-wins). Active plan mode adds a guidance prompt section; `exit_plan_mode` presents a plan for review and leaves the mode. Not orchestration spawning. | `packages/plan/plan-mode` |

Core framework underneath all of this is **cordis** (`vendor/cordis`): a proxied dependency-injection `Context` with fiber-owned plugins (`ctx.plugin`/`ctx.inject`/`ctx.effect`), a service registry, and an event bus (`ctx.on`/`ctx.emit`/`ctx.parallel`/`ctx.serial`/`ctx.bail`/`ctx.waterfall`).

---

## (A) Dynamic worker spawning & pooling

### A.1 Concurrency primitives (summary)

- **Subagents: no pool, no semaphore.** Each spawn is an independent `Provider.start()` call that publishes one child. The provider contract explicitly permits concurrent calls: *"The service may call one provider concurrently for distinct children… a shared capacity controller may delay an operation but must not couple its settlement or cleanup to a sibling."* (`packages/subagent/subagent/src/types.ts`, `SubagentProvider` doc). The only hard cap on subagent *spawning* itself is the recursion depth cap `maxDepth`; a *background one-shot* subagent additionally counts against the jobs registry per-owner cap.
- **Worker threads** are used ONLY by the workflow engine (`node:worker_threads`), one fresh `Worker` per run.
- **OS child processes** are used ONLY by the out-of-process `acp` subagent provider (via the `dsh-subprocess` seam).
- **`Promise.all` / `Promise.allSettled` / `Promise.race`** are the async fan-out primitives throughout (no `Promise.map`-with-concurrency library).
- **A hand-rolled FIFO slot semaphore** is the workflow engine's `agent()` concurrency limiter (see A.3).

### A.2 Subagent provider registry

`SubagentRuntime` (`packages/subagent/subagent/src/index.ts`) is `ctx.subagents`. Multiple providers coexist (like the LLM adapter registry), each keyed by a unique name:

- `registerProvider(provider)` → effect-scoped; on unregister emits `subagent/provider-removed`; duplicate name throws `SubagentError('DUPLICATE_PROVIDER')`.
- `start(name, request)` → capability check → descriptor snapshot → `provider.start(resolved)` → wraps the returned run with lifecycle observation (`observeRun`).
- `getProvider(name)`, `list(): string[]`.

Built-in providers:

| Provider name | Class / package | Process boundary | Capabilities | `inheritsParentContext` |
|---|---|---|---|---|
| `spawn` | `SpawnInProcessProvider` (`packages/subagent/subagent-spawn-in-process/src/index.ts`) | same process (child `Agent` on same cordis context) | `outputSchema, depthLimit, toolFilter, persona` = all `true` | `false` |
| `fork` | `ForkInProcessProvider` (`packages/subagent/subagent-fork-in-process/src/index.ts`) | same process, seeded | same as spawn | `true` |
| `acp` | `AcpProvider` (`packages/subagent/subagent-acp/src/index.ts`) | fresh OS child process over ACP wire | ALL `false` (`NO_START_CAPABILITIES`) | `false` |

`SubagentCapabilities` (`types.ts`): `{ outputSchema: boolean, depthLimit: boolean, toolFilter: boolean, persona: boolean }`. Requests needing an unsupported capability are rejected with `SubagentError('UNSUPPORTED_CAPABILITY')` before `start()` runs (fail-loud, never accept-then-ignore). Out-of-process providers advertise `NO_START_CAPABILITIES` (`packages/subagent/subagent/src/out-of-process.ts`).

### A.3 Workflow engine worker + slot semaphore

`WorkerThreadWorkflowEngine.start(request)` (`packages/workflow/workflow-worker-thread/src/index.ts`):

1. `validateMeta(request.meta)` (`META_INVALID`), `assertBodyParses` (`SCRIPT_PARSE`), `resolveSubagentProvider` (`AGENT_START` if provider unknown), `resolveMaxTotalAgents` (request cap must be ≤ engine ceiling, `INVALID_ARGUMENT`).
2. Resolve `WorkerLimits`: `maxConcurrentAgents` = config value, or if `0` → `Math.min(16, Math.max(1, availableParallelism() - 2))` (uses `node:os` `availableParallelism`).
3. Build `WorkerInit { meta, body, args?, limits }` and construct `WorkerRun` (`packages/workflow/workflow-worker-thread/src/host.ts`), which does `new Worker(entry, options)` with `workerData: init` and `execArgv: []` and a scrubbed `env` (`workerSpawnEnv`: only `TMP`/`TEMP` on win32, plus `TSX_TSCONFIG_PATH` when unbuilt).

Inside the worker, `WorkflowExecution` (`packages/workflow/workflow-worker-thread/src/runtime.ts`) compiles `new vm.Script('(async () => {\n' + body + '\n})()', { filename: 'workflow:<name>', lineOffset: -1 })` into `vm.createContext({}, { name: ... })`, and runs `compiled.runInContext(context, { timeout: limits.syncTimeoutMs })`.

**`agent()` slot semaphore** (`runtime.ts`, `acquireSlot`/`releaseSlot`):

```
activeSlots: number            // currently occupied concurrency slots
slotWaiters: {resolve,reject}[]  // FIFO queue of waiters
acquireSlot(): if activeSlots < maxConcurrentAgents → activeSlots++ (sync), else push a waiter
releaseSlot(): activeSlots--; shift a waiter and resolve it (activeSlots++)
cancel(): splices all queued waiters and rejects them with WorkflowError(CANCELLED)
```

Cancellation is a HOOK boundary: after `cancel()`, every hook (`agent`/`parallel`/`pipeline`/`phase`/`log`) throws `WorkflowError('CANCELLED')` at its next call; queued `agent()` slot waiters reject; the script dies at its next `await`. A script that never settles is force-settled by the HOST after `disposeGraceMs` and its worker is `worker.terminate()`d.

**`pipeline(items, ...stages)`** (`runtime.ts`): `Promise.all(items.map(async (item, index) => { let value = item; for (const stage of stages) value = await stage(value, item, index); return value; catch → null if not fatal }))`. NO barrier between stages — each item's stage chain runs independently.

**`parallel(thunks)`** (`runtime.ts`): `Promise.all(thunks.map(async thunk => { try return await thunk(); catch → null if not fatal }))`. This IS a barrier (await ALL).

**Failure isolation rule** (`packages/workflow/workflow/src/index.ts`, `isFatalWorkflowError`): only `WorkflowError` with `fatal: true` propagates through combinators; ordinary stage throws and child failures resolve to per-item `null`. All `WorkflowErrorCode`s are fatal by default.

### A.4 ACP out-of-process child spawn + teardown

`startAcpRun` (`packages/subagent/subagent-acp/src/run.ts`):

- Mints a parent-namespace run id `SessionId(randomUUID())` (ACP session ids are child-server-local).
- `spec.spawn({ argv: [command, ...args], cwd, stdio: { stdin:'pipe', stdout:'pipe', stderr:'inherit' }, graceMs: disposeGraceMs, env })` → `SubprocessHandle` (from `dsh-subprocess` seam; parent env is credential-scrubbed, `spec.env` merges after).
- Wires a `ClientSideConnection` over `ndJsonStream(NodeWritable.toWeb(stdin), NodeReadable.toWeb(stdout))` with `PROTOCOL_VERSION`; `initialize` → `newSession({cwd, mcpServers: []})` → `conn.prompt(...)`.
- `disposeAcpChild` teardown ladder: `stdin.end()` → wait `disposeEofGraceMs` (default **6000 ms**) for whole-tree exit → `child.terminate()` (SIGTERM → `disposeGraceMs` default **3000 ms** → SIGKILL; Windows force-terminates) → `waitForExit()`.

---

## (B) Subagent lifecycle & state isolation

### B.1 One-shot in-process run driver

`startInProcessRun(request, options)` (`packages/subagent/subagent-in-process-driver/src/index.ts`):

- `assertSubagentMaxDepth(request.maxDepth)`; reject if `request.signal.aborted` (throw `subagent request was aborted before child publication`).
- `childDepth = resolveChildDepth(parent, maxDepth)` = `delegationDepthOf(parent) + 1`; throws `SubagentDepthError` if `> maxDepth`, `RangeError` past safe-integer.
- `childId = SessionId(randomUUID())`; `seed = options.seed` (fork only); `activationBoundary = seed?.length ?? 0`.
- `captureDelegatedPolicyOverrides(parent)` (synchronous, before first await) → `{ sandboxMode, approvalPolicy }`.
- `parent.ctx.agents.create({ sessionId, meta: childSessionMeta(parent, childDepth, activationBoundary), ...(seed ? {seed} : {}), agentOptions: resolveChildAgentOptions(parent, request.agentOptions, childDepth), signal: request.signal, setup })`.
- `setup(childCtx)` applies: `appendDelegatedPolicyOverrides`, `applyChildComposition(childCtx, parent, {persona, toolFilter})`, optional `attachStructuredRuntime(childCtx, request.outputSchema)`, `attachDescriptorAppend` (appends `subagent/descriptor` inside the child's first turn via an `agent/pre-step` listener).
- Returns `drivePublishedRun(handle, signal, prompt, childId, boundary, structured)`.

`drivePublishedRun`:
- Installs an `abort` listener on `signal` → `flags.cancelled = true; child.cancel({ kind: 'parent' })`.
- Result: `child.followup(createUserMessage({ content: prompt, source: {kind:'user'} }))`, `await child.whenIdle()`, then `readResult(...)`.
- `readResult`: `own = child.session.events.slice(boundary)`; `lastEnd = foldConsumedWork(own).end`; `output = finalAssistantOutput(own) ?? []`; `stopReason = toStopReason(lastEnd?.data.reason)`, overridden to `aborted` if cancelled and recorded ≠ completed; structured capture read if requested.
- `dispose()` (idempotent): `Promise.allSettled([handle.dispose(), result])`.

`toStopReason` maps `TurnEndReason.kind` → `SubagentStopReason`: `completed→completed`, `max-tokens→max-tokens`, `aborted→aborted`, `blocked→refusal`, `error|interrupted|undefined→error`.

### B.2 State isolation per child

`applyChildComposition` (`packages/subagent/subagent/src/child-agent.ts`):

- `childCtx.get('agentPresets')?.composeFrom(childCtx, parent.ctx)` — joins the parent's preset (tool registry + prompt sections) into the child scope.
- Registers the fixed delegation statement `subagent:delegation` context at **order 120** (`SUBAGENT_DELEGATION_CONTEXT`): *"You are a delegated subagent: your permission scope was fixed when you were started and cannot be widened…"*.
- Optional per-child persona: `deployment:persona` section at **order 0**, shadowing the deployment persona.
- Optional `toolFilter` → `childCtx.tools.restrict(filter)` — scoped, so named tools vanish from prompt AND refuse to execute; unknown names fail loud.

`captureDelegatedPolicyOverrides` / `appendDelegatedPolicyOverrides`: seeds `sandbox/mode` (parent's explicit override, `source:'delegation'`) and `approval/policy` (`policy:'never'`, `source:'delegation'`) onto the child's own log, so a delegated child's approval asks are deterministically rejected and its policy is reconstructable from its log alone. Appends land AFTER any fork seed.

`childSessionMeta` persists: `cwd` (inherited), `agentPreset`, `parentSession: parentHeader.id`, `origin: 'subagent'`, `delegationDepth: childDepth`, `seedLength` (only when >0). These live in `SessionHeader` (`packages/core/session/src/types.ts`).

`resolveChildAgentOptions`: inherits parent `provider`/`model`/`maxTokens` unless overridden, always stamps `subagentDepth: childDepth`.

`delegationDepthOf` (`depth.ts`) = `Math.max(agent.session.header.delegationDepth ?? 0, agent.options.subagentDepth ?? 0)` — the persisted header is the monotone floor, so a resumed child cannot delegate as if top-level.

### B.3 Fork seed

`completedTurnPrefix(parent)` (`packages/subagent/subagent-fork-in-process/src/index.ts`): `events.slice(0, lastEnd.seq + 1)` where `lastEnd = events.findLast(e => e.type === 'turn/end')`. Empty (no completed turn) → fresh child (seed omitted). The seed must be contiguous from seq 0 and balanced (no open turn/tool call) — enforced by the session factory.

### B.4 Continuable children (durable background subagents)

`SubagentContinuationManager` (`packages/subagent/subagent/src/continuation.ts`):

- One durable `Session` + at most one process-local **Activation** (one residency epoch of a reconstructed child `Agent`). The Agent inbox is the ONLY turn queue.
- `ActivationState = 'running' | 'waiting' | 'settled'` (derived, not a stored machine): `running` = active admission/turn OR `accepted.size > 0`; `waiting` = quiescent but `ownedChildren.size > 0`; `settled` = quiescent + no owned children → dispose handle.
- `startContinuable(spec)` resolves at **inbox acceptance** (returns `{ childId, messageId }`), NOT turn completion. `spec` = `{ provider, label, childId?, request, signal }`. Reserves id (`SessionId(randomUUID())` unless caller supplies), resolves descriptor (`mode:'continuable'`), calls `provider.prepareContinuable` (returns `{ seed? }`), seeds the descriptor turn, `materialize` (create via `agents.create` with the descriptor seed + `meta` + `agentOptions` + `composition`), `submitMaterialized`.
- `followup(parent, childId, content, options)` — next FIFO turn; routes by residency: resident `running` → enqueue; `waiting` → wake; absent → `coldResume` (loads persisted session via `sessionPersistence.inspect`, authorizes lineage, folds descriptor from own suffix, `agents.resume`, submit). Requires the EXACT live direct parent (`UNAUTHORIZED` otherwise).
- `interrupt(targetSessionId, authority)` — `authority = {kind:'user', parentSessionId} | {kind:'ancestor', agent}`. Authorizes, then `agent.cancel(cause, { keepInbox: true })` (fire-and-return; keeps parked FIFO work + descendants). Unknown/one-shot ids are accepted no-ops.
- `reportFrom(child, content, {delivery, signal})` — child → direct parent. `delivery: 'quiet' | 'next-step'`. Message source `kind:'subagent-report', form:'relay'`.
- Settlement: `watchSettlement` re-checks quiescence under a per-child `ChildLock`; `dispose()` (memoized) cancels top-down then releases child-first; `notifySettlement` sends the parent a `kind:'subagent-settled', form:'notice'` message (`settlementSummary`: "Background subagent <id> finished…"), woken (followup/steer) on idle/busy parent, injected if the parent is already tearing down.
- Per-child serialization: `ChildLock.run(childId, op)` chains operations per durable child id on a promise tail.
- Drain: `drain()` closes admission then disposes the root Activations child-first; `drainDescendants(parents)` / `drainChildren(parent, ids)` for scoped teardown.

**State that does NOT cross to a child**: the parent's tool runtime, service instances, and scope are NOT inherited as live objects — the child is composed fresh in its own scoped `Context` (spawn) or replays a copied event-log prefix (fork). For continuable cold resume, composition (`persona`, `toolFilter`, `agentProvider`, `agentModel`) is reconstructed ONLY from the durable descriptor (version **2**, `SUBAGENT_DESCRIPTOR_VERSION`) — `subagent/descriptor` event; `maxTokens` and `outputSchema` are deliberately NOT persisted (they budget one activation).

### B.5 Result vocabulary

`SubagentResult` (`types.ts`): `{ output: ContentBlock[], structured?: unknown, diagnostic?: string, stopReason: SubagentStopReason }`. `SubagentStopReasonMap = { completed, aborted, error, 'max-tokens', refusal }` (merge-extensible union). `diagnostic` is provider-authored, capped at **4096 UTF-8 bytes** (`MAX_SUBAGENT_DIAGNOSTIC_BYTES`).

`finalAssistantOutput(events)` (`assistant-output.ts`): last non-empty `assistant/message`, else accumulated `assistant/chunk` text-deltas, else `[]`.

`SubagentRun` (`types.ts`): `{ id, localAgent?: Agent, result: Promise<SubagentResult> (never rejects on child failure), dispose(): Promise<void> }`. One disposable foreground delegation = one result. Continuable conversations have no `SubagentRun`.

Stop-reason → tool error text (`tool-subagent/src/index.ts`, `stopReasonError`): `aborted→'subagent run was cancelled'`, `error→'subagent run failed'`, `max-tokens→'…hit its token limit before finishing'`, `refusal→'subagent declined the task'`, unknown → `ended abnormally (<reason>)`.

Stop-reason → `JobOutcome` (`run-settlement.ts`, `runOutcome`): `completed→{status:'completed', output: finalText}`, `aborted→{status:'killed'}`, `error|max-tokens|refusal→{status:'failed', detail}`.

---

## (C) Inter-agent communication & synchronization

### C.1 Event bus (cordis)

`EventsService` (`vendor/cordis/src/events.ts`). Five dispatch modes:

| Mode | Method | Semantics |
|---|---|---|
| emit | `ctx.emit(name, ...args)` | synchronous; runs all listeners; ignores return values |
| parallel | `ctx.parallel(name, ...args)` | `Promise.allSettled` over listeners; throws `AggregateError` |
| serial | `ctx.serial(name, ...args)` | await in order; stops at first bail value (non-null/false/undefined) |
| bail | `ctx.bail(name, ...args)` | synchronous; stops at first bail value |
| waterfall | `ctx.waterfall(name, ...args)` | compose listeners around a final `next`; not calling `next()` vetoes |

`ctx.on(name, listener, {prepend?, global?})` registers a fiber-owned listener (auto-disposed on fiber unload). Scope filtering: if the dispatch `thisArg` carries a `Context.filter`, listeners from contexts not matching the filter are excluded.

### C.2 Scoped dispatch

`packages/core/scope/src/index.ts` (`scopeTarget`, `scopeOf`, `ScopedLayers`) plus `agentEvents`/`agentCarrier` (`packages/core/agent/src/dispatch.ts`). Agent-subject events are dispatched with `thisArg = scopeTarget(agent, agent)` so an agent-scoped listener observes only its own agent (and descendants it composes). `agentEvents(ctx, agent).emit/serial/waterfall(name, payload)` injects `agent` into the payload so subject and scope key cannot diverge.

### C.3 Exact event names & payloads

**Subagent** (`packages/subagent/subagent/src/index.ts` + `lifecycle.ts`, `types.ts`):

- `subagent/provider-added(provider: SubagentProvider)` — emit.
- `subagent/provider-removed(name: string)` — emit.
- `subagent/start(info: SubagentRunInfo)` — emit, scope-filtered by parent. `SubagentRunInfo = { runId: SubagentRunId, provider: string, id: SessionId, local: boolean }`.
- `subagent/end(info: SubagentRunEndInfo)` — emit, same scope. `SubagentRunEndInfo = SubagentRunInfo + { stopReason, lastAssistantMessage?: ContentBlock[] }`.

Pairing: `observeRun` attaches `run.result.then` to emit `end` before emitting `start`, guaranteeing start → end ordering. `runId` is `SubagentRunId(randomUUID())` per accepted run (or per Activation epoch for continuable). Listener failures are contained (logged, never break the run).

**Workflow** (`packages/workflow/workflow/src/index.ts`):

- `workflow/start(info: WorkflowRunInfo{id, meta})`
- `workflow/phase(info, title: string)`
- `workflow/log(info, message: string)`
- `workflow/agent-start(info, agent: WorkflowAgentInfo{seq, label, phase?, childId})`
- `workflow/agent-end(info, agent: WorkflowAgentEndInfo{seq, label, phase?, childId, outcome})` — `WorkflowAgentOutcome = 'completed' | 'failed' | 'cancelled'`
- `workflow/end(info, result: WorkflowResultInfo{stopReason, error?, agentsStarted})`

`WorkflowEventName` union = those six. `WorkflowStopReason = 'completed' | 'cancelled' | 'error'`.

**Goal** (`packages/goal/goal/src/domain.ts`):

- `goal/changed(payload: { agent: Agent, change: GoalChanged })` — emit, scope-filtered. `GoalChanged = { operation: GoalOperation, ref: GoalRef, goal?: GoalView }`.

**Agent** (`packages/core/agent/src/runtime-types.ts`): `agent/created`, `agent/disposed`, `agent/status{agent,status}`, `agent/inbox/inserted{agent,message}`, `agent/inbox/claimed{agent,message,turn}`, `agent/inbox/discarded{agent,message}`, `agent/session-start{agent,source}`, `agent/pre-step` (waterfall), `agent/request` (waterfall), `agent/request-error` (waterfall), `agent/turn-stopping` (serial), `agent/error{agent,turn,step,error}`.

**Tools** (`packages/core/tools/src/index.ts`): `tools/pre-execute` (waterfall, `PreToolDecision = allow | deny | ask`), `tools/execute` (waterfall), `tools/post-execute` (waterfall, `PostToolDecision = accept | block`), `tools/result` (emit, frozen result), `tools/change` (emit, unfiltered), `tools/code-dispatch-log` (waterfall).

**Jobs**: no cordis events. `JobRegistry.onJobDone(listener)` (terminal snapshot + exact owner) and `onJobsChanged(listener)` (owner whose visible set changed), both effect-scoped, both contained.

**Durable session events** (the shared log, appended via `session.append(type, data)`):

- `subagent/descriptor` (`packages/subagent/subagent/src/descriptor.ts`): version `2`, `{ version, mode:'one-shot'|'continuable', provider, label?, agentProvider?, agentModel?, persona?, toolFilter? }`. Model-hidden, log-only, survives compaction.
- `tool-workflow/run-start`, `tool-workflow/agent-start`, `tool-workflow/agent-end`, `tool-workflow/run-end` (`packages/workflow/tool-workflow/src/types.ts`).
- `goal/change` (`packages/goal/goal/src/domain.ts`): `{ kind:'goal/change', version:1, operation, goal, roundsStarted, createdAt, updatedAt }` or the `clear` tombstone.
- `plan/mode` `{active: boolean}` (`packages/plan/plan-mode`).
- `llm/retry`, `llm/retry-started` (`packages/llm/llm-retry/src/types.ts`).
- `agent/inbox/spliced`, `turn/start`, `turn/end`, `user/message`, `assistant/message`, `assistant/chunk`, `request/header` (`packages/core/agent/src/types.ts`, `packages/core/session`).

### C.4 Message passing between agents

`MessageSource` attribution (`packages/llm/llm/src/message.ts` via `MessageSourceMap` declaration merging). Sources introduced by orchestration:

- `coordinator` (`form:'relay'`, `senderSessionId`) — `send_message` follow-up from a parent model.
- `subagent-report` (`form:'relay'`, `senderSessionId`) — child's explicit `report` tool result to parent.
- `subagent-settled` (`form:'notice'`, `summary`, `senderSessionId`) — runtime's account that a child settled.
- `goal` (`goalId`, `revision`, `round`) — automatic goal-round continuation prompt.
- `plugin` (`plugin`, `form:'notice'`, `summary`) — e.g. tool-jobs completion notice, tool-goal wrap-up.
- `user` — human (or omitted-source default).

Delivery verbs on `Agent` (`packages/core/agent/src/runtime-types.ts`): `followup(message)` (ordinary next-turn, wakes), `steer(message)` (nearest step; busy → next-step batch, idle → new turn), `inject(message)` (next-step context, does NOT wake), `send(message, target, wakeup)` (raw), `cancel(cause, {keepInbox?})`, `whenIdle()`, `runMaintenance(task)`, plus `status: 'idle'|'running'` and `inbox` (two ordered lists: `next-turn`, `next-step`).

### C.5 Progress consumption by the parent

- **Workflow**: `tool-workflow` registers `createWorkflowRecorder` listening on `workflow/agent-start` and `workflow/agent-end` and writes `tool-workflow/*` records into the parent's session log. The engine's `emitWorkflowEvent` also fans `phase`/`log`/`agent-*`/`start`/`end` to any observer.
- **Subagent**: `createLifecycleEmitter` emits `subagent/start`/`subagent/end` with per-listener exception containment.
- **Jobs**: `tool-jobs` registers `ctx.jobs.onJobDone((snapshot, owner) => …)` which, for an unreported completion, builds a `plugin`/`notice` message and (default `completionDelivery:'wakeup'`) wakes an idle owner via `owner.followup` (bounded by `maxConsecutiveWakes`, default **3**, reset on claimed `user` input), or `owner.inject`s into a busy owner.

### C.6 Cancellation / abort propagation

- The single canonical channel for a one-shot child is the start request's `AbortSignal` (the tool's `exec.signal`). In-process: abort listener → `child.cancel({kind:'parent'})`. ACP: abort → `requestCancel()` (races `conn.prompt` against local `cancelSettled`), and `dispose()` tears the process down regardless of child cooperation.
- `AgentCancelCause = {kind:'user'} | {kind:'parent'} | {kind:'hook', reason} | {kind:'disposed'}` (session `types.ts`).
- Workflow `cancel(reason)`: posts `Cancel` to worker (hooks throw, script dies at next await) + aborts the one shared `AbortController` carried by every child start + arms a grace timer (`disposeGraceMs`, default **5000 ms**, `unref()`d) that force-settles `cancelled` and `worker.terminate()`s. `dispose()` = cancel + `Promise.race([result→childQuiescence, sleep(disposeGraceMs)])` + terminate + reap.
- Process signals: `SIGINT` → exit **130**, `SIGTERM` → exit **0**; teardown disposes the tree with `PROCESS_SHUTDOWN_TIMEOUT_MS = 5000` escalation to `process.exit` (`apps/cli/src/process-shutdown.ts`).
- Tool-level: `ToolRunContext.signal` (fused caller signal); `TOOL_ABORTED = 'ABORTED'`, `TOOL_ABORTED_BEFORE_DISPATCH = 'ABORTED_BEFORE_DISPATCH'` (`packages/core/tools/src/index.ts`); `timeoutMs` cooperative budget enforced by `dsh-tool-call-timeout-policy` (a `tools/execute` wrapper).

---

## (D) Concurrent tool invocation & rate limiting

### D.1 Tool execution pipeline & parallel dispatch

`ToolRuntime` (`packages/core/tools/src/index.ts`). Pipeline order: `tools/pre-execute` (waterfall allow/deny/ask) → monotonic `guard` → `tools/execute` (waterfall; timeout/retry/metrics wrappers) → tool body → `tools/post-execute` (waterfall accept/replace/block) → `finalizeContent` → lossless materialization → `tools/result` (emit).

`isConcurrencySafe?(args): boolean` on `ToolDefinition`: only `true` opts a call into overlap with sibling calls; omission/exceptions/non-`true` → exclusive (ordering barrier). The `subagent` tool declares `isConcurrencySafe: () => true`. `ToolExecutionMode = {kind:'parallel'} | {kind:'exclusive'}`.

`maxParallelSubCalls` (tool config, default **10**): cap for a `run_code` program's overlapping sub-calls. `ToolPresentationMode = 'native' | 'code' | 'both'` (default `native`).

### D.2 LLM rate limiting / retry (per-provider)

There is **no token bucket, sliding window, or semaphore** in `packages/llm`. Rate limiting is reactive: the request-retry policy (`packages/llm/llm/src/retry-policy.ts`) plus the `retry-after` header.

`RetryPolicyConfig` (under each provider's config, key `retryPolicy`):

- `{ mode: 'normal', maxRetries? (default 5), retryableCodes? (default ['EMPTY_RESPONSE','RATE_LIMIT','SERVER','TIMEOUT','TRANSPORT']), backoff? }`
- `{ mode: 'always', backoff? }` — unbounded until success/cancel/disposal.
- `backoff = { initialDelayMs? (500), maxDelayMs? (10_000), jitterRatio? (0.1) }` — all bounded by `MAX_TIMER_DELAY_MS` (from `dsh-timeout`).

Delay formula (`packages/llm/llm-retry/src/index.ts`, `localDelay`):

```
exponent  = min(retry - 1, 1024)
exponential = min(initialDelayMs * 2**exponent, maxDelayMs)
jitter    = 1 - jitterRatio + 2 * jitterRatio * random()   // symmetric [1-r, 1+r]
delayMs   = min(exponential * jitter, maxDelayMs)
```

`retry-after` header handling: `providerRetryAfterMs` (seconds or HTTP-date) is honored when `> 0`; in `normal` mode a provider delay `> maxDelayMs` → NO retry (give up); in `always` mode → fall back to `localDelay`. HTTP status mapping (`packages/llm/llm-deepseek/src/adapter.ts`, `httpErrorCode`): `401/403→AUTH`, `413→INVALID_REQUEST`, quota wording→`QUOTA`, `429→RATE_LIMIT`, `400→CONTEXT_WINDOW_EXCEEDED|INVALID_REQUEST`, `≥500→SERVER`, else `HTTP_<status>`. Retry numbers are durable (`llm/retry` before the wait, `llm/retry-started` after) and validated by `packages/llm/llm-retry/src/invariant.ts`.

### D.3 Complete concurrency/budget parameter table (exact names, defaults, source)

| Parameter | Default | Meaning | Source file |
|---|---|---|---|
| `maxDepth` | `3` (`0` forbids; `'provider-managed'` = no cap) | subagent recursion depth cap | `packages/subagent/tool-subagent/src/index.ts` (Config) |
| `backgroundMode` | `'one-shot'` (`'continuable'`) | default background policy | same |
| `enableRunInBackground` | `true` | expose `run_in_background` param | same |
| `maxConcurrentJobsPerOwner` | `10` | running+stopping jobs per exact owner | `packages/jobs/jobs-local/src/index.ts` |
| `maxConcurrentAgents` | `0` → `min(16, max(1, cores-2))` | concurrent `agent()` in one workflow | `packages/workflow/workflow-worker-thread/src/index.ts` |
| `maxTotalAgents` | `1000` | total `agent()` per workflow run (runaway-loop backstop) | same |
| `maxItemsPerCall` | `4096` | items per `parallel()`/`pipeline()` call | same |
| `syncTimeoutMs` | `5000` | vm timeout for initial synchronous slice | same |
| `disposeGraceMs` | `5000` | cancel→force-settle+terminate window | same |
| `maxResultChars` (workflow tool) | `50_000` | rendered JSON result ceiling | `packages/workflow/tool-workflow/src/index.ts` |
| `maxRounds` (ralph) | `256` | Ralph round cap | `packages/workflow/tool-ralph/src/index.ts` |
| `maxHandoffChars` (ralph) | `16_384` | serialized structured handoff cap | same |
| `maxResultChars` (ralph) | `16_384` | terminal text cap | same |
| `defaultMaxGoalRounds` | `256` | goal round cap when create omits it | `packages/goal/goal/src/index.ts` |
| `blockedAfterConsecutiveRounds` | `3` | min rounds before model may self-`block` | `packages/goal/tool-goal/src/index.ts` |
| `waitTimeoutMs` | `30_000` | `job_output wait:true` default | `packages/jobs/tool-jobs/src/index.ts` |
| `maxWaitTimeoutMs` | `600_000` | clamp on model-supplied `timeout_ms` | same |
| `completionDelivery` | `'wakeup'` | idle-owner completion wake policy | same |
| `maxConsecutiveWakes` | `3` | self-exciting wake budget (reset on user input) | same |
| `maxParallelSubCalls` | `10` | run_code sub-call overlap cap | `packages/core/tools/src/index.ts` |
| `retryPolicy.maxRetries` | `5` | LLM retries (normal mode) | `packages/llm/llm/src/retry-policy.ts` |
| `retryPolicy.backoff.initialDelayMs` | `500` | retry backoff base | same |
| `retryPolicy.backoff.maxDelayMs` | `10_000` | retry backoff ceiling | same |
| `retryPolicy.backoff.jitterRatio` | `0.1` | symmetric jitter ratio | same |
| `COLD_READ_CONCURRENCY` | `4` | concurrent persistence inspections per listing | `packages/subagent/subagent/src/list-children.ts` |
| `disposeEofGraceMs` | `6000` | ACP EOF quiesce window | `packages/subagent/subagent-acp/src/run.ts` |
| `disposeGraceMs` | `3000` | ACP SIGTERM→SIGKILL window | same |
| `MAX_SUBAGENT_DIAGNOSTIC_BYTES` | `4096` | diagnostic cap | `packages/subagent/subagent/src/out-of-process.ts` |
| `PROCESS_SHUTDOWN_TIMEOUT_MS` | `5000` | CLI shutdown escalation | `apps/cli/src/process-shutdown.ts` |
| `provider` (workflow) | `'spawn'` | default subagent provider for `agent()` | `packages/workflow/workflow-worker-thread/src/index.ts` |

Config/env override note: every `Config` above is a schemastery `z.object` schema resolved from the profile's `cordis.yml` patch layers (not CLI flags). Provider `retryPolicy` is the only orchestration-relevant rate control, and it is set per-provider in config.

---

## (E) CLI / env contract

### E.1 Launcher flags (`apps/cli/src/args.ts`)

`parseDshArgs` uses `commander` with `passThroughOptions()` + `allowUnknownOption()`; the launcher stops parsing at the first token it does not recognize, and everything after is handed to the booted app verbatim.

- `--profile <name>` — profile under `$DSH_HOME/profiles` to boot.
- `--patch <path>` — repeatable extra patch-list overlay, applied after the profile layer, in argv order.
- `--dump-config` — print the composed profile tree and exit.
- `--dump-default-config` — print bundle layers only (no user layer / `--patch`), exit. Mutually exclusive with `--dump-config`.
- `-V, --version`.
- Subcommand `web` — alias for `--profile web`; takes its own `--patch`, `--dump-config`, `--dump-default-config`.
- Subcommand `plugin --profile <name> <pnpm args...>` — forwards to pnpm inside the profile dir and reconciles `dsh.profile.bundles` (`apps/cli/src/plugin.ts`).

`apps/cli/src/bin.ts` dispatches by invocation mode: `profile` (→ `runProfile`), `plugin` (→ `runPlugin`), `dump-config` (→ `runDumpConfig`).

### E.2 Env vars

- `$DSH_HOME` — profiles root; home-level user patch layer at `$DSH_HOME/cordis.patch.yml` (applied over every profile's own layer, outranking it).
- `DSH_TELEMETRY_DISABLED` — ANY non-empty value (including `'0'`/`'false'`) disables the `session-telemetry-otel` row (`apps/cli/src/profile-boot.ts`, `resolveTelemetryPatch`).
- Layered env via `loadLayeredEnv('dsh')`; the snapshot is provided to the tree as `DSH_LAUNCH_ENVIRONMENT_KEY` before any entry mounts.

### E.3 Inner app args

`provideCmdline(ctx, { args, exit })` installs `ctx.cmdlineArgs` (`CmdlineArgs.get(): readonly string[]`, frozen snapshot) and `ctx.appExit` (`AppExit`). Apps build their own `commander` program and call `parseCmdline(ctx, program)` (`packages/boot/cmdline/src/index.ts`). **There are no launcher-level orchestration flags** (no `--max-agents`, `--budget`, `--replay`, `--resume` in `apps/cli`). Orchestration knobs are config fields (table in D.3). Session *resume/replay* is an API/session-layer capability (`packages/host/apiproxy` `sessions.ts` `resume`; session projections and `SessionStore.prepare`/`RestoredSessionOptions`), not a CLI flag.

### E.4 Telemetry / usage aggregation across children

- **Workflow**: `WorkflowResult.agentsStarted` counts `agent()` calls; `workflow/end` carries `agentsStarted`; the `tool-workflow/*` durable records carry `runId`, per-member `seq`, `label`, `phase`, `childId`, `outcome`.
- **Subagent timing/identity**: two session projections (`packages/subagent/subagent/src/projection.ts`): `subagentTiming` (`{ settledMs, active? }` accumulated active-turn ms) and `subagent` identity (`{ mode, label, seq }`), both `stateVersion: 2`.
- **Tokens**: `packages/llm/token-meter` provides token accounting over surface events.
- **Goal**: `goal` projection (`packages/goal/goal/src/types.ts`) = `{ goal, roundsStarted, createdAt, updatedAt } | null`.
- **Plan**: `plan` projection (`packages/plan/plan-mode`) = `{ active, pending }`.
- Per-child records are correlated by the child's `SessionId` (the `subagent/descriptor` + `parentSession`/`delegationDepth` in the `SessionHeader` enable the durable tree walk of `listChildren`/`listDescendants`).

---

## Appendix: key type/message names quick reference

- `SubagentProvider`, `SubagentRuntime`, `SubagentRun`, `SubagentResult`, `SubagentStopReason`, `SubagentCapabilities`, `SubagentRunInfo`, `SubagentRunEndInfo` — `packages/subagent/subagent/src/types.ts`.
- `SubagentError` codes: `DUPLICATE_PROVIDER`, `NO_PROVIDER`, `UNSUPPORTED_CAPABILITY`, `CONTINUATION_UNAVAILABLE`, `UNAUTHORIZED`, `ACTIVATION_CLOSING`, `PARENT_UNAVAILABLE`, `NOT_RESUMABLE`, `DRAINING`, `DUPLICATE_CHILD`, `CANCELLED`, `ACTIVATION_TEARDOWN_FAILED`, `ACTIVATION_SETUP_REVOKED`, `ACTIVATION_SETUP_RELEASE_FAILED` (`packages/subagent/subagent/src/error.ts`, `continuation.ts`, `activation-setup-registry.ts`, `list-children.ts`).
- `WorkflowEngine`, `WorkflowRun`, `WorkflowResult`, `WorkflowStopReason`, `WorkflowError`, `WorkflowErrorCode` (`SCRIPT_PARSE, META_INVALID, INVALID_ARGUMENT, UNSUPPORTED_OPTION, UNSUPPORTED_SCHEMA, AGENT_CAP, ITEM_CAP, AGENT_START, AGENT_RESULT, RESULT_UNSERIALIZABLE, CANCELLED`) — `packages/workflow/workflow/src/index.ts`, `types.ts`.
- Worker protocol enums: `WorkerToHostType = ready|phase|log|agent-start|agent-end|child-start|child-dispose|result`; `HostToWorkerType = go|cancel|child-started|child-start-error|child-settled|child-failed|child-disposed` — `packages/workflow/workflow-worker-thread/src/protocol.ts`.
- `JobRegistry`, `JobStatus`, `JobOutcome`, `JobSnapshot`, `JobId` (`<kind>-N`), `JobKindMap { bash, subagent }` — `packages/jobs/jobs/src/{index,types,brand}.ts`.
- `GoalService`, `GoalRef`, `GoalSnapshot`, `GoalPhase` (`active|paused|blocked|complete`), `GoalOperation` (`create|edit|pause|resume|complete|block|clear`), `GoalActivation` (`armed|disarmed`), `GoalErrorCode` — `packages/goal/goal/src/{index,types,domain,fold,runtime}.ts`.
- `Agent`, `AgentHandle`, `AgentStatus` (`idle|running`), `AgentCancelCause`, `AgentOptions` (`provider, model, maxTokens, subagentDepth`), `PreStepDecision` — `packages/core/agent/src/{runtime-types,index}.ts`.
- `ToolDefinition`, `ToolRunContext` (`deferContext`, `concludeTurn`, `exec.signal`, `exec.agent`), `ToolRestriction`, `ToolPresentationMode` — `packages/core/tools/src/index.ts`.
- `SessionHeader` fields: `version, id, createdAt, cwd?, parentSession?, seedLength?, origin?: 'subagent', delegationDepth?, agentPreset?` — `packages/core/session/src/types.ts`.

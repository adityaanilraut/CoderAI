# Architectural Parity & Hardening Audit

Audit completed against the supplied source snapshot on 2026-10-04.

CoderAI now has stronger execution, cancellation, context, persistence, and protocol guarantees. **Exact functional, structural, and terminal UX parity is not certified.** The remaining differences below are part of the audit result, rather than assumptions hidden behind a passing test suite.

## Scope and reference identity

Target: `CoderAI`.

Reference: `<reference-checkout>/kimi-code-main`, CLI package version **2.1.1**. This reference is a TypeScript monorepo, without Git metadata. [The reference manifest](architectural-parity-reference.json) pins the inspected files by SHA-256; it does not identify an upstream commit.

Three agents examined orchestration/streaming, tool execution, and sessions/protocols independently. Their changes were integrated and reviewed, including cancellation and configuration regressions caught by the isolated suite. Existing uncommitted workspace changes were retained. Consequently, the aggregate Git diff also contains work that predates this audit.

The implementation remains Python, Rich, prompt-toolkit, terminal and stdio based. No browser frontend, HTML viewer, or HTTP presentation server was introduced. The reference's web/desktop features are outside the user-authorized terminal scope. No PEP 695 syntax was introduced.

Some requested terminology differs from the actual snapshot: the reference supports `--session`, `--resume`, and `--continue` flags; its inspected Grep and Glob input schemas each accept one `path`. Multiple workspace roots and concurrent searches are distinct from a native list-of-paths argument.

## Parity matrix

“Adapted” means the behavioral guarantee has a Python implementation and focused regression coverage. It does not mean source structure, event schema, or presentation is identical.

| Area | Reference evidence | CoderAI implementation and outcome | Status |
| --- | --- | --- | --- |
| Hierarchical children | `session/subagent/{subagentService,spawn,runAgentTurn,mirrorAgentRun}.ts` | Independent child conversations, inherited execution policies, parent ownership, recursive interruption, and durable child artifacts in `subagents/` | Adapted; restart/model compatibility gaps remain |
| Live child activity | TUI `subagent-event-handler.ts`, `subagent-activity-store.ts` | `ChildWireEmitter` forwards child envelopes; a bounded Rich tree renders status, steps, and tool activity; Ctrl-E opens collapsed output | Adapted; layout differs |
| Interrupted checkpoints | Subagent abort lifetime and mirrored run handling | Pending assistant tool calls receive interrupted results; partial content is checkpointed; completion clears retained caller tasks | Adapted |
| Token accounting | `session/tokenCounting/tokenCountingAgentModel.ts` | Measured anchors plus pending estimates; invalidation after summaries; media cost estimation; configurable strategies | Adapted; estimator and event model differ |
| Compaction | `fullCompaction/{strategy,fullCompactionService}.ts`, `contextMemory/compactionHandoff.ts` | Serialized, tool-boundary-aware summaries; pinned exchanges retained; stale/empty/truncated summaries rejected; child-local distillation | Partial |
| Tool scheduling | `toolExecutor/{toolScheduler,toolExecutorService}.ts` | Fair resource conflicts, concurrent unrelated paths, retained locks until cancelled workers finish, conservative handling for unknown tools | Adapted; scope is more conservative |
| Shell lifecycle | `tools/os/bash/bashTool.ts` | Foreground/background timeout vocabulary; same-process timeout handoff; process-tree termination/reaping; PowerShell cancellation | Adapted; persistent PTY/config differences remain |
| Hooks and permissions | Tool executor lifecycle interception | Asynchronous pre/post-tool, turn, child, prompt, and compaction paths; subprocess cleanup on cancellation; descriptor restrictions enforced at execution | Adapted; fail-closed policy retained |
| Large tool results | `toolResultTruncation/toolResultTruncationService.ts` | Sanitized durable spills and readable pointers, including failed/MCP results; valid inline JSON envelopes | Adapted; thresholds differ |
| Session resume/fork/replay | Reference session/subagent services and ACP `session.ts` | Forks copy context/state/wire; unsafe IDs/collisions rejected; native/legacy text and thinking replay; persisted ACP controls | Partial schema/child replay parity |
| Migration | `migration-legacy/src/run-migration.ts` | `coderai migrate` safely copies legacy CoderAI project sessions/state/assets into workspace storage | Partial; broader Kimi migration absent |
| Configuration and MCP | Reference task/profile configuration and dynamic registries | Existing scalar precedence preserved; trusted workspace MCP overlays; concurrent connections; stale-generation guards; automatic tool-list refresh | Adapted |
| ACP | `acp-server/src/{session,config-options}.ts` | Default/plan/auto/yolo modes; capability-aware model/thinking selectors; resume/fork persistence; shutdown disposal | Partial SDK/protocol parity |

Reference paths in the table are relative to `packages/agent-core-v2/src/`, except the explicitly named TUI, migration, and ACP packages. Target paths are relative to `coderai/`.

## Execution and context fixes

### Children and cancellation

`subagents/runner.py` races provider/tool work and start hooks against the child's abort signal. Terminal lifecycle notices are emitted on completion, failure, timeout, and interruption. Child handles retain parent/root relationships, but relinquish the invoking task once the child settles. Registry cleanup therefore cannot later cancel a parent that has already continued.

Killing a native foreground child sets its owned abort signal and returns an interrupted result. Direct cancellation of the invoking parent task still propagates cancellation. Session closure drains only owned child resources and preserves unrelated sibling sessions. Continuable workers stop their current turn while retaining queued follow-ups, and their UI spinners stop at settlement rather than waiting for parked workers to exit.

Child tool policies are applied both when constructing schemas and at execution. Parent masks, discovered descriptors, aliases, namespaced MCP tools, inherited permissions, and plan/read-only restrictions cannot be bypassed by an advertised tool name alone. Parent history snapshots retain tool-call IDs and completed exchanges, omit the current unanswered exchange, and copy mutable payloads.

Child `meta`, context, output, prompt, and wire artifacts persist through `SubagentStore`. These artifacts preserve evidence; they do not yet reconstruct every live worker relationship after a process restart.

### Budgets and compaction

Parent and child completion requests honor the configured context window. Request rebuilding retains the selected completion cap and tool choice. Zero reserved context is a valid setting. Pending tool output increases the estimate instead of relying only on the previous response's usage. Media payloads receive a bounded estimate rather than counting base64 text as ordinary prose.

Compaction works on projections rather than mutating stored message objects. Interleaved user steering cannot cause a cut through an assistant/tool exchange; pinning one member retains the whole exchange. Sessions serialize compaction requests, and manual compaction requires an idle session. Custom focus is call-local. A rewind during summarization invalidates the pending commit. Empty or truncated summaries leave original context intact. Summary request usage is no longer incorrectly recorded as the size of removed history.

Children distill their own verbose intermediate work with a tool-free request, retaining system instructions, the initial task, recent steering, and the latest exchange. A rejected or nonshrinking summary leaves their scratchpad unchanged. The parent receives a result/summary rather than the child's entire live conversation.

### Scheduling, hooks, and subprocesses

`tools/legacy/resources.py` implements FIFO ordering among conflicting accesses while allowing unrelated paths to progress. Read/search operations can coexist; mutations conflict with reads and recursive searches over the affected path. Unknown MCP/plugin accesses default to exclusivity. Cancelled noncooperative synchronous handlers keep resource/path leases until their worker exits. Cancellation also fixes the previous queued-read lock accounting leak.

Async tool deadlines cancel the owned handler. Bash and PowerShell cancellation terminate and reap subprocess trees before cleanup finishes. Foreground Bash uses a 60-second default, up to 300 seconds; explicit background work defaults to 600 seconds, up to 86,400 seconds. Background timeout disabling is explicit. A foreground timeout can detach the same process when task management tools are available. `bashAutoBackgroundOnTimeout=false` disables that behavior; explicit legacy `timeout_ms` retains a hard timeout. Persistent PTY execution retains its existing path.

Hooks run outside the event loop's blocking subprocess path. Cancellation reaps hook subprocesses, including while a turn or child is starting. Hook errors/timeouts retain CoderAI's fail-closed authorization policy. This is an intentional security difference from reference behavior, not a claimed exact match.

Large output is sanitized before persistence, saved under durable session tool-result storage, and referenced by a bounded preview. MCP output is no longer irreversibly cut before the shared spill policy. Failed diagnostics are also preserved. JSON envelopes retain valid structure. Existing read tools remain exempt from recursive spilling. New spill files use mode `0600`; deleting a session removes its durable results, while ordinary runtime shutdown preserves them.

## Sessions, configuration, and protocols

Session creation rejects unsafe/duplicate IDs. Facade forks copy the materialized conversation, session state, and wire history as well as the underlying engine session. ACP records wire activity before sending client updates, understands both supported text/thinking representations, and restores model/thinking/mode controls for resumed sessions.

ACP advertises four modes: `default`, `plan`, `auto`, and `yolo`. Auto maps to the existing unattended AFK behavior. Unsupported thinking values are rejected rather than advertised. Server shutdown cancels sessions, closes their managers/MCP resources, and clears terminal bridges.

MCP merges global configuration, trusted repository-root `.mcp.json`, trusted local `.coderai/mcp.json`, and explicit overlays. Untrusted project configuration remains excluded. Connections initialize concurrently. Generation guards prevent a late connection/disconnection from restoring disabled tools or removing a replacement client's tools. `notifications/tools/list_changed` schedules coalesced refreshes; shutdown cancels tracked refresh work.

`coderai migrate --dry-run --source <legacy-store> --work-dir <workspace> --report <file>` is reviewable without changing storage. Applying migration leaves sources untouched, preserves destination collisions and shared checkpoint conflicts, rejects overlapping roots/symlink inputs, takes an exclusive migration lock, publishes copies without overwriting existing files, and commits the index last. Automatic legacy session migration uses the same safe path and falls back to the global store on failure.

## Remaining work for exact parity

These are substantive gaps. Passing CoderAI tests cannot close them without additional reference-derived contract work.

1. **Full compaction state machine and recovery.** Port the reference's recent-message/token-window selection, user-origin retention and 20,000-token user-message treatment, retry/overflow shrinking, blocking threshold, observed model capacities, todo/tool recovery pointers, and durable handoff graph. CoderAI currently retains its older-region selection plus safer commit rules, and a separate child distiller. Token estimates also use different heuristics and anchor representation.
2. **Durable subagent lifecycle equivalence.** Define restart/resume/fork compatibility and model-routing checks for every child profile, and reconstruct worker ownership from durable state. The Python envelope/event-bus adapter differs from the reference's scoped DI and semantic event graph. Synchronous SDK network calls in threads may remain blocked until the provider returns or yields; asynchronous cancellation cannot forcibly stop Python threads.
3. **Exact terminal rendering and ACP child replay.** CoderAI's Rich activity panel and pager differ from Kimi's inline tool rows and step presentation. Existing diff/rendering tests establish target behavior, not screenshot equivalence. ACP currently records child envelopes but does not translate/replay their activity into equivalent client tool trees. Add differential event fixtures and real PTY/SIGINT presentation tests.
4. **Broader migration compatibility.** The reference migrates configuration, MCP, user history, skills, plans, and its own session schema. The new command migrates legacy CoderAI project sessions/state/assets only. It must not be represented as an importer for Kimi's full data format.
5. **ACP SDK and capability differences.** Installed `agent-client-protocol` 0.8.1 lacks typed close/delete-session methods/capabilities used by newer reference behavior. Shutdown disposal is implemented; session close/delete requests and the reference's full thinking-effort taxonomy remain unimplemented. Upgrade and negotiate supported capabilities before advertising them.
6. **Tool/config presentation details.** Exact spill thresholds differ (reference 50,000 characters; target has existing 30,000-byte previews and 32,000-character message envelopes). Background timeout configuration and persistent PTY details are not fully identical. Scheduling is shared per event loop rather than scoped per reference agent. Native multi-path search arguments, if desired, are an extension beyond the inspected reference schemas.

## Verification and practical limits

The release verification command is:

```bash
.venv/bin/python scripts/verification.py check --suite all \
  --report docs/architectural-parity-verification.json
```

It runs test files independently with isolated homes and scrubbed credentials, rather than putting the entire suite in one process. Dependencies were installed with `pip install -e '.[dev,jev]'`; the real Jev SDK tests execute. The environment is Python 3.14.7; this run does not independently prove Python 3.12 or Windows compatibility.

[Machine-readable test evidence](architectural-parity-verification.json) records per-file outcomes. Formatting, Ruff, mypy, dependency constraints, `pip check`, and the dependency audit are separate verification gates. Three broad mypy suppressions were removed; suppression budgets were not increased. `git diff --check` and the migration CLI help smoke test also pass.

Final result: **1,354 tests passed across 98 independently executed files; zero failures, errors, or skips.** The complete `check --suite all` command exited successfully. Ruff checked formatting of 428 files, mypy reported no issues across 294 source files, and all 28 installed dependency constraints plus `pip check` passed. The dependency audit passed and saved evidence to `.verification/dependency-audit.json`. Offline tests took approximately 118 seconds in aggregate, excluding the other gates.

Focused regression evidence covers partial streaming, child policies, cancellation trees, parked settlement, child distillation, token accounting, stale compaction, idle/manual leases, resource fairness, sync-worker lock retention, foreground/background process ownership, hook process cleanup, large failed/MCP output, MCP reconnection races/notifications, session forks, ACP controls/replay, and migration collision/crash behavior.

Live provider quality, cross-platform OS behavior, long-lived process restart recovery, and exact Kimi terminal screenshots were not validated by these offline tests. The reference test suite was inspected as evidence where relevant; it was not executed as a cross-runtime differential oracle.

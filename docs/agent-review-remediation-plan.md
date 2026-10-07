# Agent review verification and phased remediation plan

Prepared October 6, 2026 as the pre-implementation audit. Implementation was subsequently authorized and delivered; see the [implementation and verification report](agent-review-remediation-implementation.md). The verdicts below describe the audited baseline, not the repaired code.

## Basis and limits

This plan checks the supplied 105-item review against the **current working tree**, whose HEAD is `dd7db969dc9c4b1d6bb8a9e62274dd1edb729dc3`. HEAD alone does not identify the reviewed source: the workspace already contains extensive tracked edits and untracked modules. Preserve that work and recheck the affected files before implementation. The supplied line numbers mostly locate the relevant functions, but several conclusions omit newer guards and caller behavior.

Verification combined source inspection, caller/reference searches, temporary-directory probes, and eight existing test files. No live provider requests, destructive external-path experiments, installed external-agent CLI compatibility checks, or exhaustive concurrency stress tests were performed. A source-confirmed weakness does not automatically establish the review's stated exploit or severity.

Verdicts in the claim ledger:

- **V — verified defect/behavior:** reproduced locally or directly supported by the implementation; the note specifies what is established.
- **P — partially supported:** a weak primitive or inconsistency exists, but the review overstates reachability, consequences, severity, or part of the claim.
- **N — not supported as stated:** current guards, caller behavior, or a passing regression contradict the central claim.
- **R — requires reproduction:** plausible, but inspection alone does not establish the claimed failure. Do not implement speculative fixes until a deterministic reproduction exists.

The original numbers are preserved below. Phase assignments describe planned work, not permission to start it.

Disposition: **39 verified defects/behaviors, 57 partially supported claims, 6 unsupported as stated, and 3 requiring reproduction.** These are claim-level counts, not 39 independently demonstrated security exploits; several entries overlap.

## What should drive the work

The strongest findings are unsafe storage paths and scratchpad cleanup, permissive or inconsistent spec parsing, lower-level approval bypasses, incomplete terminal-status handling, polling teammate workers, malformed/deep graph handling, ambiguous fork points, and unsafe file-history restoration.

Several headline claims need correction:

- **#19:** `spawn_teammate` is already denied in read-only execution and filtered from read-only tool advertisement; it is also included in recursion filtering.
- **#63:** passing `depth=None` deliberately invokes ancestry-derived depth. The nested teammate regression verifies depth inheritance and rejection at the inherited cap.
- **#55:** `compute_tool_call_permissions(settings=None)` applies defaults and produced a permission row in the probe. The cited `None` crash is not present for a valid call.
- **#96:** `_setup_session` restores the persisted engine binding and has a same-ID fallback. Missing binding and workspace-mismatch handling still deserve explicit validation; normal resume is not universally blank.
- **#18:** the helper uses some exact tool names, but the tested `Bash`, `EDIT`, `shell`, and `Terminal_Open` spellings are not registered tools. Helper acceptance alone does not demonstrate execution. Known write tools also have mutation metadata.
- **#80:** the permission resolver really defaults to `allow`; that is a risky fallback, not proof that arbitrary unknown tools execute. The executor still resolves definitions and enforces separate invocation-bound grants.
- **#100:** locks deliberately remain held until a synchronous worker finishes. Preserve that protection; the remaining issue is cooperative stop/settlement behavior, not premature lock release.
- **#54:** `SessionSoul` is documented as an adapter for state and prompt injections. Its filename does not promise enforcement of agent-role policy.

## Phase order

| Phase | Result | Dependency |
| --- | --- | --- |
| 0 | Stable baseline, grouped findings, deterministic reproductions | Before each affected change |
| 1 | Safe persistence/restore paths and session-bound approval/terminal boundaries | 0 |
| 2 | One validated, monotonic child capability policy | 1 |
| 3 | Predictable agent cancellation, settlement, and resource disposal | 2 |
| 4 | Scoped, event-driven team coordination with bounded messaging | 2–3 |
| 5 | Correct forks, repair rows, compaction, and protocol lifecycle | 1–4 |
| 6 | Remove proven dead scaffolding and duplicate state/policy paths | Relevant behavior stabilized in 1–5 |
| 7 | Integration and release verification, updated architectural contract | 1–6 |

Small cleanup can accompany the phase that makes it safe. Do not bundle independent security fixes with a wholesale orchestration rewrite.

### Phase 0 — Baseline and reproduce

1. Record a source/diff fingerprint including untracked runtime files. Keep existing edits intact; use the current checkout or a snapshot that explicitly includes those edits. A worktree from HEAD alone would omit the code audited here.
2. Convert the V/P findings into grouped tickets with an actual trigger, expected behavior, affected entry points, and a failing regression. Keep duplicates together: #10/#49, #17/#50/#52, #18/#19/#24/#54, and the overlapping cancellation findings.
3. Reproduce R findings using controlled barriers/events rather than timing-only sleeps. Trace public tool/ACP/CLI reachability separately from direct Python API behavior.
4. Define intended contracts before changing them: missing spec fields, intentional wildcard policies, trusted role roots, single-loop ownership, continuation settlement notices, message consumption versus user acknowledgment, external backend capabilities, and partial undo coverage.

**Done when:** every implementation ticket has a failing reproduction or a clearly documented contract violation; unsupported exploit claims are excluded from the release-blocking list. The ledger remains the audit trail.

### Phase 1 — Storage, restoration, and approval boundaries

**Main claims:** #1–3, #12, #26, #29, #46, #80–84, #94–95, #104; related spec trust in #35.

1. Introduce a small shared identifier/path boundary for session and agent storage. Reject empty/absolute IDs, separators, traversal, and unexpected ID formats while retaining valid legacy IDs. Verify containment under the owned storage root before read/write/delete. Guard symlinked roots and intermediate directories; a `resolve()` check followed by an unguarded write is insufficient against a path-swap race. Reuse existing safe filesystem helpers where they meet this contract.
2. Replace scratchpad substring deletion with explicit ownership metadata. Only delete a directory created for that run. Do not delete caller-supplied workspaces. Remove unnecessary scratch creation for an already supplied `isolated_cwd`; decide whether the unused `isolate` path is supported or retired in Phase 6.
3. Validate file-history manifests before any restore side effect. Check all restoration and deletion paths, checkpoint ownership/ancestry, and the current symlink state. Use root-relative paths for new manifests and a guarded migration for legacy absolute paths. If approved additional directories are supported, include those explicit roots rather than silently breaking that feature. Reject the whole invalid restore before writing any file.
4. Eliminate ambient auto-approval for missing session IDs. Bind permission tickets and approvals to the owning project, session, invocation, and argument digest; reject missing patterned targets. Make `Approval.request` preserve explicit approval for sandbox escalation, hook approval, and plan approval even under YOLO/AFK.
5. Remove the unmatched-ID `allow` fallback from normal permission dispatch. Audit legacy callers and provide an explicit internal compatibility path only where intentional. Preserve the executor's separate invocation-bound authorization checks; do not replace them with a weaker permission flag.
6. Require terminal extension methods to reference a loaded, authenticated session with a matching workspace. Keep the existing terminal operation catalog, cwd containment, ownership checks, secret scrubbing, and write timeout. Add request-size and terminal-count limits and finite timeout validation. Propagate the owner's sandbox policy into terminal creation; a bounded cwd does not itself sandbox the command.

**Acceptance:** traversal, absolute IDs, symlink swaps, cross-project tickets, missing session IDs, patterned tickets without targets, unknown ACP sessions, and invalid restore manifests fail before side effects. Valid session migration, additional-root access, and ordinary terminal use continue to work.

**Verification:** extend storage/authorization/terminal regressions and add restoration containment cases; retain `test_phase1_authorization.py`, `test_approval_identity.py`, `test_phase3_session_approvals.py`, `test_phase1_terminals.py`, and `test_acp_terminal.py`.

### Phase 2 — Spec validation and one child policy

**Main claims:** #4–8, #18–19, #23–28, #34–37, #44, #53–55, #65–67; backend contract from #10/#49.

1. Validate markdown frontmatter and descriptors into a common typed representation. Reject malformed modes and non-string policy entries; preserve the distinction between `None` (explicit inheritance) and `[]` (deny all). Handle lists/tuples consistently and comma strings consistently for allow/exclude fields. Decide/document whether plain markdown is an explicit agent-file feature; do not accidentally auto-discover ordinary markdown as unrestricted roles.
2. Split execution lifetime (`one-shot`/`continuable`) from permission mode (`read_only`/`general`). Validate finite positive timeouts and iteration limits, nonnegative/positive budget fields according to their contract, and trustworthy depth. Role-resolution exceptions must stop construction or yield an explicit denied policy.
3. Resolve role definitions from the owning project/user roots. Remove the extra process-cwd search when it broadens those roots. Define project versus user precedence and require trust for project-provided role instructions and policy changes.
4. Compute effective capabilities as the intersection of parent restrictions, role restrictions, requested restrictions, and descriptor restrictions, with exclusions applied last. Explicit requested tools must not widen a restrictive role. Preserve intentional namespace wildcards used by external tools; a wildcard is a requested capability, not an authorization bypass. Reject unrestricted wildcards in untrusted role policies unless explicitly authorized.
5. Reuse the existing tool registry/effects policy for canonical names, aliases, mutation classification, and external-tool effects. Use one child policy evaluator for both advertisement and execution; schemas are hints, not enforcement. Evaluate nested child policy in the original project context and reject unresolved/writable children from a read-only parent. Explicitly encode the repository's prohibition on delegation by its discovered read-only roles.
6. Add explicit `mode: read_only` to bundled project reviewer/planner/architect specs. Reject invalid teammate modes instead of converting them to `general`. Require inherited execution context for tool-triggered teammates; trusted standalone use must provide an explicit policy.
7. Define external backend capability support. Pass effective cwd and supported sandbox/tool/approval restrictions through a tested adapter. Reject launches when the backend cannot enforce the requested restriction. Validate backend options and separate prompt data from options using the target CLI's supported syntax; prove it with the actual supported CLI version before declaring flag injection fixed.

**Acceptance:** the same decision is reached at construction, advertisement, and actual dispatch; no override or error can widen parent/role restrictions. Registered aliases and external namespace tools receive consistent policy. Unknown types and invalid budgets are rejected with useful errors. Existing teammate lineage tests remain green.

**Verification:** extend `test_subagent_permissions.py`, `test_subagent_parity.py`, `test_phase2_teammate_context.py`, `test_review_phase4.py`, and descriptor/spec parsing cases. Include runtime dispatch tests, not just schema checks.

### Phase 3 — Agent lifetimes, cancellation, and bounded state

**Main claims:** #11, #13–16, #20–22, #29–33, #38–43, #45, #47–48, #51, #56, #86, #97–102.

1. Use full-entropy agent/run IDs and reject duplicate registration. Validate quota before publishing/registering a child. Index immutable lineage directly instead of repeatedly scanning or guessing handle IDs. Add visited sets/iterative traversal for corrupted trees.
2. Define one terminal-status set and one disposal path shared by registry eviction, waits, persistence, and UI. Include refusal, iteration exhaustion, and budget exhaustion. Keep continuable turn settlement separate from worker disposal. Preserve failure/timeout status when the idle TTL expires; clear stale turn results when a follow-up starts.
3. Use a bounded deque/queue for steering, with maximum message size and explicit overflow behavior. Make activation, interrupt, kill, inbox drain, and parking a single documented state machine. Reproduce activation-boundary races before changing event lifetimes; do not clear an abort event still observed by an old provider worker.
4. Document event-loop ownership of registries/controllers/mailboxes. Marshal cross-thread mutations onto that loop. Use locks only for actually shared synchronous state and atomic durable read-modify-write operations; do not add blanket locks around every dictionary. Give parent notices a supported sync/async contract and append sequence numbers under the owning store/manager's synchronization.
5. Stop provider work cooperatively: track and close the response/transport, check cancellation before retries and stream callbacks, bound connection/read deadlines, and prevent late callbacks from mutating a settled conversation. Prefer cancellable async transport where the provider supports it. Cancelling `to_thread` does not terminate the underlying thread or guarantee zero remote billing.
6. Interrupt owning turn tasks as well as setting the controller where immediate cancellation is required, then await owned cleanup. Keep the executor's lock transfer until the real synchronous handler finishes; add cancellation checks before irreversible writes and expose incomplete settlement honestly. Do not start a conflicting follow-up while the previous mutation is unsettled.
7. Consume spawn/start hook outcomes and distinguish deny/stop from advisory errors. Use one end-to-end deadline with bounded hook cleanup rather than granting each hook and execution a fresh full timeout. Initialize cleanup state before imports and preserve cancellation semantics in parallel aggregation.
8. Reap every subprocess on timeout/cancel, drain pipes with bounded buffers, and cap external output. Reuse process helpers where compatible. Missing `wait()` in the git helper is a cleanup gap; do not claim OS zombies without demonstrating it.
9. Define observer versus durable-recorder wire failure behavior. Disconnect/report a slow UI subscriber without losing durable records or crashing unrelated turns. Bound cross-thread publish acknowledgment and emitter shutdown. Avoid silently dropping lossless events by broadly catching overflow.
10. Reconcile persisted child states on startup against actual owned workers/jobs. The stale-foreground helper currently has no production call site; implement and invoke recovery for both foreground and background records with ownership awareness.

**Acceptance:** controlled interrupt/send/kill/timeout schedules preserve history and status; owned tasks and processes settle without affecting sibling projects; all terminal statuses are reclaimable; queue/state sizes remain bounded; late provider output cannot alter a new activation. Wire failures are observable and their effect matches the subscriber type.

**Verification:** keep `test_subagent_parity.py`, `test_phase2_child_lifecycle.py`, `test_phase1_cancellation.py`, `test_phase2_runtime_ownership.py`, `test_cli_subagent_process.py`, `test_background_wire.py`, and wire framing tests. Add repeated spawn/settle/dispose and blocked-stream tests with explicit resource-count assertions.

### Phase 4 — Team ownership and event-driven coordination

**Main claims:** #58–62, #64–79; #63 is a protected behavior to preserve.

1. Park reusable workers on task/message events instead of polling at 10 Hz. Define an idle retention limit and an explicit async teardown that cancels and awaits workers, unregisters mailboxes/topics, removes execution contexts, and removes or bounds retained teammate/task records.
2. Make project/root-session scope mandatory at public coordination boundaries, including task-board mutations and dependency validation. Reject scope-less model-originated operations. Resolve names within scope, enforce unique names within a team, and use stable IDs internally.
3. Replace duplicate active mailbox/inbox/unread delivery state with one delivery record and explicit cursors for worker consumption and user acknowledgment. Keep a bounded display history if the UI needs it. A worker's consumption and a user's read action may intentionally differ; encode that contract rather than clearing everything indiscriminately.
4. Make waits cursor/event based with monotonic deadlines and validated target types. Unknown targets return an error consistently in the manager and handler. Include all terminal result reasons, and preserve the chosen all-target/first-target wait contract.
5. Use priority plus stable sequence ordering for runnable tasks. Claim pending tasks atomically with revision/owner validation; distinguish owned in-progress recovery from new execution. Replace recursive cycle checks with an iterative traversal and add DAG size bounds.
6. Put finite enqueue budgets and explicit overflow results on async fan-out APIs. Snapshot recipient collections before awaiting, so registration changes cannot invalidate iteration. Use sequence ordering for equal-priority mailbox messages; add completion accounting only if queue `join` is part of the supported API.
7. Treat worker-start failure as a failed spawn and roll back registration. Preserve the original execution context and ancestry-derived depth that already pass regressions. Include ownership in persisted representations; an LLM-facing `to_dict` can omit internal scope if it is explicitly a presentation view.

**Acceptance:** idle workers cease polling, teardown returns resource counts to baseline, duplicates cannot misroute work, full/slow mailboxes cannot stall healthy recipients indefinitely, invalid/deep graphs return useful errors, and nested teammates still obey inherited quotas.

**Verification:** extend `test_orchestration.py`, `test_phase2_teammate_context.py`, runtime ownership tests, and targeted manager/mailbox/task-board cases. Test two teams with identical display names, one full recipient, revision contention, startup rollback, and large/deep DAGs.

### Phase 5 — Session history and protocol correctness

**Main claims:** #17/#50/#52, #57, #85–93, #96, #103–105.

1. Pair every begun turn with exactly one end event, including pre-loop interruption, resumed pending-tool interruption, permission waits, errors, and cancellation. Use a guarded finalization path; avoid adding duplicate end events to paths already covered.
2. On cancellation, settle/pause pending goal requests using the goal runner's cancellation contract. Do not blindly move `goal_runner.drain()` into `finally`: `drain()` can start new goal work, which is the wrong action during cancellation.
3. Generate every synthetic tool failure through one constructor with `ok: false`, canonical tool name, call ID, error code, and consistent UI/protocol classification. Repair both legacy message rows and event rows without losing sequence/pairing invariants.
4. Make fork targets explicit: sequence number, message ID, or a separately named legacy index. Reject missing/negative/unknown targets before creating history, goals, or file-history branches. Rewrite known ownership fields using event schemas, preserve provenance intentionally, and rebind approvals/runtime handles rather than recursively replacing every occurrence of a source ID.
5. Make D-Mail rewind transactional: retain the staged directive until revert/checkpoint succeeds, restore it on failure, and prevent duplicate consumption during retry.
6. Guard corrupt summary replacement graphs with visited sets and bounded traversal. Budget child summarization input and preserve complete tool groups, original task, newest steering, and pinned context. For partial compaction, define which earlier context is essential before expanding the input; blindly including the entire prefix could defeat the budget.
7. Keep role instructions at their intended trust level. Encode git metadata, steering, and child output as bounded provenance-tagged data; escape delimiters/control sequences in presentation. Delimiters reduce ambiguity but do not prevent prompt injection; tool permissions remain the hard boundary. Reuse output sanitization and credential redaction where appropriate.
8. Verify ACP session/workspace binding on resume, retain its existing successful binding restoration, and report corrupt/missing binding rather than silently creating a fresh conversation. A requested ACP fork must clone source history or fail and remove partial artifacts; it must not report a history-less fork as success.
9. Inventory mutation/checkpoint coverage using handler callbacks and tool metadata. File handlers already invoke pre/post callbacks, so the narrow `target_paths` list does not prove all editor changes are untracked. Cover actual gaps for shell and multi-file changes; either implement tracking or explicitly report unsupported undo coverage. Do not claim full rollback for arbitrary shell commands.

**Acceptance:** history remains paired and ordered after interrupted tools, migration, compaction, and fork; invalid forks create no artifacts; D-Mail survives a failed revert; resume retains the correct source; corrupted summaries terminate promptly; failed operations render as errors; checkpoint coverage is truthful.

**Verification:** extend `test_session_storage.py`, `test_compaction_conversion.py`, `test_session_engine.py`, `test_protocol_parity_hardening.py`, `test_acp_server.py`, and goal/file-history tests. Use fixtures with noncontiguous seqs, nested ownership metadata, unknown IDs, corrupt replacement graphs, and failed reverts.

### Phase 6 — Remove demonstrated bloat

Cleanup should reduce duplicate ownership and misleading APIs, not remove active features merely because their files are named `legacy`, `soul`, or `teams`.

| Candidate | Evidence from this checkout | Planned action and condition |
| --- | --- | --- |
| `SubagentQuotaConfig` | Referenced only by its definition and package export in production source; construction uses orchestration defaults and `SubAgentSpec` instead | Deprecate/remove unused parallel quota representation after checking SDK/public import compatibility; retain one quota source |
| `SubAgentSpec.seed_events` | No production reader found | Remove unused field if it is not an external serialization contract; retain the active `seed_messages` fork path |
| Descriptor `agent_provider`, `agent_model`, `persona`, lifetime/provider fields | Parsing/serialization exists; runtime uses `spec.provider`, `spec.model`, `spec.mode`, `spec.continuable`; the active descriptor restriction is `tool_filter` | Decide which metadata is a compatibility contract; consolidate operational fields or remove unsupported knobs with migration. Do not imply they already control runtime behavior |
| Scratch creation for explicit `isolated_cwd` and dynamic `isolate` handling | Explicit cwd causes an extra scratchpad to be created; `isolate` is read with `getattr` but is not a declared spec field | Use one explicit workspace strategy; remove unused allocation and retire undeclared feature plumbing if no supported caller needs it |
| Three policy paths | Tool alias table, child advertisement/execution predicates, and executor effects policy overlap | Reuse the existing catalog/policy as the authority; retain enforcement at actual dispatch while removing duplicated decision logic |
| Two active-controller maps | Manager-local map plus module-global map | Consolidate ownership/indexing or give the global map one narrow purpose with deterministic registration/disposal |
| Teammate inbox/unread/mailbox copies | Same messages delivered into lists and the async mailbox | Replace duplicate pending state with bounded history plus explicit consumption/read cursors |
| `mark_stale_foreground_failed` | No production call site found | Replace and wire up complete recovery in Phase 3, or remove the unused helper if recovery lives elsewhere |
| Guessed inbox handle fallbacks | Runner tries agent ID, task ID, and `agent_<task_id>` | Remove after all launch paths bind one authoritative handle |
| Broad exception swallowing and repeated hook/settlement plumbing | Repeated in builder, runner, team manager | Replace with small shared lifecycle/error helpers only after contract tests exist; keep intentionally advisory failures explicit |

**Keep:** `SessionSoul` adapter, active `ToolExecutor` and its compatibility namespace, external namespace wildcard support, `_resolve_subagent_cwd` (used by execution hooks), `_parse_markdown_agent_file` (used by `agentspec.py`), file-history/fork support, and runtime ownership tests. These have real callers or contracts. Do not delete test files merely because their names describe older phases.

Do not delete user-owned `.verification` evidence, uncommitted work, or retained session artifacts as part of source cleanup. Avoid a new generic framework, a parallel policy registry, and file splitting solely to reduce line counts. Each removal must have a reference/public-API check and measurable simplification such as fewer duplicate fields, fewer retained resources, or one decision authority.

**Acceptance:** all deletion candidates have recorded caller/export/migration analysis; behavior and public imports stay compatible or are explicitly migrated; redundant active state is removed; regressions cover the retained contract. Python remains 3.12+ without PEP 695 syntax; the application remains terminal/daemon only.

### Phase 7 — Verification and documentation

1. Run each affected pytest file independently in the project virtualenv with isolated HOME/XDG directories and scrubbed credentials. Use the shared runner's environment rather than a developer's live configuration.
2. After the phases land, run `.venv/bin/python scripts/verification.py test --suite all`, which isolates files into separate processes. Complete format/lint, type-budget/mypy, dependency, installed real-SDK Jev, CLI, and release gates defined in `docs/verification.md`; install the declared `[dev,jev]` extras if missing.
3. Add integration checks spanning real manager → child/team → executor → wire → persistence, using fake providers. Measure bounded retained state and idle worker wakeups under repeated create/settle/dispose cycles. Add supported-platform subprocess/PTY tests; record platform limitations rather than treating mock tests as live compatibility certification.
4. Update role/policy, backend capability, cancellation, recovery, fork, and undo documentation to match actual behavior. Close each ledger entry with its regression and final disposition.

**Done when:** all verified high-impact tickets have regressions, focused and shared gates pass on the actual final tree, supported external backends have versioned capability checks, and no R finding has been silently relabeled fixed.

## Verification performed for this plan

The repository's `scripts.verification.run_tests` was used with an in-memory file selection; no runner source was edited. Each file ran in its own subprocess with fresh HOME and scrubbed credentials.

| File | Passed | Failed/errors/skipped |
| --- | ---: | ---: |
| `tests/test_subagent_permissions.py` | 11 | 0 |
| `tests/test_subagent_parity.py` | 18 | 0 |
| `tests/test_phase2_teammate_context.py` | 14 | 0 |
| `tests/test_phase2_child_lifecycle.py` | 5 | 0 |
| `tests/test_phase1_cancellation.py` | 12 | 0 |
| `tests/test_protocol_parity_hardening.py` | 17 | 0 |
| `tests/test_acp_terminal.py` | 9 | 0 |
| `tests/test_phase1_authorization.py` | 37 | 0 |
| **Total** | **123** | **0** |

These are existing-contract checks, not proof that all reported defects are fixed. The passing child cancellation, ancestry, and authorization tests are particularly relevant to correcting overstated claims.

Temporary probes established: store traversal escapes the instance root; frontmatter-less specs default to general/inherited tools; invalid spec mode is retained; comma exclusions remain one string; `*` permits write; tuple descriptor allow is discarded; non-string policy entries raise `AttributeError`; role-resolution exceptions can retain general/inherited tools; cleanup attempts removal of an unrelated directory under a temp-containing path; budget-exceeded handles are not evicted; invalid teammate mode becomes general; direct unknown-agent wait succeeds; deep DAG traversal raises `RecursionError`; unmatched permission IDs allow; patterned tickets without a target validate; low-level YOLO approval approves sandbox escalation. Destructive cleanup was mocked and traversal was checked without deleting outside the temporary workspace.

## Claim ledger — all 105 original claims

Locations refer to the reviewed working-tree functions/lines. Phase 0 applies to every R entry before a change is attempted.

### A — Subagents, originally critical/high

| # | Verdict | Evidence and correction | Phase |
| ---: | :---: | --- | :---: |
| 1 | V | `store.py:80–84,213–218`: unchecked ID join escapes the root; recursive deletion uses that path. Primitive verified; ordinary launch IDs are generated and no production delete caller was found, so arbitrary remote deletion is not demonstrated. | 1 |
| 2 | V | `builder.py:142–159`: scratch session ID is joined without validation/containment. Normal runner IDs are generated; malicious direct input still crosses the boundary. | 1 |
| 3 | V | `builder.py:162–173`: substring `tmp`/`temp` controls deletion; project scratch outside those substrings is never pruned. Mocked probe confirmed an unrelated directory qualifies. | 1/6 |
| 4 | P | `registry.py:75–96`: missing tools/mode yields general with inherited tools. Behavior verified; missing fields may intentionally represent a general role, so define the trust/default contract before changing all specs. | 2 |
| 5 | V | `registry.py:84–86`: invalid explicit mode is retained. Probe parsed `banana`; validate before construction and use. | 2 |
| 6 | P | `registry.py:303–321`: wildcard matching is real. Parent restriction intersection and later permissions still apply; wildcard support is intentional for external namespaces. A single spec is not proven to escape all authorization. | 2 |
| 7 | V | `builder.py:236–276`: role-resolution exception logs and leaves policy unchanged. Probe retained general mode and `allowed_tools=None`. Default read-only mode does not repair an explicitly general spec. | 2 |
| 8 | V | `builder.py:98–104`: tuple allow becomes `None`; non-string entries can later crash policy matching. Probe confirmed lost tuple restriction. | 2 |
| 9 | P | `runner.py:998–1004` uses `isolated_cwd` for executor root, contradicting blanket isolation bypass. Scratch-based dynamic `isolate` can use a different hook cwd; explicit cwd also allocates unused scratch. | 2/6 |
| 10 | P | `runner.py:484–562`: external runs use project root and do not translate child capabilities. Backend argv concerns duplicate #49; extra args are not model-supplied in this runner path. Sandbox escape needs a backend-specific reproduction. | 2 |
| 11 | P | `runner.py:295–323`, `core.py:143–145`: registration precedes depth check and overwrites duplicate IDs. Rejected foreground runs settle as failed; background live-agent cap is checked before registration. “Live ghost” is overstated. | 3 |
| 12 | V | `runner.py:232–263`, `manager.py:462–463`: parent ID joins storage without containment; `OSError` permits loss of durable setup. Validate IDs and surface persistence failure. | 1 |
| 13 | R | `runner.py:762–767`: per-activation event replacement is intentional to preserve old thread abort lifetimes. A lost boundary cancellation needs a controlled schedule; current in-flight cancellation tests pass. | 3 |
| 14 | P | `runner.py:1239`, `core.py:367–404`: thread calls remain non-preemptible and update checkpoint data, but `_run_abortable` races abort and streaming polls/closes on cancellation. “Only loop-top polling” is false. | 3 |
| 15 | P | `runner.py:831–859`: TTL sets completed and settlement notice is worker-wide. Waiter clears at activation start, not immediately before parking, so the described check-then-park race is not established. Background wrapper later reapplies result status. | 3 |
| 16 | V | `core.py:284–294` omits refusal, max_iterations, budget_exceeded; runner returns those statuses. Probe confirmed budget-exceeded retention. Shared waits omit them too. | 3 |
| 17 | P | `runner.py:1015–1036,1104–1108`: trusted role instructions and context enter prompts, with some labels/delimiters. Hostile git/child/context text deserves provenance and limits, but role instructions are intentionally instructions. | 5 |
| 18 | P | `execution.py:40–59`: some comparisons are exact; known write mutation metadata also denies. Tested capitalized names are unregistered, so a callable casing bypass was not demonstrated. Unify canonical/effects policy. | 2 |
| 19 | N | `execution.py:54`, `runner.py:1594,1626`: spawn_teammate is blocked in read-only paths and recursion-filtered. AskUserQuestion has an exact check, but tool exposure is also constructed with noninteractive settings. | 2 |
| 20 | P | `git_context.py:87–93`: kill is not followed by awaited wait/communicate; cleanup gap verified. Asyncio process reaping means “zombies” requires runtime evidence. | 3 |
| 21 | R | `core.py:139–294` has no locks, but its synchronous operations normally run without yielding on the event loop. Prove cross-thread/multiloop access before claiming a concrete send/evict race. | 3 |
| 22 | P | `core.py:35–56`: sink is called synchronously and async return is not awaited. Actual registered sinks are synchronous; lock necessity and live async-sink loss are not established. Define the supported callback contract. | 3 |

### B — Subagents, originally medium/low

| # | Verdict | Evidence and correction | Phase |
| ---: | :---: | --- | :---: |
| 23 | V | `registry.py:41–46,61–96`: no frontmatter becomes a live general role. Probe confirmed behavior; restrict auto-discovery or explicitly document trusted plain markdown. | 2 |
| 24 | P | `registry.py:87–92`: narrow read-only inference is real. Think/media reads and session-state tools need explicit effects/defaults; an omitted inference entry does not by itself grant unrestricted tools beyond the allowlist. | 2 |
| 25 | V | `registry.py:101–109`: comma-string exclude list is one entry; probe confirmed `('write,edit',)`. | 2 |
| 26 | V | `registry.py:64,163–170`: spec reads follow symlinks and have no size cap. Set trusted-root and maximum-size rules, including explicit agent-file behavior. | 1/2 |
| 27 | P | `registry.py:291–292`: requested allowlist wins; `SubAgentSpec` also skips role tools when explicitly supplied. Parent intersection still applies. Restrictive role tools should also be intersected. | 2 |
| 28 | V | `registry.py:313`: non-string runtime allow entries raise `AttributeError`; reproduced. Parsed markdown coerces strings, but descriptors/direct/team inputs are weaker. | 2 |
| 29 | P | `core.py:59–87`: durable notice computes sequence by read-then-append outside manager seq locking. Path construction is also unchecked. Duplicate sequence numbers require concurrent writers, not simply a notice call. | 1/3 |
| 30 | V | `core.py:163–198,238–250`: recursive tree operations have no visited set/depth bound. Corrupt/cyclic lineage can recurse indefinitely until `RecursionError`. | 3 |
| 31 | V | `core.py:200–214`: inbox is unbounded; wakeup changes terminal status while old result/report remains. Define per-turn result and queue limits. | 3 |
| 32 | N | `core.py:208–213` changes a parked interrupted handle to running on send. `test_interrupted_continuable_history_can_resume` passes; permanently pinned interruption is not the ordinary follow-up behavior. | 3 |
| 33 | P | `core.py:234–235,272–282`: generic cancel/evict do not drain tasks. Manager disposal already drains/evicts owned handles, and foreground settlement clears its invoking task. Direct eviction still needs a lifecycle contract. | 3 |
| 34 | V | `builder.py:86–88`: descriptor mode accepts both lifetime and permission vocabulary. Split concepts and validate migration. | 2/6 |
| 35 | P | `builder.py:240` prefers project_root, with isolated_cwd fallback; role discovery additionally scans process cwd. Owning-root isolation tests pass for supplied project roots. Trust/cwd fallback remains too broad. | 2 |
| 36 | P | `builder.py:322–374` admits nonfinite/negative numeric values; depth may be supplied by trusted internal calls. The configured/inherited max_depth is now clamped at 415–420. Model depth/max-depth override bypass is not shown. | 2 |
| 37 | P | `builder.py:347–352`: parent lookup scans run IDs. Run IDs contain the truncated parent prefix **and task ID**, so prefix truncation alone does not cause collisions. Short task-ID collision remains a risk. | 3/6 |
| 38 | P | `runner.py:46,172,225–230`: dual controller maps exist. Both one-shot and continuable finally blocks remove entries; not clearing them immediately on cancel preserves active ownership. Unconditional map leak is not supported. | 3/6 |
| 39 | R | `runner.py:205–223`: cancellation snapshots descendants. Need a test where spawning after the snapshot bypasses the parent's already-set cancellation, rather than assuming a race. | 3 |
| 40 | V | `runner.py:450–482`: broad exceptions are swallowed and returned hook deny/stop outcomes are ignored; hooks/execution each receive a full timeout. Define enforceable versus advisory outcomes. | 3 |
| 41 | P | `runner.py:646–666,860–873`: stop hooks are unbounded during cleanup. A pre-assignment import failure could leave parent_sid unbound, but broad exception handling swallows that cleanup error; the claimed uncaught crash is overstated. | 3 |
| 42 | P | `runner.py:688–702`: gathered child BaseException becomes failed with raw exception text. Parent gather cancellation itself propagates. Preserve child cancellation status and redact error details where needed. | 3 |
| 43 | P | `runner.py:1087–1108`: list pop(0), unbounded steering, and guessed handle fallback are real. Cross-agent theft through public routing is not demonstrated; active paths already bind spec.handle. | 3/6 |
| 44 | P | `execution.py:63–80`: writable-child lookup omits original project and unresolved definitions pass this helper. Builder later denies unknown roles; cross-project lookup/policy consistency still needs consolidation. | 2 |
| 45 | V | `store.py:138–162`: atomic file replacement does not serialize the surrounding read-modify-write. Concurrent state/status updates can overwrite one another. | 3 |
| 46 | V | `store.py:183–211,236–259`: directory listing follows symlinks; JSON root shape is not checked before `.get`, and AttributeError is not caught. Validate records and paths. | 1/3 |
| 47 | P | `store.py:220–233`: foreground-only recovery excludes background records; helper has no production callers. The larger defect is unwired/undefined recovery, not only one missing status. | 3/6 |
| 48 | P | `backends/base.py:62–65,99–111`, `process.py`: timeout/cancel kills group and awaits wait with a two-second bound, but interrupted pipe consumption is not explicitly drained. Large-output deadlock requires reproduction. | 3 |
| 49 | P | `backends/codex.py:44–50`, `claude_code.py:44–53`: raw argv construction/extra options need validation. Claude prompt is the value of `-p`, unlike Codex positional prompt. No shell-string execution; prove target-parser flag handling. | 2 |
| 50 | P | `git_context.py:43–72,101–119`: branch/commit/file text is data inside git-context; remote credentials/hosts are already sanitized. Escape data delimiters and cap fields; blanket “raw remote” claim is inaccurate. | 5 |
| 51 | P | `streaming.py:40–70`: close-check/forwarding and observer exceptions can cause inconsistent delivery. Normal sends marshal onto owning loop; demonstrate close/observer failure schedules before alleging thread races. | 3 |
| 52 | P | `output.py:68–87`: child markdown is inserted into result presentation. Formatting/context ambiguity is real; structured tool-message boundaries and executor sanitization also exist. Not a demonstrated authorization bypass. | 5 |
| 53 | P | Project architect/planner/security-reviewer specs omit explicit mode but read/grep/glob infer read_only correctly. Add explicit mode for clarity; current inference is not already granting write access. | 2 |
| 54 | N | `soul/agent.py` is a documented SessionSoul adapter. Role policy belongs elsewhere. Duplicate policy lists are a cleanup target independently of this naming-based allegation. | 2/6 |
| 55 | N | `approval.py:792–806` defaults missing permissions; probe returned one row for a valid call, and empty-parent-settings regressions pass. Malformed settings/calls should still receive validation. | 2 |
| 56 | P | `core.py:352–361`: substring-based stream_options fallback may misclassify errors. It deliberately retries once without the option. Use structured capability/error detection rather than claiming all such retries are wrong. | 3 |
| 57 | P | `compaction.py:45–53` serializes full history without an explicit input budget. The `messages != original` check is live: original is a deepcopy captured before an awaited completion. | 5 |

### C — Teams / swarm

| # | Verdict | Evidence and correction | Phase |
| ---: | :---: | --- | :---: |
| 58 | P | `manager.py:557–637`: completed/failed workers keep polling every 0.1 s. Reusable workers are intentional; unnecessary wakeups and undefined retention are the defect, not necessarily completion requiring immediate exit. | 4 |
| 59 | P | `manager.py:670–695`: global cancel clears tasks only, retaining teammate/context/mailbox state. Scoped cancel removes these; reset replaces the manager. Define cancel versus dispose and implement complete teardown. | 4 |
| 60 | P | `mailbox.py:59–79,174–212`: default async backpressure can wait indefinitely. Production TeamManager delivery uses send_nowait, so the claimed normal fan-out hang does not follow. Bound async public APIs. | 4 |
| 61 | P | Direct `TeamManager.wait_agent` returns success for unknown target; probe reproduced it. `tools.py:345–373` rejects unknown scoped targets before that call. Internal API consistency issue remains. | 4 |
| 62 | P | Worker consumes mailbox without updating unread list. Worker delivery and user read acknowledgment may intentionally differ; tests/contracts must decide. Current duplicate state obscures message-wait meaning. | 4 |
| 63 | N | `builder.py:344–362` derives child depth when None and clamps parent quota. `test_nested_teammate_inherits_lineage_and_enforces_parent_depth_cap` passes. | 2/4 |
| 64 | V | `manager.py:255–261`: auto-start without a running loop leaves a registered teammate/mailbox but no worker. Roll back or require explicit manual-start mode. | 4 |
| 65 | V | `manager.py:241`: invalid mode becomes general; probe reproduced it. Builder's safer coercion does not undo the manager's conversion. | 2 |
| 66 | P | `manager.py:513–518`: trusted fallback context has no parent policy. Normal tool-spawn path stores/inherits execution context and policy, proven by passing tests. Require explicit standalone restrictions. | 2 |
| 67 | P | `tools.py:27–31`: handler accepts list entries/modes too loosely. Executor schema validation is an additional boundary; direct handlers/manager still need consistent validation. | 2 |
| 68 | P | `manager.py:265–272`: duplicate names and global name lookup are ambiguous. Most public tool routes resolve scope to stable IDs first; direct manager calls and duplicate same-team names remain problematic. | 4 |
| 69 | P | `manager.py:286,485`: unscoped direct objects share None scope. Public handlers derive a project/root-session tuple. Enforce scope at manager/board boundaries without assuming every public call leaks. | 4 |
| 70 | P | `manager.py:475–493,568–574`: insertion order beats priority; in_progress tasks are runnable and worker claim omits revision. Task board supports CAS, and synchronous select/update does not itself yield. Duplicate execution needs contention/ambiguous assignment reproduction. | 4 |
| 71 | P | `manager.py:639–668`: scoped cancel skips missing owner roots/session IDs. Normal stored-context teammates have both; legacy/standalone workers need explicit ownership and disposal. | 4 |
| 72 | P | `models.py:51–65,90–105`: presentation dictionaries omit owner_scope. No evidence these dictionaries are used to restore owned runtime state; distinguish persistence from model-facing views. | 4 |
| 73 | V | `manager.py:48–83,100–134`: direct task-board dependencies validate existence/cycles but not owner scope. Tools enforce scope separately; move invariant to board as well. | 4 |
| 74 | V | `deadlock.py:26–46`: a 1,200-node chain raised RecursionError. Create handler does not catch it; update handler has a broad catch, so the review overstates both being uncaught. | 4 |
| 75 | V | `tools.py:346`: converting a noniterable integer target to list raises TypeError in direct handler use. Add target-type validation; public schema may already reject it. | 4 |
| 76 | V | `mailbox.py:70,96`: ordering compares wall-clock arrival before sequence, so backward clock changes reorder equal priority. Sequence is the cleaner FIFO authority. | 4 |
| 77 | P | `mailbox.py:111–138`: unfinished task accounting is never decremented. No production queue-join caller found; private join would hang. Define whether processing acknowledgment is supported. | 4 |
| 78 | V | `manager.py:383,461,465`: wait duration uses wall clock; use monotonic for elapsed/deadline calculation. | 4 |
| 79 | P | `tools.py:329–339`: handler clamps nonfinite timeout and invalid wait_for to defaults. Bounds exist; silently hiding invalid input is an API quality issue, not a demonstrated privilege bypass. | 4 |

### D — Session / approval / fork / ACP

| # | Verdict | Evidence and correction | Phase |
| ---: | :---: | --- | :---: |
| 80 | P | `approval.py:1002–1022`: unmatched ID returns allow; reproduced. Separate executor definition, permission, and invocation-bound grant checks prevent inferring arbitrary execution from this helper alone. Remove risky fallback after caller audit. | 1 |
| 81 | V | `approval.py:1243–1251`: low-level YOLO Approval.request returns approved for sandbox-escalation; reproduced. Higher-level plans preserve explicit asks and executor grants are separate. Close this inconsistent lower-level boundary. | 1 |
| 82 | P | `session/approval.py:39–42,78–85`: controller membership allows manager-wide autoapprove lookup for inactive sessions. Whether manager mode is intentionally global requires contract tests; not proof of unrelated-manager takeover. | 1 |
| 83 | V | `approval.py:1264–1272`: missing session ID consults any registered autoapproving manager. Remove ambient cross-owner fallback. | 1 |
| 84 | V | `approval.py:208–229,288–303`, `manager.py:566–567`: empty-session tickets match other sessions and patterned tickets validate without a target; latter reproduced. Require complete ownership/target context. | 1 |
| 85 | V | `coderaisoul.py:402–403,448–449,467–477`: these returns omit turn-end emission. Other waiting/interruption paths emit it; centralize exactly-once finalization. | 5 |
| 86 | P | `manager.py:1819–1830`: cancellation skips post-finally drain. GoalRunner.interrupt already clears pending requests on normal interrupt; raw task cancellation can differ. Settle/pause requests, do not start new goals in cancellation cleanup. | 3/5 |
| 87 | P | `compaction.py:452`: only preceding system messages plus target slice go to summary. Partial-range summarization intentionally excludes some earlier history; establish lost essential context with a fixture before widening input. | 5 |
| 88 | V | `session/log.py:54–62`: cyclic summary replacement chains loop without visited set. Guard malformed persisted history before deriving visible messages. | 5 |
| 89 | V | `session/store.py:219–234`, `manager.py:2149–2154,1244–1254`: synthetic error payload lacks ok/name; UI structured-error branch requires ok, otherwise reports is_error=False. Use canonical failure constructor. | 5 |
| 90 | V | `fork_ops.py:53–69`: mixed seq/index logic and fallback index slicing give ambiguous integer fork points. Reject invalid seqs or use an explicit index API. | 5 |
| 91 | V | `fork_ops.py:70–78`: absent string message ID copies all history without signaling no match. Validate fork point before creating artifacts. | 5 |
| 92 | P | `fork_ops.py:91–102`: only top-level ownership fields rewritten. Nested fields remain; some may be intentional provenance. Define schema-specific ownership rewrites and runtime rebindings. | 5 |
| 93 | V | `manager.py:1173–1176`, `coderaisoul.py:93–100`: D-Mail is popped before awaited revert, so revert failure loses staged directive. Use transactional consumption. | 5 |
| 94 | P | `acp/server.py:729–755`: arbitrary nonempty ID creates bridge even when not loaded. Auth and terminal operation catalog do apply; bypassing the static extension allowlist alone is not the defect. Bind bridge to known session/workspace. | 1 |
| 95 | P | `terminal/manager.py:75–99,192–215`: cwd is contained, secrets scrubbed, send has a 10 s deadline. ACP bridge omits sandbox propagation and has no payload cap; original “no containment/unbounded send” claim is inaccurate. | 1 |
| 96 | N | `acp/server.py:349–369` restores stored engine binding and checks same-ID fallback. Resume omits UI replay, which differs from load and does not itself erase engine history. Missing/corrupt binding and cwd mismatch still merit tests. | 5 |
| 97 | V | `wire/__init__.py:103–140`, `utils/broadcast.py:136–155`: overflow raises to producer while soul side catches only shutdown. It is deliberate explicit delivery failure, but needs subscriber-specific turn/recording policy. | 3 |
| 98 | V | `wire/emitter.py:102–116`: cross-thread publisher waits on Future.result without timeout. Loop shutdown/stall can block provider worker indefinitely. | 3 |
| 99 | P | `manager.py:2393–2401`: synchronous provider thread remains non-preemptible. Streaming raises SessionInterrupted and closes when a chunk arrives; callback suppression alone is not the cancellation mechanism. Blocked/no-chunk reads remain the gap. | 3 |
| 100 | P | `executor.py:861–916`: sync worker can continue after caller cancellation, but cancellation_event is set and locks remain with owned cleanup. File helpers also check cancellation before writes. Test the remaining window/settlement, preserve lock safety. | 3 |
| 101 | V | `manager.py:961–992` signals controllers/processes but does not cancel tracked turn tasks; disposal does at 2887–2890. Prompt cancellation can therefore depend on provider progress. Coordinate signal/cancel/drain. | 3 |
| 102 | P | `acp/engine.py:195–202` direct interrupt cancels inner drive, while cancel watcher also cancels _run_task and close drains it. Only direct-interrupt wait states need additional reproduction. | 3 |
| 103 | P | `manager.py:2010–2020` post-dispatch path map covers named single-file tools only. `tools/file/utils.py:149+` invokes mutation callbacks, so str_replace_editor/multi-file omissions are not proven solely by this list. Shell undo coverage still needs inventory. | 5 |
| 104 | V | `file_history.py:157,219–251`: manifests retain resolved absolute paths and restore/delete them without workspace containment or symlink revalidation. Check checkpoint ownership and all destinations before any restore side effect. | 1 |
| 105 | V | `acp/server.py:496–512`: missing source binding or failed-to-produce fork ID logs a warning and returns a new history-less ACP fork. Fail transactionally and clean partial state. | 5 |

## Delivery rule

Implementation was separately requested and delivered. Keep future changes small and reviewable. Each change must identify the original claim numbers, the actual contract it fixes, meaningful regressions, and any migration impact. Retain corrected/unsupported findings in the ledger to prevent future agents from rebuilding the same false-positive backlog.

# CoderAI Competitive Audit Remediation Plan

This plan translates the competitive product audit into a sequenced product
program. The ordering is based on user trust and task-completion impact rather
than implementation ease.

## Outcome

CoderAI should own the whole engineering workflow: understand the objective,
plan when needed, act within an explicit permission boundary, collect evidence,
recover strategically, and report a truthful completion state.

The primary product metric is **verified task completion rate**. Supporting
metrics are false-success rate, verification-after-mutation rate, recovery
success, tool-schema tokens, approval burden, and task cost/latency.

## Priority Map

| Order | Program | Audit findings | Why now |
|---:|---|---|---|
| 1 | Completion contract and engineering-state ledger | 1, 8, 13 | Prevents false success and supplies durable state for every later workflow. |
| 2 | Real plan mode | 2 | Converts ambiguous work into reviewable scope, criteria, risks, and checks. |
| 3 | Run/session isolation and transactional workspace | 3, 10, 12 | Makes concurrency and rewind safe before multi-agent behavior expands. |
| 4 | Installed-product and release integrity | 4, 14 | Ensures the wheel contains and verifies every advertised capability. |
| 5 | Progressive capability routing and code intelligence | 5, 6 | Improves accuracy, latency, and repository understanding. |
| 6 | Strategic recovery and durable task state | 7, 8 | Makes long tasks resumable and changes recovery strategy by failure type. |
| 7 | Scoped memory and permission profiles | 9, 11 | Enables useful continuity and safe automation without global pollution or YOLO. |
| 8 | Product polish and extension discipline | 15–17 | Simplifies setup, unifies extensions, and removes architecture/documentation drift. |

## Milestone 1 — Completion Contract

Status: **first runtime slice and durable objective-ledger persistence implemented**.

The runtime now creates an `ObjectiveState` for every turn and records:

- The original objective and acceptance criterion.
- Workspace mutations and changed artifacts.
- Post-edit file inspection.
- Successful verification performed after the latest mutation.
- Latest failed tool outcomes.
- Task checklist items touched during the turn.
- Required and completed checks plus unresolved risks.

When a model returns no tool calls, that response is treated as a completion
proposal. For workspace-changing turns, the deterministic gate requires fresh
inspection and verification. It gives the agent one evidence-focused retry by
default. If evidence is still missing, the turn ends as `unverified` with
`success=false`; it is no longer silently reported as successful.

The objective ledger is now independent of the chat transcript. Every turn
gets a stable objective ID and an owner-only, atomically replaced record under
`~/.coderAI/objectives/<session-id>/`. Initial state, tool evidence, mutation
and inspection clocks, checks, completion decisions, plan linkage, open work,
and unresolved risks are persisted after each state transition. The store is
bound to the immutable run context and rejects cross-session, cross-workspace,
unsafe-ID, and symlink-escape access. Session resume restores the latest
objective with its deterministic completion clocks intact; transcript
compaction and rewind cannot remove the separate ledger.

### Follow-up slices

1. Add explicit acceptance-criterion editing and `verified`, `reasoned`,
   `blocked`, and `not-applicable` evidence outcomes.
2. Capture pre/post workspace hashes so shell-driven mutations enter the ledger.
3. Associate checks with affected artifacts and invalidate only impacted checks.
4. Surface objective, evidence, open work, and completion status in the TUI.
5. Add fixture-repository evals that grade final repository state and false success.

### Exit criteria

- No workspace-changing turn can return `success=true` without fresh evidence.
- Failed checks cannot be summarized as success.
- Compaction and resume preserve objective, decisions, mutations, and checks.
- Deterministic evals measure false-success and verified-completion rates.

## Milestone 2 — Real Plan Mode

Status: **core workflow implemented**.

Introduce a `PlanSession` state machine with read-only exploration, structured
questions, editable acceptance criteria, constraints, interfaces, failure
modes, checks, rollout, and explicit approval into an execution run. Link the
approved plan revision to `ObjectiveState`; implementation changes become
visible amendments.

Keep an immediate-execution path for small, low-risk tasks.

The implemented workflow now provides:

- Enforced read-only exploration at both schema-routing and execution layers,
  including hook, MCP autolaunch, and workspace-trust suppression.
- Structured proposals and stable question IDs, choices, and editable answers.
- A reviewable mutable `draft.json` artifact per plan. Applying the validated
  artifact creates a new immutable revision; stale, invalid, symlinked, and
  out-of-project drafts are rejected, and unapplied edits block approval.
- Immutable revision, amendment, approval, and execution-attempt histories
  under `.coderAI/plans/`, with approved snapshot hashes checked before every
  initial or resumed execution.
- TUI plan cards with decision state, choices, answers, artifact paths, and
  direct `/plan answer`, `edit`, `apply`, `approve`, and `resume` actions.
- An execution-only `request_plan_amendment` transition. Divergence records a
  complete replacement revision, stops further mutation, invalidates approval,
  and requires review and reapproval instead of silently changing scope.
- Persisted session-to-plan execution linkage and resumable interrupted
  attempts without relying on transcript context.
- A scriptable `coderAI plan create|show|edit|apply|answer|approve|execute`
  workflow with JSON output and deny-on-mutate execution by default.

### Milestone 2 exit criteria

- Planning cannot execute workspace mutations, project hooks, MCP launchers,
  or workspace-trust actions.
- A user can review and edit the entire plan without asking the model to make
  each change, and every applied change becomes an immutable revision.
- Approval always names and hashes one exact revision; unanswered questions or
  unapplied artifact edits prevent approval.
- Execution divergence cannot silently continue: it creates a visible
  amendment and restores the read-only boundary until reapproval.
- Revision, amendment, approval, execution-attempt, and session linkage state
  survive process restart and can be resumed from the TUI or CLI.
- Deterministic runtime, TUI, CLI, resume, rejection, and security tests cover
  the state machine.

Lower-priority Plan Mode polish remains outside the milestone gate: an
optional full-screen form editor/question picker and NDJSON lifecycle events
for the dedicated `coderAI plan` command. Those do not weaken reviewability,
resumability, approval integrity, or the read-only boundary.

## Milestone 3 — Isolation and Transactions

Status: **in progress; run/session isolation, synchronous transaction ledger,
and mutating-subagent worktree integration implemented**.

Replace ambient `HistoryManager.current_session` with an immutable `RunContext`
carrying run, session, agent, workspace, checkpoint store, and permission
policy. Construct history and backup stores explicitly per session.

Implemented isolation foundation:

- Every `Agent` owns a frozen `RunContext` with a unique run ID, stable
  workspace identity/root, session ID, tracker agent ID, pinned permission
  policy, isolation domain, and session-owned checkpoint/undo store.
- Session creation and resume explicitly bind `.coderAI` recovery operations
  to `~/.coderAI/backups/<session-id>`; backup resolution no longer reads the
  mutable process-wide `HistoryManager.current_session`.
- The tool executor carries the owning run context across concurrent asyncio
  tasks and worker-thread filesystem operations. Parent and child recovery
  ledgers therefore remain distinct even when their tool calls interleave.
- Rewind uses the active agent's explicit checkpoint store. Reopening a
  session deterministically reopens the same ledger, while unsafe store IDs
  and cross-root paths are rejected.
- Deterministic tests cover immutability, nested context restoration,
  concurrent parent/child selection, thread propagation, resume, unsafe IDs,
  and immunity to ambient-history changes.

Implemented transaction-ledger slice:

- Each session owns a durable `~/.coderAI/transactions/<session-id>` ledger.
  Every approved synchronous mutating tool call opens a complete pre-operation
  workspace snapshot before pre-tool hooks, observes the workspace after the
  tool and post-tool hooks, and persists `open -> recorded -> committed` state.
- Ledger entries link the exact run, session, agent, workspace, permission
  snapshot, objective, approved plan revision, and tool-call ID. Tool arguments
  are linked by a stable hash rather than duplicated as secret-bearing text.
- Native filesystem changes and foreground shell-observed changes are recorded,
  including side effects left behind by failed or timed-out tools. The observed
  paths also feed the completion evidence ledger.
- Resume converts interrupted open or recorded transactions into explicit
  recovered state; interrupted rollback becomes a retryable partial failure.
  Recovery metadata is stored in both the transaction record and session
  metadata independently of transcript compaction.
- `undo` and file rewind prefer transaction rollback. Rollback runs newest
  first, refuses to overwrite a path changed after the transaction, reapplies
  existing project/protected-path/symlink guards, retains partial failures for
  retry, and falls back to the legacy per-file ledger for older sessions.
- Parent and delegated agents retain separate transaction stores. The parent
  does not wrap `delegate_task` in a second transaction, so it cannot select or
  roll back the child's ledger through its own undo history.
- Deterministic core and security tests cover all state transitions, failed
  tools with mutations, snapshot rejection, resume/recovery, native and shell
  observation, conflict/partial rollback, traversal and symlink attacks,
  Plan Mode/approval rejection, and parent/child ownership boundaries.

Implemented mutating-subagent worktree slice:

- Production `workspace`/`auto` delegations require the configured project root
  to be a Git root and run in a detached, owner-only worktree under
  `~/.coderAI/worktrees/`. The child is constructed with that explicit root;
  filesystem, terminal, quality, package, context, transaction, prompt, and
  project-trust resolution therefore point at the child workspace rather than
  the process CWD.
- The child starts from the parent's live tracked and non-ignored state,
  including dirty and untracked files. A private baseline distinguishes those
  pre-existing parent changes from the child's delta, so integration never
  replays or discards unrelated work already in progress.
- After the child finishes, the runtime creates an exact unified/binary review
  preview and stable fingerprint. Integration requires parent approval (or the
  parent's explicit auto-approval policy), rejects changed symlinks and unsafe
  paths, rechecks both the reviewed child state and the live parent baseline,
  and fails closed on either-side drift before overwriting anything.
- Parent `delegate_task` execution now opens a parent transaction only for the
  workspace domain. That record brackets the approved patch integration and
  parent `on_subagent_stop` hooks while remaining separate from every child
  transaction. Denied/no-change delegations produce no parent workspace delta.
- Worktrees are removed and unregistered on success, denial, failure, timeout,
  and resume attempts. Child task/plan runtime artifacts are not integrated.
  Native background-process and Git-mutating tools are withheld from workspace
  children while the remaining background/Git-metadata policies stay open.
- Deterministic core and security coverage exercises dirty/untracked seeding,
  approval and denial, parent/child drift, transaction recording, symlink
  traversal, review-swap attacks, cleanup, non-Git refusal, and capability
  narrowing.

Remaining before Milestone 3 is complete:

Extend transaction supervision across long-lived background processes, and
decide how Git metadata-only mutations (`.git` is intentionally excluded from
workspace snapshots and linked worktrees share the repository's Git metadata)
should be previewed and reversed, including mutations reached through arbitrary
foreground commands. Add retention/incremental storage for full snapshots plus
process-kill fault-injection coverage beyond the deterministic interrupted-state
tests. Decide whether non-Git projects need an equivalently isolated copy-and-
patch backend; mutating delegation currently fails closed outside a Git root.
Remove or formally deprecate the legacy
`HistoryManager.current_session` compatibility surface after remaining callers
and integrations use explicit session objects.

## Milestone 4 — Distribution and Release Integrity

**Status: in progress (local distribution-integrity exit work complete 2026-07-30;
release-candidate workflow evidence pending).**

Move built-in personas and starter assets into package resources with explicit
`builtin`, `user`, and `project` scopes. Add clean-wheel smoke tests for every
advertised capability. Build once, test that exact artifact on supported
platforms, and promote the same signed artifact to GitHub and PyPI.

Completed locally:

- First-party personas, skills, rules, prompts, the bundled `git_extended` MCP
  server, and `/init` starters are shipped in the wheel. Capability discovery
  uses deterministic `project → user → builtin` precedence, excludes untrusted
  project personas/skills, and rejects symlink escapes and persona traversal.
- `coderAI skills list --scope all` now includes and visibly labels immutable
  built-ins; the installed-wheel probe caught and now guards both the omitted
  entries and Rich-markup scope-label regression.
- `scripts/verify_wheel.py` validates wheel metadata, the `coderAI` console
  entry point, all advertised extras and their dependency markers, typing and
  runtime resources, and the adjacent sdist. The sdist carries its build inputs,
  referenced documentation, changelog, security policy, and the verifier while
  excluding a misleading partial test tree.
- The probe installs the wheel into an isolated environment with an empty home
  and project, then exercises every CLI/group help path, built-in discovery,
  prompt composition, `/init`, native tool discovery, and an actual stdio
  initialize/list exchange with the packaged MCP server. Repeatable `--extra`
  probes cover `semantic`, `local-embeddings`, `web`, and `browser`; browser
  verification can also install Chromium and launch a local page.
- Distribution versioning has one runtime source (`coderAI/_version.py`), with
  setuptools metadata derived from it. The release gate requires an exact
  `v<package-version>` tag. PyPI publication no longer masks an existing-version
  mismatch with `skip-existing`.
- The release workflow builds and attests once, smoke-tests the downloaded exact
  artifact on Linux/macOS with Python 3.10/3.12, and separately installs every
  advertised optional extra on Python 3.12. The browser job also launches
  Chromium. GitHub and PyPI promotion remain downstream of all core and optional
  smoke jobs and download the same workflow artifact.
- A manual workflow dispatch is now a non-publishing release-candidate run. Tag
  pushes are the only publishing path, preventing a validation run from
  accidentally creating a GitHub/PyPI release.

Local verification can establish archive contents, sdist rebuildability,
metadata/entry-point/version consistency, isolated installed behavior, optional
dependency resolution, CLI dispatch, bundled subprocess behavior, and a local
Chromium launch. It cannot issue or inspect GitHub build-provenance attestations,
exercise artifact upload/download on hosted runners, prove the supported
Linux/macOS and Python 3.10/3.12 matrix, or validate GitHub/PyPI trusted-publisher
configuration.

### Milestone 4 exit criteria

- The local format, lint, type, full-test, security-test, archive, and clean-wheel
  gates pass from a fresh build.
- Every advertised core and optional capability has an installed-wheel probe;
  no probe imports runtime data or code from the source checkout.
- Wheel and sdist metadata agree with the single runtime version; a release tag
  must be exactly `v<version>`.
- One non-publishing release-candidate dispatch of the updated workflow succeeds
  in the build/attestation job, every supported core smoke matrix cell, and all
  four optional-extra smoke cells. The run's artifact and attestation digests
  are retained as exit evidence.
- Trusted publishing configuration is reviewed before the next tag. The next
  tag run must promote only the already-attested `release-dist` artifact; any
  existing PyPI version is a hard failure.

The first three criteria are locally implementable. Keep Milestone 4 open until
the release-candidate workflow criterion and trusted-publishing configuration
review are satisfied. The tag-promotion assertion is verified on the next real
release rather than simulated locally; a failure there reopens the milestone.
Milestone 3 remains independently in progress.

## Milestone 5 — Capability Routing and Code Intelligence

Status: **in progress; routing evaluation and useful-action observability implemented**.

Completed in the first slice:

- Ordinary unknown objectives load an eight-schema universal set. A compact,
  ordered native catalog deterministically adds editing, execution, quality,
  Git, search, web, browser, desktop, package, memory, undo, vision, context,
  and MCP families from objective vocabulary. Unknown and targetless ambiguous
  mutation requests fail conservatively to the universal set.
- Routing consumes only the already-filtered registry. Persona rules,
  workspace trust, platform and optional dependency availability,
  `web_tools_in_main`, delegated-agent native ceilings, and disabled dynamic MCP
  therefore remain hard upper bounds. Dynamic MCP matching uses server/tool
  identifiers rather than untrusted descriptions and stays disabled for
  domain-scoped subagents.
- Successful schemas remain warm only in the current objective's `TurnContext`.
  Every reroute intersects warmth with current eligibility, so it cannot cross
  objective, session, agent, permission, persona, dependency, MCP-health, or
  Plan Mode/amendment boundaries.
- The executor remains the enforcement boundary for invented calls and retains
  all confirmation, workspace-trust, provenance/egress, MCP confused-deputy,
  approved-plan, transaction, and parent-to-child checks.
- The existing event bus now carries `capability_routing` telemetry with
  provider-estimated schema-token cost, selected names, deterministic reason,
  Plan Mode state, and selection success/failure. Headless NDJSON exposes the
  same payload as `capability.routing` without changing terminal result shapes.
- Deterministic core and security regression coverage exercises the universal
  limit, objective selection, warm/reset behavior, Plan Mode and amendment
  boundaries, persona/subagent ceilings, optional/browser/desktop gates,
  dynamic MCP selection, conservative unknowns, completion evidence, and
  permission enforcement.

Completed in the evaluation and observability slice:

- A 40-case deterministic offline corpus covers ordinary read/search; every
  routed native family; multi-capability, unknown, and ambiguous objectives;
  persona, Plan Mode, amendment, subagent, dependency, platform, network, and
  dynamic-MCP ceilings; same-objective warmth; and every documented warmth
  reset boundary.
- Exact and required-subset scoring reports false positives, false negatives,
  conservative-fallback accuracy, routed and full eligible-registry schema
  tokens, absolute/percentage savings, and capability/boundary groups. Checked
  thresholds are 100% routing and fallback accuracy, zero false positives and
  negatives, and at least 50% aggregate token savings. The calibrated corpus is
  40/40 with 74.63% savings (100,001 routed versus 394,178 baseline tokens using
  the deterministic `cl100k_base` encoding).
- Corpus evidence removed an editing-family false positive from undo objectives:
  when undo vocabulary matches, the router now fails closed instead of exposing
  forward mutation tools because the objective mentions the change being
  reversed.
- A monotonic objective clock records the first real, successful action that is
  present in the current routed eligible surface. Routing/control-only,
  task-management-only, denied, failed, cached, and synthetic recovery activity
  does not qualify. The `first_useful_action` event and headless
  `capability.first_useful_action` envelope contain only `tool_name` and
  `elapsed_ms`, never objective text, arguments, secrets, or result content.

Remaining before Milestone 5 is complete:

Add an LSP gateway for Python and TypeScript with definitions, references,
hover, workspace/document symbols, diagnostics, and rename preview. Feed compact
repository-graph and diagnostic evidence into planning and the completion
ledger, including invalidation after mutations. Add supported-platform and
clean-wheel probes for the gateway and optional language-server dependencies.

### Milestone 5 exit criteria

- Fewer than ten universal schemas are loaded on ordinary turns, and routed
  additions meet a checked accuracy/token-cost budget on the offline corpus.
- Warm capabilities improve repeat actions without crossing any objective,
  session, agent, permission, persona, Plan Mode, dependency, or MCP boundary.
- Routing telemetry supports schema cost, accuracy, and time-to-first-useful-
  action reporting without leaking objective, tool arguments, secrets, or
  untrusted result content. **Met locally by the routing evaluation and
  observability slice.**
- Python and TypeScript LSP operations cover definition, references, hover,
  symbols, diagnostics, and rename preview from a clean installed artifact.
- Compact repository-graph/LSP evidence participates in planning and completion
  verification, with deterministic mutation invalidation and security tests.

Keep Milestone 5 open until all criteria above pass. The LSP gateway is not part
of this first routing slice.

## Milestone 6 — Recovery and Durable State

Classify failures into provider/network, schema/arguments, dependency,
permission, patch conflict, test failure, context overflow, and repeated
semantic failure. Give each class a bounded recovery policy and exact resume
point. Persist objective, decision, artifact, verification, and failure ledgers
separately from the transcript.

## Milestone 7 — Memory and Permissions

Split memory into global user, shared project, private project, and task-local
scopes. Store provenance, confidence, last use, supersession, and expiry; make
durable writes reviewable.

Replace headless deny-all versus `--yolo` with explicit `read-only`,
`workspace-write`, `workspace-write + declared network`, and `full-access`
profiles. Control filesystem, commands, network, secrets, git push, and external
side effects independently.

## Verification Strategy

Every milestone must add:

1. Deterministic trajectory tests using fake providers.
2. Fixture repositories graded by end state and required checks.
3. Focused security and concurrency regressions.
4. Nightly real-model evals across supported providers.
5. Clean-wheel smoke tests where packaging behavior is affected.

The blocking local gate remains:

```bash
ruff format --check coderAI/ tests/
ruff check coderAI/ tests/
mypy coderAI/
pytest -q
pytest -q -m security
```

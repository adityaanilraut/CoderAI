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

Status: **first runtime slice implemented**.

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

### Follow-up slices

1. Persist the objective ledger independently of transcript compaction.
2. Add explicit acceptance-criterion editing and `verified`, `reasoned`,
   `blocked`, and `not-applicable` evidence outcomes.
3. Capture pre/post workspace hashes so shell-driven mutations enter the ledger.
4. Associate checks with affected artifacts and invalidate only impacted checks.
5. Surface objective, evidence, open work, and completion status in the TUI.
6. Add fixture-repository evals that grade final repository state and false success.

### Exit criteria

- No workspace-changing turn can return `success=true` without fresh evidence.
- Failed checks cannot be summarized as success.
- Compaction and resume preserve objective, decisions, mutations, and checks.
- Deterministic evals measure false-success and verified-completion rates.

## Milestone 2 — Real Plan Mode

Status: **first end-to-end runtime slice implemented**.

Introduce a `PlanSession` state machine with read-only exploration, structured
questions, editable acceptance criteria, constraints, interfaces, failure
modes, checks, rollout, and explicit approval into an execution run. Link the
approved plan revision to `ObjectiveState`; implementation changes become
visible amendments.

Keep an immediate-execution path for small, low-risk tasks.

The first slice provides enforced read-only tool routing, structured plan
submission, immutable revisions under `.coderAI/plans/`, visible plan cards,
unanswered-question blocking, explicit amendments, and approval into an
execution turn linked to the exact plan ID and revision.

## Milestone 3 — Isolation and Transactions

Replace ambient `HistoryManager.current_session` with an immutable `RunContext`
carrying run, session, agent, workspace, checkpoint store, and permission
policy. Construct history and backup stores explicitly per session.

Track direct edits and shell-observed diffs in a transaction ledger. Give
mutating subagents isolated worktrees and integrate reviewed patches instead of
sharing a writable directory.

## Milestone 4 — Distribution and Release Integrity

Move built-in personas and starter assets into package resources with explicit
`builtin`, `user`, and `project` scopes. Add clean-wheel smoke tests for every
advertised capability. Build once, test that exact artifact on supported
platforms, and promote the same signed artifact to GitHub and PyPI.

## Milestone 5 — Capability Routing and Code Intelligence

Keep fewer than ten universal tools loaded. Add a compact capability catalog
and load only the most relevant schemas, keeping successful tools warm for the
current objective. Track schema tokens, routing accuracy, and time to first
useful action.

Add an LSP gateway for Python and TypeScript first: definitions, references,
hover, symbols, diagnostics, and rename preview. Feed compact repository-graph
results into planning and completion evidence.

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

# Agent review remediation implementation

Implemented October 6, 2026, against the existing working tree. The original
[105-claim audit](agent-review-remediation-plan.md) remains the evidence for what
was actually wrong and what the supplied review overstated. This implementation
addresses the verified weaknesses and the supported portions of partial claims;
it does not turn the six unsupported claims into new features or security fixes.

Existing tracked and untracked work was preserved. A file-content baseline was
saved before editing; the final verification evidence includes hashes of the
incremental source changes.

## Delivered phases and claim coverage

| Area | Original claims | Delivered behavior |
| --- | --- | --- |
| Storage and restoration | 1–3, 12, 26, 29, 45–47, 104 | Validate storage IDs; reject linked paths; use descriptor-relative reads, writes, directory creation and deletion on POSIX; serialize instance metadata transactions; validate corrupt records; delete only scratchpads created by this process; reconcile stale foreground and background records without failing another live process's records. File restore validates every destination and blob before mutation and requires source-session checkpoint ancestry. |
| Child capabilities | 4–8, 10, 18–19, 23–28, 34–36, 44, 53–55, 65–67 | Validate modes, descriptor lifetime, numeric limits and tool lists; reject ordinary markdown during discovery; narrow requested capabilities against roles and parent policies, including exclusions and descriptor filters; share advertisement and execution policy; respect trusted external-tool effects; resolve roles only within owning project/user roots. Untrusted projects cannot define unrestricted roles. Unsupported external child restrictions fail before launching the backend. |
| Agent lifecycle | 11, 13–16, 20–22, 30–33, 37–43, 47–49, 51, 56 | Use full UUIDs and reject duplicate registration; check quota before publication; retain cancellation per activation; bound steering and lifecycle history; guard cyclic lineage; recognize every terminal outcome; clear stale results on wakeup; refuse eviction of a live worker; enforce registry thread ownership; support async notice callbacks; honor deny/stop hooks and shared deadlines; settle failed startup; reject late spawning from interrupted parents; close blocked streams and reap subprocesses. |
| Team coordination | 58–64, 68–79 | Park workers on events with an idle retention limit; clean owned workers, contexts, mailboxes and tasks; require session ownership for public tools; reject ambiguous names and cross-scope dependencies; validate target types and deadlines; use monotonic waits, stable mailbox sequence order and priority task selection with revision checks; use iterative bounded DAG validation and finite enqueue budgets. |
| Approval and ACP boundaries | 80–84, 94–95 | Missing permission rows ask instead of allowing; missing sessions never borrow ambient YOLO/AFK authority; explicit sandbox/hook/plan approval remains explicit; tickets require exact session and patterned target context, with locked use-count consumption and mutations. ACP terminals require a loaded session and inherit its sandbox, with payload/count/time limits. |
| History and protocol | 17, 50, 52, 57, 85–93, 96, 103, 105 | Escape and bound repository metadata; quote child summaries as child output; bound child compaction input while retaining the task and complete tool groups; emit exactly one turn end; interrupt pending goals on raw cancellation; guard replacement cycles; mark synthetic tool failures correctly; reject absent/ambiguous fork points; retain mixed legacy rows and rewrite known ownership fields; roll back failed forks; retain D-Mail until rewind and directive injection succeed; validate ACP bindings and workspaces. |
| Wire and cancellation | 97–102 | Detach slow UI observers without failing producers; keep recorders and approval-control subscribers lossless; bound cross-thread publish acknowledgment; bind child emitters to the launching loop; prevent trace appends through links; cancel owning turn tasks and suppress late provider callbacks. |
| Cleanup | 3, 9, 24, 34, 37–38, 43, 47, 54, 62 | Remove unused quota/seed/scratchpad fields and exports, undeclared isolation scaffolding, redundant scratch allocation, guessed inbox-handle lookup, unused read-only inference lists, ambient approval scans and duplicated tool-filter policy. Keep shared controller ownership and distinct message consumers where they serve real behavior. |

The table covers all 105 original claim numbers; overlapping areas deliberately
share some numbers. Protected behaviors remain covered: read-only teammate
blocking, ancestry-derived depth, continuation after interruption, normal ACP
resume, and executor lock ownership during synchronous cancellation.

The three reproduction-only findings now have explicit dispositions:

- **13:** kept the per-activation cancellation lifetime. Existing continuation and
  interruption regressions pass; a blocked stream cancellation test also verifies
  transport closure without waiting for another chunk. This does not establish
  the review's alleged historical event-replacement race.
- **21:** registry mutation is confined to its owning thread and wrong-thread
  mutation is rejected deterministically. This makes the supported contract
  explicit without claiming that ordinary single-loop calls previously raced.
- **39:** a regression interrupts a parent, then attempts new foreground and
  background children after the cancellation snapshot; both attempts are denied.

## Runtime contracts and migration

- IDs now use full UUID entropy. Valid legacy ASCII identifiers remain accepted;
  empty IDs, separators and traversal are rejected. Private storage writes use
  restrictive modes. Stale instance recovery distinguishes current-process
  workers, another live PID, and dead/legacy owners.
- Discovered markdown requires frontmatter. Missing permission mode defaults to
  read-only; omitted tools together with omitted mode defaults to read/grep/glob.
  Explicit general roles require project trust or a trusted user role root.
  Project roles take precedence over user roles with the same name.
- `None` means inherited tools and `[]` means no tools. Requested tools narrow
  the role; parent exclusions and descriptor filters survive nested spawning.
  Discovered read-only roles cannot delegate. Unknown external effects remain
  denied in read-only execution; trusted `toolPolicies` may declare read effects.
- Descriptor `mode` means `one-shot` or `continuable`; permission mode remains
  `SubAgentSpec.mode`. Provider/model/persona descriptor metadata is serialized
  compatibility metadata; execution uses the spec's provider/model/role fields.
  Native continuable workers require `in_process`.
- External manager launches reject restrictions they cannot enforce, including
  read-only role policy, tool filters, token budgets, plan mode, nondefault
  sandbox restrictions and file mutation callbacks. The standalone CLI drivers
  have their own execution policy. Their prompts go through stdin and their
  stdout is limited to 4 MB, with bounded stderr and process cleanup.
- Manual standalone teammates can use `auto_start=False`; starting a worker
  requires a running loop and an inherited context or explicit tool policy.
  Display acknowledgment and worker delivery are different consumers: consuming
  a mailbox does not pretend the user has read the display history. Both are
  bounded, and teardown removes both.
- Direct registry eviction requires worker settlement. Cancellation signals a
  worker; callers that dispose it must await it. Session disposal already follows
  that sequence. Each resumed activation owns a new abort event so an old
  provider thread cannot observe a reset cancellation flag.
- Fork integers are existing sequence numbers, never fallback list indexes.
  Missing message IDs raise before creating a branch. Failed durable creation
  removes partial history/state/reference artifacts; goal deletion retains its
  normal tombstone to prevent late recreation.
- File-history restore is confined to the workspace. New manifests store relative
  paths; legacy absolute paths are accepted only inside that workspace. Tools
  can still use approved additional directories, but undo does not snapshot or
  restore those external directories. Arbitrary shell side effects are also
  outside the checkpoint guarantee. Recording callbacks cover supported file
  mutations; this is not a complete filesystem transaction system.

## Verification

Latest results across **108 independently executed test files**:

- **1,664 passed; zero failed, errors or skipped cases.**
- **65 new focused regressions** in `test_remediation_runtime.py` and
  `test_remediation_storage.py`.
- Repository formatting and lint pass; type checking passes all **333 source
  files**, with no added suppression budget.
- Strict installed dependency constraints and `pip check` pass. The dependency
  audit covers **147 installed distributions**, with zero blocking findings.
- Real installed-SDK Jev calibration runs: **15 passing cases**, not skips.

The complete release command was:

```sh
.venv/bin/python scripts/verification.py check --suite all
```

Final wire changes were followed by independent reruns of 11 affected test files,
then formatting, lint and type verification. Each test file used an isolated
home and scrubbed credentials. The latest per-file counts, commands, source
hashes and dependency audit summary are saved in
[verification evidence](../.verification/agent-review-remediation-tests.json).

CLI argument parsing was checked locally against Codex CLI **0.160.0** and Claude
Code **2.1.273**, using help-only commands. Tests launch real local Python
subprocesses to verify stdin prompt data, output limits and cleanup. No billed
provider request or external agent job was launched.

## Practical limits

Synchronous SDK work already connecting or awaiting a response cannot be forcibly
terminated by cancelling an asyncio task. The owned stream is closed when the
transport becomes available, and late callbacks cannot mutate the next turn;
this does not guarantee immediate remote cancellation or zero billing.

Filesystem descriptor protections were verified on macOS/POSIX. Native Windows
uses link/path validation without the same descriptor-relative race guarantees;
Windows reparse-point races and native terminal behavior were not certified in
this run. Live provider behavior was not certified either.

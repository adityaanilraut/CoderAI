# Terminal UI implementation and verification

CoderAI keeps inline terminal scrollback, a persistent composer, and focused
browsers. The seven implementation phases are complete in the working tree.
The interactive shell now uses a session-scoped async controller, with
prompt-toolkit owning terminal input and presentation committing each finished
message once. Existing unrelated working-tree changes were preserved.

## Phase outcomes

| Phase | Implemented outcome | Main source |
|---|---|---|
| 1: correctness | Explicit dialog cancellation; recursive credential redaction in both configuration renderers; public session-owned job APIs; first-request image metadata; quoted, external and Windows path parsing; literal error rendering; configured versus verified setup status | `session_picker.py`, `security.py`, `dispatch.py`, `prompt.py`, `setup.py`, `welcome.py` |
| 2: lifecycle | Concurrent input and execution; sequential queues; steering; stop and recovery; exclusive approval/question focus; scoped drafts, attachments, queues and subscriptions; cleanup of owned tasks and terminal state | `controller.py`, `controller_commands.py`, `interaction.py` |
| 3: presentation | One transient preview and scrollback commit path; semantic light/dark styles; bounded narrow layouts; critical toolbar badges; cached Git status; persistent display settings; static accessible interaction; full tool-output inspection | `presentation.py`, `prompt.py`, `interaction.py`, `preferences.py`, `../theme.py` |
| 4: navigation | Search over complete session/job collections; explicit zero-results state; session status/fork/response previews; full transcript and compaction boundaries; output search; preserved browser position; bounded job previews | `controller_commands.py`, `interaction.py`, `output.py`, `../../background/store.py` |
| 5: composition | Typed shared submissions; reviewed file/image attachments; Ctrl-T removal tray; ambiguous-file choice; workspace-aware completion; quoted paths; missing/stale/unsupported attachment errors; queued and recalled image metadata; safe external editor handoff | `submission.py`, `attachments.py`, `submission_history.py`, `completer.py`, `prompt.py` |
| 6: trustworthy feedback | Effective context and execution settings; per-model cost with unavailable pricing explicit; estimated/provider-reported usage provenance; completion summaries and diff/undo links; retry with attachments; unified command/help/alias/completion metadata | `runtime_view.py`, `presentation.py`, `slash.py`, `exit_summary.py` |
| 7: interaction gates | Offline tests through the actual shell entrypoint; delayed turns and background progress; queue/steer, approval/question submission and cancellation, attachment removal, output inspection, session transitions, resize and terminal restoration | `tests/test_shell_ui_pty.py`, `tests/fixtures/shell_ui_provider.py`, `scripts/verification.py` |

Source paths in the table are relative to `coderai/ui/shell/` unless they start
with `tests/` or `scripts/`.

## Commands and keys

| Action | Control |
|---|---|
| Submit while idle; queue while a turn runs | Enter |
| Steer a running turn; submit normally when idle | Ctrl-S or `/steer <text>` |
| Request cancellation; pause queued turns | Ctrl-C or `/stop` |
| Inspect or edit queued submissions | `/queue [list\|remove <n>\|move <from> <to>\|clear\|run]` |
| Retry the last failed/interrupted submission with its attachments | `/retry` |
| Review/remove files and images without losing the draft | Ctrl-T or `/attachments edit`; `/attachments remove <n>`; `/attachments clear` |
| Attach a file to the reviewed draft | `/attach "path with spaces"`; `@"path with spaces":10-20` |
| Attach an image before submitting a turn | `/image "image path.png" [prompt]`, then Enter |
| Inspect complete tool output | Ctrl-E or `/output [message/tool ID or search]` |
| Browse/search full messages and compaction boundaries | `/history [message ID or search]` |
| Search/resume sessions with previews | `/sessions [session ID or search]` |
| Inspect foreground work, jobs, subagents and pending interactions | `/activity` or `/task` |
| Compose in the external editor and return to review | Ctrl-O or `/editor` |
| Persist output detail | `/display compact` or `/display verbose` |
| Persist static, numbered, ASCII interaction | `/display accessible`; `/display animated` restores interactive presentation |
| Persist theme | `/theme dark` or `/theme light` |

Approvals and questions temporarily own focus and preserve the composer draft.
Escape, Ctrl-C, Ctrl-D and EOF cancel focused interactions; cancellation cannot
approve a plan, select an undo checkpoint or submit a default question answer.
The output inspector supports Ctrl-F search. Focused browsers preserve their
query and selection, use one pane below 80 columns, and allow Tab to view
details. Accessible browsers offer numbered choices, paging and `/search` over
all records. `NO_COLOR` removes styling without changing these actions.

Stopping pauses the queue. `/queue run` explicitly resumes it. Steering is
acknowledged and accepts text; images remain available for the next turn.
Retry uses the current workspace and retains partial edits; it does not restore
a checkpoint automatically. `/undo` shows the conversation/code restoration
scope before an explicit selection. A plan toggle during generation is labeled
as a next-turn setting while the toolbar continues to show the active plan mode.

## Ownership, retention and compatibility

`ShellViewState`, `PromptSubmission`, `SelectorOutcome` and `QuestionOutcome`
make lifecycle and cancellation explicit. The controller owns input tasks,
active execution, subscriptions, session drafts and queued submissions. Focused
applications use the same prompt-toolkit input/output, with the composer
suspended. Job execution and event delivery continue while a browser is open.
Compatibility setup/login/diagnostic wizards release raw terminal mode and run
with exclusive input ownership in a worker. External editors defer completed
scrollback until terminal ownership returns.

The presentation keeps a bounded transient preview and deduplicates finished
messages by stable ID. Full messages come from session storage. Full tool output
can use its session-owned persisted spill, with path ownership and checksum
validation. Job previews read a bounded tail; opening an inspector is the
explicit full-output operation. Public job snapshots replace private registry
access in the UI.

Inline file context is limited to 2 MiB per file. Default job tails are bounded
to 64 KiB. The transient output index retains 100 results; persisted transcript
inspection remains available beyond that index. Private per-workspace image
history snapshots retain at most 30 entries and 200 MiB. Expired placeholders
and missing snapshots produce an actionable error before execution.

Display preferences are additive user settings. Usage provenance is additive
assistant-message metadata; older histories show unavailable provenance rather
than inventing provider measurements. Existing session storage and ACP/wire
formats remain compatible. Python 3.12 remains supported with standard typing
syntax.

## Cleanup

Unused running-prompt delegates, toast protocols, the duplicate completion-menu
prototype, an unused history parser and a duplicate Git poller were removed.
Help groups now derive from the canonical command catalog rather than a second
hard-coded list. Obsolete job calls and direct image-message mutation were
replaced with the public APIs. Unused imports and misleading expansion/setup/help
text were removed or corrected.

The async controller is the default for interactive TTY sessions. The internal
`CODERAI_SHELL_CONTROLLER=0` rollback switch and the non-TTY/readline compatibility
path remain available. These are intentional compatibility paths, not competing
interactive input owners. Machine-readable CLI/wire stdout remains separate from
the interactive shell. Temporary Python 3.12 verification tooling is removed
after checks; JSON evidence is retained in `.verification/`.

## Verification and platform limits

The shared runner executes each test file in its own process with isolated
configuration homes and scrubbed credentials. The focused `ui` suite includes
the repair, controller and real-entrypoint PTY regressions, existing renderer
coverage, CLI startup, setup and approval tests. See [verification](verification.md)
for commands, required CI jobs and report semantics.

Local checks use Python 3.12.14 and the project Python 3.14.7 environment on
macOS arm64. Full verification includes format, lint, the suppression budget,
mypy, dependency constraints, `pip check`, offline tests, the installed dependency
audit and CLI startup. Reports are saved in
`.verification/ui-roadmap-python312-final.json` and
`.verification/ui-final.json`; dependency audit evidence is in
`.verification/dependency-audit.json`.

The final full Python 3.12 check passed 1,417 tests across 101 isolated files,
with zero skips and 63% statement coverage (the required gate is 28%). Format,
lint, mypy, dependency constraints and the installed-graph audit also passed.
The focused UI suite passed 196 tests across 13 files on both Python versions.
Coverage evidence is saved in `.verification/ui-roadmap-coverage.json`.

PTY tests exercise `_run_interactive` with a deterministic offline manager, real
terminal input and prompt-toolkit rendering. Separate controller tests exercise
the real engine's retry events with mocked provider transports. They cover
behavioral integration, not live-provider certification. Layout fixtures cover
40, 60, 80, 120 and 200 columns, long identifiers, Unicode and literal markup.

Required Linux/macOS Python 3.12 gates are configured in the reusable workflow.
Only macOS was executed locally; Linux CI execution remains a release check.
Windows runs the portable UI/controller regressions in the existing advisory
suite. Native Windows ConPTY input, resize and terminal restoration have not
been certified here. Live provider connectivity and real billing measurements
also remain outside these offline checks.

## UI audit follow-up

Draft recovery now saves cursor position, attachments, queued prompts and failed
submissions privately per workspace/session. Revision checks prevent delayed
autosaves from overwriting newer input or resurrecting deleted state. Pasted
text uses durable random identifiers. Recovery and queue editing always pause
queued work; only an explicit `/queue run` resumes it.

The common selector exposes details in accessible mode and visibly disables
unavailable actions. Multiple-choice questions show checkmarks and a submit
count. Checkpoint restoration explains conversation impact, offers complete diff
inspection, and requires a final scope-specific confirmation. `/diff` browses
complete file and hunk content; summary counts cover the entire patch.

Welcome fields wrap at narrow widths, and the toolbar prioritizes model and
role. Shortcut help shares metadata with the actual bindings. Permission choices
derive from supported runtime modes. Streaming commits complete Markdown blocks,
and tool previews bound both line and character counts while retaining full
output for inspection.

`/activity` combines current tools, elapsed time, todos, goals and background
work. `/model` adds favorites/recent choices, cached capability comparisons and
explicit connection verification. Estimates and unknown metadata are labelled;
rendering never probes a provider. Session navigation adds pin/archive actions,
date/status filters and fork ancestry. MCP navigation adds tool schema search,
reconnect and private project-scoped bearer tokens for remote servers.

Regression coverage lives in `tests/test_shell_ui_audit.py` and the shared UI
suite. PTY journeys cover crash recovery and paused-queue replacement prompts.
The follow-up UI run passed 226 tests across 14 isolated files, with no failures
or skips; evidence is saved in `.verification/ui-audit-followup.json`. Formatting,
lint, the type-suppression budget and focused type checks also passed. The audit
and PTY regressions were rerun after removing duplicate startup credential checks.
The offline checks do not certify live providers, MCP endpoints or screen readers.

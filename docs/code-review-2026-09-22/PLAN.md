# CoderAI remediation plan (from the 2026-09-22 code review)

**All review findings live in `docs/code-review-2026-09-22/`.** This file is the plan; the evidence is elsewhere in the folder:
- `findings/*.md`: per-area reports. Every item below cites a finding ID, for example `WF-A1`; the prefixes are listed in `README.md`.
- `00-SUMMARY.md`: executive summary.
- `static-analysis/`: raw outputs from vulture, ruff and the orphan-module scan.

Each phase is sized so one agent session can finish it. Do the phases in order: later phases assume earlier ones are done, and Phase 2 removes code that later phases would otherwise have to fix twice.

---

## Working rules (apply to every phase)

1. **Read the finding first.** Open the cited `findings/*.md` entry before changing code. Line numbers are from 2026-09-22 and will have drifted, so re-locate the code with `rg`.
2. **Re-verify before fixing.** If a finding turns out to be wrong or already fixed, don't "fix" it. Mark it `[~]` in this file with a one-line reason.
3. **Test first.** For every behavioural bug, write a failing regression test, then fix it, then confirm the test passes. Put tests in the most relevant existing `tests/test_*.py` file, or in a new `tests/test_review_phase<N>.py`.
4. **Run tests one file at a time.** Never run the whole suite in one process (repo rule in `AGENTS.md`). Use the repo venv, which has pytest-benchmark:
   ```bash
   .venv/bin/python -m pytest tests/<file>.py -p no:cacheprovider --benchmark-disable -q
   ```
   At the end of each phase, run every test file you touched and every test file that imports a module you changed. Also run `ruff check coderai` (it must stay clean for the `F` rules) and `ruff format --check` on the changed files.
5. **Python 3.12 compatibility.** The target is `>=3.12`, but `.venv` is Python 3.14, so 3.13+ APIs won't fail locally. Don't use PEP 695 syntax. Don't use `asyncio.Queue.shutdown`, `QueueShutDown` or other 3.13+ APIs directly; use `coderai.utils.aioqueue`.
6. **Stay in the live code path.** The live runtime is `coderai.main` → `coderai/ui/shell/app.py` → `SessionManager` (`coderai/soul/session/manager.py`) → `AgentLoop`/`SessionSoul` → `ToolExecutor` + `ToolRegistry` (`coderai/tools/legacy/`). Until Phase 2 is done, don't fix bugs that exist only in `KimiSoul`, `coderai/app.py`, the `Shell` class, `CoderAIToolset` or the kosong `CallableTool2` classes.
7. **Keep changes minimal.** Match the surrounding style, and don't refactor outside the phase's scope. Consolidation work belongs in Phase 9.
8. **The uncommitted working tree belongs to the user.** Don't revert or discard unrelated uncommitted changes. Don't commit unless the user asks.
9. **Finish every phase the same way:**
   - Tick the checkboxes here: `[x]` for done, `[~]` for skipped or invalid with a reason.
   - Append an entry to `PROGRESS.md` in this folder: date, phase, what changed, tests run and their results, and any follow-ups.
   - Report back with a short summary.

Checkbox legend: `[ ]` todo · `[x]` done · `[~]` skipped or invalid (give a reason) · `[!]` blocked (give a reason)

---

## Phase 1 — Stop crashes, hangs and wrong results (quick wins)

**Goal:** fix small, local bugs in the live path that crash, hang, deadlock or silently return wrong results. Each fix should be 1–30 lines plus a regression test, with no design changes.

**1a. Deadlocks, crashes and hangs**
- [x] **WF-A1** Session delete deadlocks. `coderai/background/agent_runner.py` `kill_task` / `kill_all_tasks` take `job_store._lock` and then call `job_store.kill()`, which takes the same lock again. Fix: collect the ids under the lock, release it, then kill them. Test: `kill_all_tasks` with one running job returns within a timeout.
- [x] **TL-A1** `coderai/tools/legacy/executor.py` `_invoke`: `except TypeError:` runs a sync handler a second time on the event loop. Remove the fallback. Test: a handler that raises `TypeError` runs exactly once and returns an error result.
- [x] **IN-A3** `coderai/wire/server.py` uses `asyncio.Queue.shutdown` / `asyncio.QueueShutDown`, which are Python 3.13+. Switch to `coderai.utils.aioqueue.Queue` / `QueueShutDown`, as `coderai/wire/__init__.py` already does. Test: the server's queue type comes from `aioqueue`, and serve/shutdown works.
- [x] **UI-A1** `coderai/ui/shell/app.py`: `dispatch_slash_command` in the REPL loop has no `try`, so any handler exception exits CoderAI. Wrap it: log, print the error as plain text (escaped), `continue`. Handle `KeyboardInterrupt` the same way the turn path does.
- [x] **UI-A5** After Ctrl-C mid-stream, `_STREAM_STATE.is_streaming` stays true, so every later input, including `/exit`, is only queued. Reset the stream state in every interrupt and exception path.
- [x] **UI-A12** `coderai/ui/shell/session_picker.py` `select_with_arrows`: when stdin hits EOF, `_read_single_key()` returns `""` and the loop spins at 100% CPU. Treat `""` as cancel.
- [x] **UI-A11** `coderai/ui/shell/prompt.py`: Ctrl-C in multiline continuation returns the partial buffer as a prompt. Re-raise `KeyboardInterrupt`; only `EOFError` should finish the buffer.
- [x] **TL-A21** `import coderai.tools.dmail` on its own fails with a circular `ImportError`. Define `NAME` before the soul import, or import it lazily. Test: `python -c "import coderai.tools.dmail"` in a subprocess.

**1b. Silently wrong results**
- [x] **TL-A3** `coderai/tools/shell/__init__.py` (around line 333): the f-string contains `%%d:%%s`, so `printf` prints a literal `%d:%s`. As a result the exit code and cwd are never parsed, failing commands return `ok=True`, and `cd` isn't tracked. Use `%d:%s`. Test: in the persistent shell, `false` gives a non-zero exit code and `cd /tmp` updates the cwd.
- [x] **DC-E** `coderai/config.py` `_typed_global_knobs` reads `typed.mcp.tool_call_timeout_ms`, but the field is `typed.mcp.client.tool_call_timeout_ms`. The `AttributeError` is swallowed, so it always returns `{}`. Fix the path. Test: the knobs dict is non-empty with defaults.
- [x] **TL-A4** `coderai/tools/legacy/sanitizer.py`: the `sk-` pattern has no word boundary (`disk-usage-...` becomes `di[REDACTED...]`), and the DB-URI password regex is greedy. Add a boundary or lookbehind, and make the capture non-greedy and exclude `@`. Tests: normal identifiers survive, real keys are still redacted.
- [x] **TL-A12** `coderai/tools/file/replace.py`: a `.py` edit is rejected if the result doesn't parse, even when the original didn't parse either. Only reject when the original parsed and the new content doesn't.
- [x] **TL-A16** `coderai/tools/todo/__init__.py`: `todo_write` with `merge=False` always fails, because `explanation` becomes `False`. Pass `args.get("explanation")` through. Either implement `merge` or remove it from the schema; if you remove it, say so in the report.
- [x] **TL-A14** `schedule_create`: the schema advertises `cron_expression`, but the handler needs `at` / `every_seconds`. Make the schema match the handler (`coderai/tools/legacy/registry.py`, `coderai/tools/legacy/schedule.py`, `coderai/schedule.py`).
- [x] **WF-A10** `coderai/schedule.py`: `target_timestamp` isn't persisted or parsed on load, so every schedule fires immediately after a restart. Persist it, or parse `scheduledAt`. Make `_save` atomic (`coderai.utils.io.atomic_json_write`). Also fix the B904 at `schedule.py:112`, which replaces the real validation message with a generic one.
- [x] **WF-A15** `coderai/teams/tools.py`: `create_task` raises `KeyError`/`ValueError` on an unknown or self dependency, and `int(expected_revision)` sits outside the `try`. `wait_agent` accepts negative, `inf` and `nan` timeouts. Return tool errors instead of raising, and clamp the timeout.
- [x] **UI-A15** `coderai/ui/shell/dispatch.py`: `/effort xhigh` is rejected because `valid_efforts` is hardcoded. Use the config's list of valid efforts.
- [x] **IN-A9** `coderai/auth/oauth.py` `ensure_fresh`: a `return` inside the per-key loop should be `continue`.
- [x] **IN-A11** `coderai/cli/doctor.py`: it checks `status == "error"`, but the manager sets `"failed"` / `"unauthorized"`. Match the real values. Also stop `doctor` from creating `.coderai/` as a side effect.

**Exit criteria:** every item is `[x]` or `[~]`, each fix has a regression test, all touched test files pass, and ruff is clean for `F`.

---

## Phase 2 — Decide on and remove the resurrected "Kimi runtime" and orphan modules

**Goal:** remove the second runtime, so later phases fix one code path instead of two.

**Decision gate.** Before deleting anything, ask the user to choose one:
- **(a) Delete it (recommended; matches the intent of commit `5e06ff6`).**
- **(b) Finish it and switch the entry point to it.** This option changes the plan a lot: stop and re-plan.

**Salvage first.** The uncommitted diff also touches live code in the same files, including `coderai/soul/coderaisoul.py`, `coderai/ui/shell/__init__.py`, `coderai/soul/toolset.py`, `coderai/tools/*` and `coderai/utils/io.py`. Run `git diff` on each file, and keep any changes to live code (for example `AgentLoop`, `SessionSoul`, legacy tool handlers, `render_welcome_screen`, context helpers in `toolset.py`, and `utils/io.atomic_*`, which the tests use). Only remove the dead parts.

If the user picks (a):
- [x] Delete `coderai/app.py` (untracked). **Refs:** DC cluster table, WF-B5, AL-B1, PR-D1.
- [x] Remove `KimiSoul`, `CoderAISoul`, `StepOutcome`, `TurnOutcome`, `BackToTheFuture` and the duplicate `FlowRunner` from `coderai/soul/coderaisoul.py`. Also remove the stray `AgentLoop.type_check`. The live `FlowRunner` is `coderai/skill/flow/runner.py`. **Refs:** AL-B1, AL-A12, AL-A13, AL-A14, PR-A7. (Kept the `classify_api_error` / `is_retryable_api_error` helpers + ported their test; removed `_track_api_error` / `_provider_telemetry_kwargs` with the dead code.)
- [x] Remove `coderai/soul/context.py`, the `coderai/soul/slash.py` registry, `run_soul` (`coderai/soul/__init__.py`), and `load_agent`/`Runtime`/`Agent` in `coderai/soul/agent.py`, but only where they're used exclusively by the dead runtime. `SessionSoul` in `agent.py` is live. **Refs:** DC-A, PR-A6. (Kept `BuiltinSystemPromptArgs`, the soul exceptions + wire helpers used by live ACP/approval code, and the `SessionSoul.checkpoint_count` live change.)
- [x] Remove `CoderAIToolset`/`KimiToolset` (`coderai/soul/toolset.py:210+`) and the kosong `CallableTool2` tool classes in `coderai/tools/*`. Keep the live `handle_*_tool` functions and the context helpers. Delete the tool `.md` files that only the dead classes loaded, or wire them into the registry. **Refs:** TL-B (tool-system map), TL-B2, TL-B4, PR-D2. (Also deleted the never-loaded PR-D2 `.md` files; kept `MCPTool`/`WireExternalTool`/`convert_mcp_tool_result` in toolset.py for Phase 9.)
- [x] Remove the dead UI stack: the `Shell` class and its helpers from `coderai/ui/shell/__init__.py` (move `render_welcome_screen` to `coderai/ui/shell/welcome.py`, and keep `__init__` thin), plus `visualize()` in `visualize/__init__.py`, `visualize/_live_view.py`, `visualize/_interactive.py`, `ui/shell/replay.py`, `CustomPromptSession` in `prompt.py`, `ui/print/visualize.py`, and the Shell-only helpers listed in UI-C. **Refs:** UI-B1, UI-C, UI-D6. (Kept `render_mcp_console` + `migration_nudge` live/tested helpers; removed `render_mcp_prompt`, `print_migration_goodbye`, `echo.py`.)
- [x] Delete or rewrite the tests that only cover the dead runtime: `tests/test_coderaisoul_lifecycle.py`, `tests/test_context_lifecycle.py`, `tests/test_cancellation_resilience.py`, `tests/test_visualize_wire_loop.py`, the dead-runtime parts of `tests/test_runtime_parity.py`, and `tests/test_phase4_ui.py` (`ui/print/visualize`). Before deleting, check whether any of them assert behaviour the live path should also have, such as cancellation resilience. If so, port those tests to the live `AgentLoop`/`SessionManager`. **Refs:** DC-G. (Ported `test_classify_api_error_and_retryability` to `test_session_engine.py`; `test_runtime_parity.py` kept as-is — its checkpoint assertions are live. `run_soul` cancellation semantics have no live counterpart yet — see Phase 5 follow-up.)
- [x] Delete the orphan modules: `coderai/tools/test.py`, `coderai/background/ids.py`, `coderai/utils/environment.py`, `coderai/ui/shell/export_import.py`, and `coderai/wire/serde.py` (point `tests/test_background_wire.py` at `coderai.wire.types`). Either delete `coderai/background/worker.py` or wire it in; ask the user if it's unclear. **Refs:** DC-C, TL-C, WF-C, IN-C. (`worker.py` kept per user decision 2026-09-23.)
- [x] Deduplicate `coderai/utils/shell_quoting.py`, which is a verbatim copy of `subprocess_env.py:1-209`. Point its live importers (`prompt/__init__.py`, `tools/shell/__init__.py`, `tests/test_subagents.py`) at `coderai.utils.subprocess_env`, then delete it. **Refs:** DC-D, TL-B7, IN-D2.
- [x] Remove leftover Kimi branding and env names (`KIMI_CLI_NO_AUTO_UPDATE`, "Restart kimi", "Kimi Code CLI"). **Refs:** UI-B5. (All three lived in the deleted Shell/app files; remaining `kimi*` strings are live provider/model/env names.)
- [x] Delete the stale `build/lib/` copy of `app.py`, or tell the user to rebuild. `build/` is git-ignored.

**Exit criteria:**
- `rg -n "KimiSoul|CoderAIToolset|KimiToolset|coderai\.app\b|_live_view|_interactive|replay" coderai tests` returns only intentional matches.
- `python -c "import coderai.main, coderai.ui.shell.app, coderai.soul.session.manager, coderai.acp"` succeeds.
- Every remaining test file passes, run one file at a time. Since this phase removes a lot of code, run all of `tests/*.py`, one file per process.
- Ruff is clean for `F`.

---

## Phase 3 — Fix the permission model (subagents, agent roles, approvals)

**Goal:** make tool restrictions and approvals actually hold. This is security-critical; mark the new tests with `@pytest.mark.security`.

Several agents reported the same problems. Aliases: WF-A4 ≈ TL-A2 (read-only), WF-A5 = PR-A1 + PR-A2 (allowlist inversion), WF-A6 ≈ TL-A2 (child skips approval).

- [x] **WF-A5 / PR-A1 / PR-A2** Allowlist semantics in `coderai/subagents/runner.py` (the filter and the runtime check), `builder.py` and `registry.py`:
  - `None` means inherit, `[]` means deny all; check with `is not None`.
  - Use the existing alias-aware `registry.is_tool_allowed()` for name matching (`Read` → `read`, and so on).
  - Unknown `subagent_type` must be rejected, or treated as deny-all.
  - An explicit `read_only` must never be widened by the role's mode.
  - Test: `planner`/`architect`/`security-reviewer` get read/grep/glob; `code-reviewer` is read-only; an unknown type is denied.
- [x] **WF-A4** A real read-only mode: filter by the registry's mutating flag, or use an explicit read-only allowlist. It must block `bash`/`pwsh` unless the sandbox is `read-only`, plus `str_replace_editor`, `terminal_*`, `schedule_create`, `spawn_teammate`, and `Task`/`subagent` when the child would be writable. Enforce it both when listing tools and when executing them (`runner.py`, in both places).
- [x] **WF-A6 / TL-A2** Child executions inherit the parent's approval runtime, sandbox mode, plan-mode state and file-checkpoint hooks (compare `manager.py` around 1845-1862 with `runner.py` around 967-972). The spawn approval (`soul/approval.py` around 619-632) must reflect the child's actual capabilities, not `args.get("mode", "read_only")`. Add `mode` to the Task/subagent/subagent_fork schemas, or derive the mode from the role.
- [x] **WF-A3** Depth limit: register every spawned child, foreground included, with a lineage record, and derive depth only from that registry. Clamp `max_depth` to the configured maximum; never read it from model args (`coderai/tools/agent/__init__.py`).
- [x] **PR-A3** `allowedTools` for `--agent`/`/agent` (`cli/session_factory.py`, `soul/session/manager.py`) must be enforced both when building tool schemas and in the executor's permission check. `resolve_agent_spec` must not turn an empty tool list into `None` (`agentspec.py`).
- [x] **PR-A4** Built-in `explore`/`plan` subagents must get their YAML `ROLE_ADDITIONAL` prompt and `mode="read_only"` (`subagents/registry.py`, `builder.py`).
- [x] **PR-C1 / PR-C4** Frontmatter parsing must fail closed. Validate with a schema. Accept a comma-separated `tools:` string. On a YAML error, log and skip the spec (deny, don't fall back to unrestricted). `exclude_tools: null` must not crash discovery; `exclude_tools: bash` must not split into characters; `'false'` must parse as false. Replace the `except Exception: pass` blocks with warnings.
- [x] **AL-A2** Remove the global auto-approve check (`global_auto_approve_check`) from `Approval.request`; use the per-session check. Unregister session managers on dispose and close, and delete the dead register/unregister re-exports. Test: two managers, one in YOLO; the other still prompts.
- [x] **TL-B8** Enforce plan mode in the executor via `context.plan_mode`, not only upstream, so subagents can't bypass it.
- [x] **TL-A19** `ApprovalRuntime` is disconnected. Either route `Approval.request` through it, so pending/cancel/session caching work, or delete it along with its UI hooks. Ask the user if it's unclear.
- [x] **PR-B7** `/agent <role>` must rebuild the current session's system message and tool set, or clearly say it applies only to new sessions.

**Exit criteria:** a `security`-marked test file covers every item above, and `tests/test_security.py`, `tests/test_subagents.py`, `tests/test_approval_yolo.py` and `tests/test_plan_mode_tools.py` all pass.

---

## Phase 4 — Trust boundary, secrets and untrusted-output rendering

**Goal:** a cloned repo, a web page or an MCP server can't run code, steal keys or spoof the UI.

**4a. Workspace trust and config**
- [ ] **IN-A1** Add a per-project trust prompt, stored in the user directory. Until the project is trusted, ignore project-scope `mcpServers.*.command`/`url`, hooks, statusline command providers, `.coderai/config.toml` commands, project `.env`, and a project `baseURL` paired with a user-scope `apiKey`. Also cover non-interactive mode (`--print`, ACP): default to untrusted, with a flag or env var to opt in.
- [ ] **IN-A2** Write settings and config atomically with mode 0600 (`coderai/config.py`: `_write_settings_file`, `save_typed_config`). Make `~/.coderai` 0700 and log files 0600. Tighten existing files on startup.
- [ ] **IN-A16 / IN-B6** `--key --project` must not write keys into the repo's `.coderai/settings.json` without a warning. Comment out the non-empty overrides in `.env.example`.
- [ ] **IN-A14** Config parse errors print a warning once to stderr instead of silently dropping all settings, including permission deny lists.

**4b. Secrets in subprocesses, network and logs**
- [ ] **IN-A5** MCP stdio servers get `scrub_subprocess_env(...)` plus the per-server `env`, not `dict(os.environ)` (`coderai/mcp/transport.py`).
- [ ] **IN-A4** The SSE MCP `endpoint` event must be same-origin; otherwise reject it or strip auth headers. Re-run the SSRF check on it.
- [ ] **TL-A9 (env part)** `pwsh` must use `build_shell_env` like bash does.
- [ ] **WF-B2 (env part)** `hooks/runner.run_hook` / `HookEngine` must scrub the environment like `engine.run_hook_point` does.
- [ ] **IN-A12** `log.redact_secrets` must catch JSON-style `"Authorization": "Bearer ..."`, `access_token`/`refresh_token` and bare `sk-...` keys. Add tests.
- [ ] **IN-A10** The wire server must handle each request type explicitly and fail closed. A hook "block" answer or a disconnect must never become "allow". Also fix the `ToolCallRequest.resolve` arity bug.
- [ ] **TL-A17** Add `--unshare-net` to the Linux bwrap read-only mode (`coderai/sandbox.py`).
- [ ] **TL-A6** The shell redirect check must skip `danger-full-access` mode and stop matching redirects inside quoted strings (use a shlex-based parse). Don't pretend it's containment.
- [ ] **TL-A23 (custom search script)** Run the custom web-search provider script with a scrubbed environment (`coderai/web_providers.py`).
- [ ] **WF-B5 (triage)** `/review` must not send untracked files to TypeSafe/JEV or the model by default. Send only tracked diffs, or skip dotfiles and secret-looking paths (`coderai/triage/review.py`).

**4c. Untrusted text in the terminal and in prompts**
- [ ] **UI-A2 / UI-A3 / UI-A4 / UI-A13 / UI-A14** Render all untrusted text as `rich.text.Text` or with `markup=False`/`escape()`: tool output, web results, MCP tool names, session titles and filter text, the command and description in the approval card, `/agents` output, jobs/schedules/teams tables, MCP errors, `/btw` answers, queued/pasted echoes, and the `[coderai]` prefix in print mode. Add one `_emit_plain()` helper. Test the regression strings `[/usr/bin]`, `hello [/b] world` and `[link=x]click[/link]`.
- [ ] **UI-A6** `/logout` must edit the raw user and project settings files, remove `apiKey`/`providers.*.api_key`, and report exactly what was removed. It must not write merged settings back.
- [ ] **UI-A16** Pass `is_secret=True` when reading API keys in `coderai/ui/shell/setup.py`.
- [ ] **UI-A18** `/import` must not wrap file content in `<system>`. Resolve the path against the project root with `expanduser`, and remove or implement the "session_id" claim.
- [ ] **UI-A19** `/delete` with no args must ask for confirmation before deleting the current session.
- [ ] **PR-B4** Wrap AGENTS.md, rules files and skills in explicit tagged blocks with a "treat as data" preamble. Match skill names on word boundaries or tokens only. Escape `<system-reminder>` tags in tool output.

**Exit criteria:** new `security`-marked tests for each 4a and 4b item and for the markup regression strings. All affected test files pass. A manual check that `ls -l ~/.coderai/settings.json` shows 0600 after running once.

---

## Phase 5 — Agent loop and session correctness

**Goal:** compaction, cancellation, interrupts, plan mode and model requests behave correctly in the live `AgentLoop` / `SessionManager`.

- [ ] **AL-A1** Compute the compaction trigger and choose regions on `derive_messages(...)`, not the raw log. Give summary rows stable ids (the compactionId or event seq).
- [ ] **AL-A3** Send the compaction request with `tool_choice="none"`, or without tools. Abort without committing if the summary is empty. Use the retrying completion helper.
- [ ] **AL-A7** Recompute the token estimate after compaction before calling `apply_request_completion_cap`.
- [ ] **AL-A11** Place the summary where the first hidden message was, as a marked `user` message, not a mid-conversation `system` message.
- [ ] **PR-B5** Compaction directive: keep errors and fixes, verbatim user messages, exact paths, plan-mode state and plan path, loaded skills, background job and subagent ids, and todo state. Re-inject runtime state after compaction. Use one template; consolidate with `COMPACT_PROMPT_BASE` and `prompts/compact.md`.
- [ ] **AL-B4** One compaction-trigger function, reading one config source.
- [ ] **AL-A4** `AgentLoop.run` re-raises `CancelledError` after bookkeeping. User interrupts use a dedicated `SessionInterrupted` exception instead of a hand-made `CancelledError()`.
- [ ] **AL-A5** Wrap the turn in try/except/finally, so an error marks the session failed, emits `turn_end("error")`, and always pairs `compaction_begin` with `compaction_end`.
- [ ] **AL-A6** Replace an already-set interrupt Event on a fresh user turn. Emit `turn_end` and the final status in `finally`. The no-client path must also emit `turn_end`.
- [ ] **AL-A8** LLM streaming must be cancellable: check a cancel flag per chunk and close the response, or move to `AsyncOpenAI`. Stop `on_chunk` from calling into the UI after cancellation.
- [ ] **AL-A9** Build each fallback-model request from scratch with one shared builder, using the session's reasoning effort and correct provider keys. `CONTEXT_OVERFLOW` should compact and retry, not move to the next model.
- [ ] **AL-A10 / TL-A15 / AL-B3** One `set_plan_mode()` that updates the index entry, `SessionState` and the soul together; this replaces the dead `sync_session_state_from_entry`. One plan-file path resolver, used by `SessionSoul`, `tools/plan/heroes.py` and `enter_plan_mode`. Fix the `get_plan_file_path(project_root=...)` `TypeError`, and make sure the plan file is writable under the default `workspace-write` sandbox.
- [ ] **AL-B7 / PR-B6** Emit `AFK_DISABLED_REMINDER` once when AFK goes from on to off. Re-arm one-shot reminders after compaction (`notify_compacted` is never called).
- [ ] **AL-A15** On restart, rebuilding file state must not mark the current disk contents as "seen", and must not do blocking I/O inside async code.
- [ ] **AL-A16** Keep references to fire-and-forget tasks (hooks, AFK notify) in a module-level set, and log their exceptions.
- [ ] **AL-A17** Fix the model heuristics: `"mini"` must not match `gemini`, `"sol"` must not match `solar`, `minimal` maps to `low`, and `auto`/`adaptive` stay real values (`model_capabilities.py`, `openai_thinking.py`). Also AL-D5: unknown models should not default to multimodal.
- [ ] **AL-A18** Use exact session ids internally; fuzzy matching only at the CLI boundary.
- [ ] **AL-A19** Keep an in-memory checkpoint counter under `_seq_lock`.
- [ ] **AL-B5 / AL-B6** One token estimator and one tool-result truncation limit; one retryable status-code classifier (include 408, 409, 529).
- [ ] **AL-D1** Replace the `except Exception: pass` blocks in `manager.py`/`coderaisoul.py` that hide real failures with logging. At minimum: injection failures that already appended messages, and `maybe_run_ralph` failures that cause double execution.

**Exit criteria:** regression tests for A1, A3, A4, A5, A6, A10 and A17. `tests/test_session_engine.py`, `tests/test_phase2_engine.py`, `tests/test_plan_mode_tools.py` and `tests/test_prompt_context.py` pass.

---

## Phase 6 — Tools and shell hardening

- [ ] **TL-A5** Pass the model's `timeout_ms`, clamped between a minimum and a hard maximum, into `initial_timeout_ms`.
- [ ] **TL-A7** Cap the foreground bash output buffer with a head/tail ring buffer at `MAX_CAPTURE_CHARS`.
- [ ] **TL-A8** Validate a background `cd` with `resolve_exec_cwd` before updating the session cwd.
- [ ] **TL-A9 (rest)** `pwsh` shares bash's launcher: cwd resolution, process group, timeout from context, log handle closed, job-cap error handled.
- [ ] **TL-A10** `pwsh`/`terminal_send` background jobs complete when their process or PTY exits, so they stop counting against the job cap.
- [ ] **TL-A11** `edit` must not record its own observation before checking; compare against the last `read` observation.
- [ ] **TL-A13** Refuse to edit files that decode with replacement characters or look binary. Preserve line endings per line (`coderai/utils/path.py`).
- [ ] **TL-A18** Readers of `JsonlSessionStore` must not run `cleanup_orphan_tmps()` (add `cleanup=False`).
- [ ] **TL-A20** `Think` returns the thought as the tool result only, without inserting an assistant message mid tool-call.
- [ ] **TL-A22** On executor timeout, keep the path lock until the thread finishes, or make handlers support cancellation.
- [ ] **TL-A23 (rest)** Guard `int(...)` in `web/fetch.py`/`web/search.py`. Clean up Seatbelt profile temp files. Make `_UNDO_HISTORY` per session and apply sandbox checks to undo. Remove the hardcoded `"gpt-6-luna"` fallback.
- [ ] **TL-B3** Make schemas and handlers agree: declared `mode`, WebFetch `raw`/`max_length`/`use_cache`, integer types for `offset`/`limit`/`timeout_ms`, and reject unknown keys. Add a schema/handler parity test over the whole registry.
- [ ] **TL-B5** `extract_key_argument` uses the live tool names.
- [ ] **TL-B6** Resolve relative paths the same way across read, grep, glob, write and edit, and in the path lock (`isolated_cwd` vs `project_root`).
- [ ] **UI-A17** PTY terminal buffers: enforce `DEFAULT_MAX_BUFFER_CHARS`, use an incremental UTF-8 decoder, and loop on partial or `EAGAIN` writes (`coderai/terminal/manager.py`).

**Exit criteria:** regression tests for each item. `tests/test_tools.py`, `tests/test_tool_platform.py`, `tests/test_phase6_tooling.py`, `tests/test_atomic_file_ops.py` and `tests/test_web.py` pass.

---

## Phase 7 — Workflows, orchestration and event-loop hygiene

- [ ] **WF-A2** Subagent runners re-raise `CancelledError`; only map to "interrupted" for the manager's own `abort_event` (`subagents/runner.py`, both run paths).
- [ ] **WF-A7** Single ownership of `JobStore`, `AgentRegistry` and `ScheduleManager`: either inject the SessionManager's instances into the tool context, or have the manager reference the globals. The schedule store must persist. `/agents`, `/jobs`, `/schedule` and `close_session_manager` must see the real data.
- [ ] **WF-A8** Continuable workers get a real kill (a terminal flag plus `handle.task.cancel()`), an idle TTL, eviction of terminal handles, and count toward the spawn cap.
- [ ] **WF-A9** No automatic replies to acknowledgements. Drain or cap the teammate inbox and outbox. Make `wait_agent(wait_for="message")` track unread messages.
- [ ] **WF-A11** Run hooks asynchronously (`run_hook_point_async` or `to_thread`), cap the timeout, use `start_new_session=True`, and kill the process group on timeout.
- [ ] **WF-A12** Default the subagent `isolated_cwd` to the project root; use the scratchpad only when isolation is requested, and clean it up.
- [ ] **WF-A13** Background subagent job ids come from a monotonic counter or uuid; pop the task entry in `finally`; handle the job-cap `RuntimeError`; `JobStore.kill` cancels process-less jobs through a callback.
- [ ] **WF-A14** External CLI backends use `asyncio.create_subprocess_exec` and kill the process group on cancel. This is currently latent; do it, or delete the backends in Phase 9.
- [ ] **WF-A16** Team tasks get the session's project root, session id and depth. Ready tasks are auto-started.
- [ ] **WF-A17** Subagent hooks pass the real parent session id; `SubagentStop` fires from `finally`; continuable runs fire hooks too.
- [ ] **WF-A18** Goals: advance rounds and enforce `max_rounds` from the turn loop, key the store by project root, write atomically, validate `max_rounds`, and allow only one running goal. Or, if goals are meant to be a note store, simplify and document that.
- [ ] **IN-A6** Disconnect the old MCP client before replacing it; wrap each disconnect in `try` so one failure doesn't stop the rest.
- [ ] **IN-A7** MCP stdio writes go off the event loop; errors propagate to pending futures. Streamable HTTP: honour `Mcp-Session-Id`, set `last_http_status`, and don't swallow POST errors.
- [ ] **IN-A8** OAuth device flow: handle `access_denied`, `slow_down` (+5s) and `expires_in`; no unbounded recursion.
- [ ] **IN-A13** Config precedence: pass `project_root` through, make project `config.toml` beat user `settings.json` as documented, and don't mix model/baseURL/apiKey across layers. Fix the typed-config cache key so it also covers the global file.
- [ ] **IN-A15** Loading the typed config must not write files.
- [ ] **IN-A17** ACP terminal connect removes its pending request on cancel; add `raise ... from`.
- [ ] **UI-A7** `!` shell mode uses an asyncio subprocess with SIGINT forwarding, and prints output as `Text`.
- [ ] **UI-A8** One consistent Ctrl-C model: track the current cancellable task for slash commands too, reset the counter, remove the dead sync handler, and exit with 130 on interrupt.
- [ ] **UI-A9 / UI-A10** Approval and question prompts, the `input()` calls and `cmd_upgrade` don't block the loop. Ctrl-O uses prompt_toolkit's `open_in_editor`/`run_in_terminal` and quotes the path.
- [ ] **IN-D4 / IN-D5** Keep telemetry task references. Move the blocking stdout writes in the wire server off the loop. Don't build a new OpenAI client on every prompt.

**Exit criteria:** regression tests for WF-A2, A7, A8, A9, A11 and IN-A6, A8, A13. `tests/test_subagents.py`, `tests/test_orchestration.py`, `tests/test_mcp_plugins.py`, `tests/test_hierarchical_config.py`, `tests/test_config_setup.py`, `tests/test_acp_*.py` and `tests/test_cli.py` pass.

---

## Phase 8 — Prompt ↔ code alignment

- [ ] **PR-A5** `render_system_prompt` renders through the Jinja path with a filled `BuiltinSystemPromptArgs` and fails on undefined variables. Test: no `${` or `{%` in the rendered `--agent default` prompt.
- [ ] **PR-A8 / PR-B9** Generate the "Available Tools" section from the live `get_tools(options)` names. Remove `get_goal`/`create_goal`/`update_goal`, fix the `goal` guidance, and stop duplicating the schema descriptions.
- [ ] **PR-A9 / TL-B1** `enter_plan_mode` description: use the real tool names (`enter_plan_mode`, `exit_plan_mode`, `read`, `glob`, `grep`, `Task`/`subagent`).
- [ ] **PR-B3** One plan-mode workflow, rendered into the system prompt section, the injection and the tool description from a single source. Resolve the conflicts with AFK mode and non-interactive mode. `exit_plan_mode` describes user approval and `summary`.
- [ ] **PR-B2** Move the safety guardrails (no git mutation without approval, stay inside the working directory, reply in the user's language) into `SYSTEM_PROMPT_BASE`. Make the reproduce-test-first workflow conditional on bug fixes.
- [ ] **PR-B1** Rewrite `.coderai/agents/code-reviewer.md` as a generic, non-contradictory checklist, and remove the benchmark-specific identifiers.
- [ ] **PR-A10** `extend:` resolves inherited relative paths from the directory each value came from; an explicit `null` overrides the inherited value; child specs get their own names.
- [ ] **PR-A11 / PR-A12** Update `AGENTS.md` (okabe description, read-only roles, discovery paths including `~/.coderai/agents`, what the `tools:` lists actually do) and `system.md` (Task params and tool names).
- [ ] **PR-B8** One `/init` prompt.
- [ ] **PR-B9 (instructions loading)** Load AGENTS.md hierarchically (root + `.coderai/` + user-level), and warn when a file is truncated.
- [ ] **PR-C2** Wire `model:`, `exclude_tools` and `when_to_use` into subagent specs. Generate the `subagent_type` description from the registry so discovered roles are visible. Warn when a builtin shadows a custom role.
- [ ] **PR-C3** Cap flow decision retries at about 3, count them as moves, and match case-insensitively (`coderai/skill/flow/runner.py`).
- [ ] **PR-D4** Merge `coderai/prompt/` and `coderai/prompts/` into one package with a templates folder and one loader.
- [ ] **PR-D5** Fix the skill docs (`coderai-self-refer`, `image-generator`).
- [ ] **UI-B3 / IN-B5** Help text matches behaviour (`/plan reset`, unknown args, `/goal start`, `/raw`, `/mcp` parsing). Update `docs/cli.md` for the real flags (remove `--list-sessions` or implement it).

**Exit criteria:** a prompt-render test for each agent spec (no unresolved placeholders, tool names all exist in the registry). `tests/test_prompt_context.py` and `tests/test_subagents.py` pass.

---

## Phase 9 — Consolidate duplicate systems, split large files, final dead-code sweep

Do this last, after behaviour is correct and covered by tests. Each bullet is a refactor, so keep it separate and behaviour-preserving.

**Consolidation**
- [ ] **UI-B2** One slash-command registry (decorator-based); catalog, help and completion are generated from it. Resolve the swapped canonical names (`rename`/`title`, `resume`/`sessions`) and `/reset` semantics.
- [ ] **AL-B2 / AL-B8** One session store: retire or merge `coderai/session.py` and `coderai/session_state.py`, rename `coderai/state.py` to something like `file_snippets.py`, and deduplicate index-entry creation.
- [ ] **WF-B1** One `build_spec(context, args)` for all spawn paths, using `orchestration.resolve_subagent_defaults` (so the env vars work) instead of the hardcoded 90s / depth 3.
- [ ] **WF-B2** One hook executor: Claude-compatible (exit 2 = block), with a scrubbed environment and a single `DEFAULT_HOOK_TIMEOUT_SECONDS`.
- [ ] **WF-B3 / WF-B4** One event vocabulary for the subagent lifecycle. Either fire the `HookPoint`s that config accepts or remove them from the config Literal. Make the documented env vars work or remove them.
- [ ] **IN-D2 / IN-D3** One secret-env scrubber, one redactor, one key-masking helper.
- [ ] **IN-B2** Every entry point goes through `cli.__main__:main`, with crash handlers and a shutdown phase.
- [ ] **IN-B3 / IN-B4** Make the telemetry config keys work or remove them. Honour `CODERAI_SHARE_DIR` everywhere, and fix the migration.
- [ ] **IN-B7** One logging system (route stdlib loggers into loguru, or the other way round).
- [ ] **IN-B1** Declare `tenacity` and `certifi` in `pyproject.toml`, fix the loguru comment, and regenerate `requirements.lock`.
- [ ] **TL-B7 / TL-B8** One atomic-write helper (preserve file mode). Registry `rate_limit`/`guard()`: implement them or remove them.

**File splits** (move code only, no behaviour change)
- [ ] **IN-D1** Split `coderai/config.py` into settings, typed config and provider registry, with one precedence resolver.
- [ ] **UI-D1..D5** Split `ui/shell/prompt.py` (4.1k lines), `ui/shell/app.py` (2.9k), `visualize/_blocks.py` (2k), `dispatch.py`/`slash.py` and `session_picker.py` along the seams listed in the UI report. Deduplicate the helpers from UI-B4.
- [ ] **WF-D4** Extract a `SubAgentResult` builder helper (about 10 copies in `runner.py`).

**Final dead-code sweep**
- [ ] Remove the remaining verified dead symbols listed in **DC-A** and in each report's section C (WF-C, TL-C, AL-C, UI-C, IN-C). Re-verify each with `rg -w` across `coderai tests tests_e2e scripts sdks` and `coderai/agents/**/*.yaml` before deleting. Keep the vulture false-positive categories listed in DC-H.
- [ ] Resolve the **DC-B** test-only production code: delete it, or keep it with a reason.
- [ ] **DC-D** Remove the remaining duplicated function bodies.
- [ ] **DC-E** Remove or wire up the unused config keys (`BackgroundConfig`, `default_yolo`, `default_plan_mode`, `OAuthRef.storage`, and others) and the constants that are referenced only where they're defined.
- [ ] Ruff cleanup: fix the real B904 hits. Optionally adopt `B`, `SIM` and `RET` in `pyproject.toml` once they're clean.
- [ ] Re-run `vulture` and the orphan scan (scripts in `static-analysis/`), and save the new outputs next to the old ones as `*-after.txt`.

**Exit criteria:** vulture at min-confidence 60 reports only documented false positives; no orphan modules remain; every test file passes (one process per file); ruff is clean.

---

## Starting a phase in a new session

Use `PHASE-1-PROMPT.md` for Phase 1. For later phases, copy it and change the phase number and the "Scope" line; everything else stays the same.

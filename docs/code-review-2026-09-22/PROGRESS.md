# Remediation progress log

Add one entry at the end of each phase session (newest last). The plan and its checkboxes are in `PLAN.md`; the findings are in `findings/`.

Template:

```
## Phase <N> — <YYYY-MM-DD>
- Done: <IDs>
- Skipped/invalid: <ID — reason>
- Blocked: <ID — reason>
- Tests run: <file — result> ...
- Notes / follow-ups: ...
```

## Phase 0 — 2026-09-22
- Review completed (read-only). Reports saved in `findings/`, raw static-analysis output in `static-analysis/`, plan in `PLAN.md`.
- No code changes yet.

## Phase 1 — 2026-09-22
- Done: WF-A1, TL-A1, IN-A3, UI-A1, UI-A5, UI-A12, UI-A11, TL-A21, TL-A3, DC-E, TL-A4, TL-A12, TL-A16, TL-A14, WF-A10, WF-A15, UI-A15, IN-A9, IN-A11
- Skipped/invalid: none
- Blocked: none
- Tests run: `tests/test_review_phase1.py` (25 passed, new regressions). Also one-file-per-process: test_tools, test_tool_platform, test_cli, test_mcp_plugins, test_prompt_context, test_config_setup, test_hierarchical_config, test_atomic_file_ops, test_ui_p0_fixes, test_ui_review_remediation, test_subagents, test_security, test_background_wire, test_acp_server, test_approval_yolo, test_plan_mode_tools, test_review_command, test_review_fixes, test_runtime_parity, test_phase2_engine, test_phase4_ui, test_cancellation_resilience, test_coderaisoul_lifecycle, test_visualize_wire_loop, test_e2e, test_orchestration — all passed. `ruff check coderai --select F` clean. `ruff format --check` clean on edited files except a pre-existing wrap in `config.py` (~1494) that this phase did not touch.
- Notes / follow-ups:
  - TL-A16: `merge` was removed from the `todo_write` schema. It was never implemented; `merge=False` was being passed as the plan explanation. Callers that still send `merge` are ignored. `explanation` is accepted instead.
  - TL-A14: `cron_expression` was removed from `schedule_create`. The schema now advertises `after_seconds`, `at`, and `every_seconds`, which is what the handler reads. Cron is still not implemented.
  - WF-A10: `targetTimestamp` is persisted, and older files fall back to parsing `scheduledAt`. Dispatched records are still not pruned.
  - WF-A15: non-finite `wait_agent` timeouts (`inf`, `nan`) fall back to 60s, then the value is clamped to 0–600.
  - IN-A11: `doctor` no longer creates `<project>/.coderai/`. The home-directory writability probe can still create `~/.coderai/`.
  - IN-A9: the pre-lock tombstone check already `continue`d. The in-lock `return` (the one that skipped later keys) is now `continue`.
  - Pre-existing `ruff` UP035 in `coderai/jev/client.py` is outside this phase.

## Phase 2 — 2026-09-23
- Done: decision gate (a) Delete it; removed `coderai/app.py`, `KimiSoul`/`CoderAISoul`/`StepOutcome`/`TurnOutcome`/`BackToTheFuture`/duplicate `FlowRunner`/`AgentLoop.type_check` from `coderaisoul.py`, `soul/context.py`, `soul/slash.py`, `run_soul`+helpers from `soul/__init__.py`, `load_agent`/`Runtime`/`Agent` from `soul/agent.py`, `CoderAIToolset`/`KimiToolset` from `soul/toolset.py`, all kosong `CallableTool2` classes in `tools/*` (SendDMail, AskUserQuestion, Shell+kaos fast path, Agent, ReadFile, Glob, Grep, ReadMediaFile, WriteFile, StrReplaceFile, EnterPlanMode, ExitPlanMode, Plus/Compare/Panic), the `Shell` class + `render_welcome_screen` moved to `ui/shell/welcome.py` (thin `__init__`), dead `visualize()`/`_live_view.py`/`_interactive.py`/`replay.py`/`CustomPromptSession`/`ui/print/visualize.py`/`echo.py`/`render_mcp_prompt`/`print_migration_goodbye`, orphans (`tools/test.py`, `background/ids.py`, `utils/environment.py`, `ui/shell/export_import.py`, `wire/serde.py` with test repointed at `wire.types`), `utils/shell_quoting.py` (3 importers repointed at `subprocess_env`), dead Kimi branding, stale `build/lib/coderai/app.py`, and 20 dead-only tool `.md` files. Ported `test_classify_api_error_and_retryability` to `tests/test_session_engine.py`.
- Skipped/invalid: none. `background/worker.py` kept per explicit user decision (stays test-covered by `test_background_wire.py`).
- Blocked: none.
- Tests run: all 32 remaining `tests/*.py` files, one file per process — all passed (test_jev_calibration skips: missing optional `typesafe_sdk`, pre-existing). Deleted `test_coderaisoul_lifecycle.py`, `test_context_lifecycle.py`, `test_cancellation_resilience.py`, `test_visualize_wire_loop.py`; trimmed `test_phase4_ui.py` (kept branding/theme/mcp-console/nudge/update/startup tests) and `test_atomic_file_ops.py` (kept `atomic_*` io tests); `test_runtime_parity.py` kept as-is (live checkpoint assertions). `python -c "import coderai.main, coderai.ui.shell.app, coderai.soul.session.manager, coderai.acp"` succeeds. Exit-criteria grep returns only intentional matches (live `*_interactive` pickers, wire/ACP replay, llm-replay test helper). `ruff check coderai --select F` clean; `ruff format --check` clean on changed files (only remaining ruff error repo-wide is the pre-existing UP035 in `jev/client.py`).
- Notes / follow-ups:
  - Salvaged live work from the same files: `SessionSoul.checkpoint_count` change, reworked `handle_send_dmail_tool` (`stage_dmail`), `enter/exit_plan_mode` handler metadata, `utils/io.atomic_*`, TL-A3 printf fix, `render_welcome_screen`. All Phase 1 fixes verified intact (slash guard, stream reset, prompt Ctrl-C, dmail NAME-first import, todo/schedule schemas).
  - `run_soul` cancellation semantics were deleted with the dead runtime; the live `AgentLoop`/`SessionManager` has no equivalent yet — port coverage belongs to Phase 5 (AL-A4/A5/A6). Likewise the dead `Shell` subprocess-cancellation test belongs to Phase 6 (TL-A22).
  - Left for later phases (deliberately not touched): `MCPTool`/`WireExternalTool`/`convert_mcp_tool_result` in `toolset.py` (now uninstantiated; Phase 9 DC-A), `FileOpsWindow`/`FileActions` (Phase 9 DC-A/DC-B), `extract_key_argument` dead tool names (Phase 6 TL-B5), `get_plan_file_path(project_root=...)` TypeError still swallowed in `enter.py` (Phase 5 AL-A10), `enter_description.md` naming removed tools (Phase 8 PR-A9), `BuiltinSystemPromptArgs` kept for Phase 8 (PR-A5).

## Phase 3 — 2026-09-23
- Done: WF-A5 / PR-A1 / PR-A2, WF-A4, WF-A6 / TL-A2, WF-A3, PR-A3, PR-A4, PR-C1 / PR-C4, AL-A2, TL-B8, TL-A19, PR-B7
- Skipped/invalid: none
- Blocked: none
- Tests run: `tests/test_review_phase3.py` (18 passed, `@pytest.mark.security` covering every Phase 3 item). Also one-file-per-process: `tests/test_security.py` (19 passed), `tests/test_subagents.py` (21 passed), `tests/test_approval_yolo.py` (17 passed), `tests/test_plan_mode_tools.py` (10 passed), `tests/test_session_engine.py` (31 passed), `tests/test_prompt_context.py` (43 passed) — all passed. `ruff check coderai --select F` clean (0 errors). `ruff format --check` clean on all touched files.
- Notes / follow-ups:
  - WF-A5 / PR-A1 / PR-A2: Explicit `is not None` checks used in `builder.py`, `registry.py`, `runner.py`, and `executor.py` so empty lists `[]` enforce deny-all rather than fallback to permissive. Unknown `subagent_type` defaults to `mode="read_only"` with `allowed_tools=()`. Explicit `mode="read_only"` is never widened by role mode.
  - WF-A4: Real read-only enforcement in both tool listing and runtime dispatch (`runner.py`), filtering mutating tools (`is_mutating`, `str_replace_editor`, `terminal_*`, `schedule_*`, `spawn_teammate`), blocking shell access unless sandboxed with `sandbox_mode == "read-only"`, and denying spawning of writable child agents from read-only parents.
  - WF-A6 / TL-A2: Child executions inherit parent's approval runtime, sandbox mode, plan mode, and checkpoint hooks. Spawn approval in `soul/approval.py` reflects actual role and mode capabilities (assigning `write-in-cwd` for general mode and `[]` for read-only mode). Parameter schemas for `Task`, `subagent`, and `subagent_fork` now explicitly declare `mode` enum (`read_only` / `general`).
  - WF-A3: Registered all child executions (including foreground `Task`) in `AgentRegistry` with session ID and depth. Monotonically derived depth from lineage records and clamped `max_depth` to `MAX_SUBAGENT_DEPTH` (never trusting model arguments).
  - PR-A3: `allowedTools` in session settings is enforced when generating OpenAI tool schemas in `to_openai_schemas` and at execution time in `ToolExecutor._pre_execute_deny` with alias harmonization. `resolve_agent_spec` preserves empty tool lists (`is not None`).
  - PR-A4: Builtin `explore` and `plan` subagents carry `ROLE_ADDITIONAL` prompt and default to `mode="read_only"`.
  - PR-C1 / PR-C4: `_parse_frontmatter` fails closed on YAML errors (returning `None` with warning, deleting the vulnerable line-fallback parser). Frontmatter parses comma-separated `tools:` strings, safe handles `exclude_tools: null`, preserves non-iterable strings as singletons without character-splitting, and parses `'false'` as boolean `False`.
  - AL-A2: Removed `global_auto_approve_check` in favor of per-session `check_auto_approve_for_session`. Unregistered session managers on `dispose()` and `close_session_manager()`. Deleted dead re-exports.
  - TL-B8: `ToolExecutor._pre_execute_deny` blocks mutating tools when `context.plan_mode` is active (except plan-authoring tools).
  - TL-A19: `Approval.request` routes all requests through `ApprovalRuntime`, enabling request lifecycle tracking, pending list queries, wire bridging, and clean cancellation.
  - PR-B7: `SessionManager.switch_agent_role` accepts `session_id`, updates persona and allowedTools, rebuilds the session's system message row in `session_store`, and invalidates message cache. `/agent <role>` in `dispatch.py` passes `session_id=ctx.session_id`.
  - Follow-up for Phase 4: Proceed with Phase 4 (trust boundary, secrets, untrusted-output rendering; IN-A1, IN-A2, IN-A16/IN-B6, etc.).

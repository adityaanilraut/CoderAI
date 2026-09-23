<!-- Source: review agent 89467d53-8b58-4b45-a24e-fe01fcf27c47 · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# Dead code, duplication & test hygiene audit

The biggest problem isn't in vulture's list. The uncommitted working tree brings back the "Kimi runtime" that commit `5e06ff6` ("retire the Kimi runtime and remove all remaining dead code", Sep 10) deliberately deleted. That's about 7,000 lines, and nothing reachable from the `coderai` entry point uses it. Its only production root is the untracked `coderai/app.py`, which is imported nowhere and would crash if called. Vulture misses the cluster because its parts reference each other, and some names collide with live code: `Shell` is also a tool class.

I didn't modify anything in the repo. Scripts and outputs are in `/tmp/rv/audit2/`.

## Resurrected runtime cluster (production-dead)

| Piece | Location | Status |
|---|---|---|
| `CoderAIApp` / `KimiCLI` | `coderai/app.py` (293 lines) | Untracked; deleted in `5e06ff6`. `run_print` imports `coderai.ui.print.Print`, which doesn't exist. `run_acp` imports `coderai.ui.acp`, which doesn't exist. |
| `KimiSoul` (plus `CoderAISoul` alias), `StepOutcome`, `TurnOutcome`, `BackToTheFuture`, `FlowRunner` | `soul/coderaisoul.py:984–2745` | Adds 2,133 lines vs HEAD. Only `app.py:168` and `tests/test_coderaisoul_lifecycle.py` instantiate `KimiSoul`. This `FlowRunner` duplicates `skill/flow/runner.py:50`, which is the live one (used at `session/manager.py:1147`). |
| `Shell` class, `_PromptEvent`, `_BackgroundCompletionWatcher` | `ui/shell/__init__.py:69–1471` | Adds 1,597 lines vs HEAD. Only `app.py:243` constructs it. Live code only uses `render_welcome_screen` from this module. |
| `visualize()` function | `ui/shell/visualize/__init__.py:114` | Adds 178 lines vs HEAD. Called only from `Shell` and `replay.py`. |
| `_live_view.py` (935), `_interactive.py` (530), `replay.py` (215) | `ui/shell/visualize/`, `ui/shell/` | Untracked; all deleted in `5e06ff6`. Reachable only through `Shell` / `visualize()`. |
| `soul/context.py` `Context` (317 lines) | | Used only by `app.py`, `KimiSoul`, and tests. |
| `soul/slash.py` registry (274 lines) | | Used only by `KimiSoul` (`coderaisoul.py:1627`). |
| `CoderAIToolset` / `KimiToolset` | `soul/toolset.py:210–1020` | Used only via `load_agent` (`soul/agent.py:259`, called only by `app.py`) and `KimiSoul`. The context helpers in the same file (e.g. `get_current_tool_call_or_none`) are live. |
| `run_soul` | `soul/__init__.py:234` | Called only by `app.py` and tests. |
| `ui/print/visualize.py` (196 lines) | | Test-only (`test_phase4_ui`). It's missing from `orphans.txt`. |

**Recommendation:** delete the cluster, or deliberately re-wire it. Since `pyproject` packages `coderai*`, the local `build/lib` already contains `app.py`, so any wheel built from this tree would ship it.

## (A) Verified DEAD code
"Dead" here means zero references in `coderai/` (Python and YAML), `tests/`, `tests_e2e/`, `scripts/`, `sdks/`, `pyproject.toml` or `coderai.spec`, other than the definition itself, docstrings, or other dead code.

- **acp**
  - `acp/kaos.py:46` class `ACPProcess` (119 lines): only listed in `__all__`.
  - `acp/session.py:64` `get_current_acp_tool_call_id_or_none` and `:77` `register_terminal_tool_call_id`: only in `__all__`.
  - `acp/engine.py:89` `build_prompt`: only in `__all__`.
- **app.py** (the whole module): `:76` `_write_original_stderr`, `:185` `run_soul_stream`, `:236` `run_shell`, `:246` `run_print`, `:267` `run_acp`.
- **approval_runtime/runtime.py:** `:67` `create_request`, `:93` `wait_for_response`, which makes `:210` `_publish_wire_request` dead as well.
- **auth/oauth.py:**
  - `:314` `save_tokens`.
  - `:489` `OAuthManager.from_typed_config`. It is the only reader of `services.moonshot_*`.
  - `:602` `login_device_flow` (61 lines): only called by a dead wrapper at `:640`, plus `__all__`.
- **background:**
  - `agent_runner.py:34` `record_heartbeat`, `:134` `kill_task`, `:171` `check_liveness`.
  - `ids.py:16` `generate_task_id` (the whole module).
  - `manager.py:15` `reset_job_store`: test-only.
- **cli:**
  - `doctor.py:35` `has_errors` and `:39` `has_warnings`.
  - `image_attachment.py:122` `paste_clipboard_images`.
  - `__init__.py:8` `ExitCode` (the SUCCESS, FAILURE and RETRYABLE members).
  - `elapsed.py:50` `estimate_tokens_float`: only used by its back-compat wrapper.
- **config.py:** `:360` `get_default_auto_compact_window`, `:1051` `save_base_url_setting`, `:1414` `clear_typed_config_cache`.
- **constant.py:** `:52` `get_build_sha` (52 lines) and `:36` `_normalize_remote`. Both appear only in `__all__`.
- **events.py:** `:313` `make_request_header`, `:306` `make_tool_call_event`.
- **goals/core.py:** `:22` `GoalRef`, `:172` `GoalStore.advance_round`.
- **hooks:**
  - `config.py:113` `MergedHookOutcome.is_allowed`.
  - `engine.py`: `:522` `fire_and_forget_trigger`, `:544` `add_hooks`, `:548` `add_wire_subscriptions`, `:552` `set_callbacks`, `:562` `has_hooks`, `:566` `has_hooks_for`.
  - `engine.py:383` `run_hook_point_async` (65 lines) and `:205` `execute_hook_command_async` (76 lines): only in `__all__` and each other.
  - `events.py:204` `session_start`, `:213` `session_end`, `:222` `subagent_start`, `:236` `subagent_stop`. The live payloads are built in `hooks/runner.py`.
  - `runner.py`: `run_*_async` at `:524`, `:541`, `:558`, `:579`, `:600` and `:615`; `run_stop_failure` `:344`; `build_pre_tool_use_payload` `:162`; `build_post_tool_use_payload` `:178`; `run_pre_tool_use` `:29`. All appear only in `hooks/__init__.__all__`.
- **llm.py:** `:330` `estimate_message_tokens`, `:411` `is_always_thinking`, `:416` `supports_media`, `:421` `augment_provider_with_env_vars`, `:662` `clone_llm_with_model_alias`, and the alias `:1415` `check_provider_connectivity`.
- **mcp:**
  - `client.py:100` `set_notification_handler`.
  - `manager.py`: `:125`, `:139` and `:143` (the session tool-mask trio), `:160` `eject_server`, `:195` `reload_server`, `:199` `list_active_servers`, `:206` `set_on_status_changed`, `:331` `auto_heal_servers` (which makes `probe_health` `:316` dead), `:375` `hot_reload_tools`, `:704` `get_mcp_prompt` (which makes `McpClient.get_prompt` dead), `:730` `read_mcp_resource`.
  - `mcp_oauth.py:122` `create_mcp_oauth`, `:132` `has_mcp_oauth_tokens`.
- **orchestration.py:** `:59` `stop_reason_error`, `:149` `get_orchestration_event_bus`, `:204` `resolve_child_depth`, `:229` `auto_max_concurrent_agents`, `:235` `resolve_subagent_defaults`, `:266` `resolve_goal_defaults`, `:278` `resolve_max_parallel_tool_calls`.
- **prompt/__init__.py:** `:907` `_project_guidance`; `:256` `get_compact_prompt_token_threshold` (only in `__all__`); `:611` `build_cache_stabilized_messages` (only in `__all__` and tests).
- **sandbox.py:269** `sandbox_available`.
- **session.py:** `:268` `Session.list_all`, `:278` `Session.continue_`.
- **soul:**
  - `approval.py`: `:319` `clear_session_tickets`, `:1054` `Approval.share`, `:1057` `set_runtime`, `:1074` `set_runtime_afk`, `:918` `normalize_ask_permissions` (only in `__all__`).
  - `coderaisoul.py:876` `type_check`. It is mis-indented, so it has become an `AgentLoop` method. Also `:1132` `add_injection_provider` and `:1303` `set_plan_mode_from_manual`.
  - `dynamic_injections/plan_mode.py:97` `_has_plan_reminder`.
  - `btw.py:140` `inject_dmail_and_continue` and `compaction.py:91` `prune_tool_results_for_compaction`: only in `__all__`.
  - `session/approval.py:12` `register_session_manager` and `:18` `unregister_session_manager`, plus the unused `noqa` re-exports at `session/manager.py:101–110`. The `_global_afk_check` and `_check_afk_for_session` re-exports are live (used by `tools/ask_user`).
  - `session/manager.py`: `:438` `sync_session_state_from_entry`, `:457`/`:480`/`:490` (the permission-ticket API), `:877` `_create_empty_session`, `:2336` `sync_mcp_servers` (which makes `McpManager.sync_servers` dead), `:2479` `dispose`.
  - `session/models.py:288` `copy_message_with`.
  - `session/store.py:206` `delete_log`.
  - `toolset.py:239` `hide`, `:246` `unhide`, `:317` `dedup_triggered`, `:596` `register_external_tool` (which makes `WireExternalTool` `:908` dead), `:647` `has_deferred_mcp_tools`, `:729` `_check_oauth_tokens`, `:742` `_mark_oauth_unauthorized`.
  - `tool_context.py:22` `set_current_tool_call` and `:37` `get_current_step_no`: only in `__all__`.
- **state.py:** `:241` `_set_file_version`; `:257` `was_file_read` along with its method `:128`.
- **subagents:**
  - `core.py`: `:155` `list_descendants`, `:167` `get_tree`, `:230` `interrupt_tree`.
  - `store.py:69` class `SubagentStore` (155 lines, every method): only in `__all__`.
  - `builder.py:117` `SubagentQuotaConfig`, `models.py:33` `SubagentRunRecord`, `registry.py:183` `list_subagent_types`, `:234` `is_tool_allowed`, `backends/acp.py:14` `AcpSubagentDriver`: only in `__all__`.
  - `runner.py:528–539` is unreachable: the fallback `return` after `while True:` (`:452`), which has no `break`.
- **teams:**
  - `concurrency.py:50` `cas_retry_async` and `:97` `cas_retry_sync`, which makes `calculate_backoff_delay` dead.
  - `deadlock.py:109` `release_wait`.
  - `mailbox.py:168` `publish_async` and `:188` `broadcast_async`, which makes `AsyncMailbox.send_async` dead.
  - `manager.py:37` `_validate_dag`, `:295` `get_messages`.
- **telemetry:**
  - `crash.py:139` `install_asyncio_handler`.
  - `sink.py:116` `get_active_trace_id`.
  - `__init__.py:54` `set_client_info` and `:67` `track_session_started_once` (34 lines): only in `__all__`.
- **terminal/manager.py:213** `get_full_output`.
- **tools:**
  - `file/replace.py:917` `_find_line_numbers`.
  - `file/utils.py:138` `async_write_file_with_locks`.
  - `legacy/registry.py`: `:178` `suppress_tool`, `:197` `is_tool_suppressed`, `:227` `set_session_mask`, `:241` `clear_session_mask`, `:292` `get_tool`.
  - `legacy/types.py:139` `defer_context`, `:143` `conclude_turn`.
  - `utils.py:80` `n_chars`.
  - `file/__init__.py:6` `FileOpsWindow`.
  - `test.py` (the whole module): `Plus`, `Compare`, `Panic`.
- **utils:**
  - `aiohttp.py:266` `post_async`.
  - `common/file_history.py:468` `_is_same_entry`.
  - `common/message_converter.py:192` `build_messages`, `:196` `get_trailing_pending_tool_calls`.
  - `common/model_capabilities.py:211` `format_capability_badges` and `:219` `format_model_effort_tags`, which makes `get_supported_reasoning_efforts` `:101` dead.
  - `common/openai_thinking.py:141` `resolve_reasoning_key`.
  - `common/repeat_tool_reminder.py:78` `args_hash` (a copy of `soul/toolset.py:93` `_args_hash`), `:165` `observe_legacy`, `:177` `should_force_stop`.
  - `diff.py:17` `format_unified_diff`.
  - `file_filter.py:215` `list_directory_filtered`.
  - `logging.py:43` `_mask_sensitive`.
  - `path.py:21` `WriteFileAtomicOptions`, `:296` `shorten_home`, `:420` `find_project_root`.
  - `rich/syntax.py:83` `resolve_color_system`.
  - `slashcmd.py:24` `slash_name`, `:101` `iter_command_entries`.
  - `term.py:183` `get_cursor_row`.
  - `shell_quoting.py:91` and `subprocess_env.py:91` `windows_path_to_posix_path` (both copies).
  - `subprocess_env.py:250` and `environment.py:21` `scrubbed_parent_env` (both copies).
- **wire:**
  - `emitter.py:47` `attach_session_wire`, `:51` `detach_session_wire`, `:168` `hook_resolved`.
  - `file.py:159` `append_record`, `:164` `replay_sync`, `:191` `dump_line`.
  - `jsonrpc.py`: `:56`, `:62` and `:65` (predicates), `:103`, `:107`, `:114` and `:118` (message builders), `:122` `ClientInfo`, `:128` `ExternalTool`, `:150` `JSONRPC_OUT_METHODS`, `:24` `AUTH_EXPIRED`.
  - `types.py:279` `QuestionResponse`, `:360` `HookResponse`.
- **ui** (whole functions and classes only):
  - `ui/shell/app.py`: `:67` `error_callout`, `:331` `_live_deny`, `:1595` `_render_help_menu`, `:1600` `_show_diff`; `_StreamState` methods at `:1256`, `:1274`, `:1294` and `:1401`.
  - `keyboard.py:89` `listen_for_keyboard`.
  - `migration_nudge.py:21`, `:31`, `:58` and `:68` (four of its functions).
  - `prompt.py`: `:2100` `_render_agent_prompt_label`, `:3358` `pop_steer`, `:3889` `format_streamlined_status_bar`, `:3947` `render_statusline`, `:4079` `_is_multiline_trigger`. The `render_statusline` here doesn't match the plugin hook's name-based lookup: its signature differs.
  - `session_picker.py:516` `_format_badges_markup`, `:921` `prompt_plan_implementation`.
  - `visualize/_approval_panel.py:655` `expand_plan_in_pager`.
  - `visualize/_blocks.py:1339` `make_plan_progress_bar`, `:1519` `StatusSpinner`, `:1603` `MultiStepProgress`, `:1779` `_find_committed_boundary_parser`.
  - `visualize/_btw_panel.py:155` `compose_for_live`.
  - `ui/theme.py:227` `get_task_browser_style` along with `_task_browser_style_dark` and `_task_browser_style_light`.

## (B) TEST-ONLY production code
- **Runtime cluster, test-only pieces:** `KimiSoul`, `Context`, `run_soul`, `BackToTheFuture`, `soul.agent.Agent`/`Runtime`, `visualize()`, `_live_view`, `_interactive`, `ui/print/visualize.py`.
- **Modules:** `background/worker.py:41` `run_background_task_worker`; `wire/serde.py`.
- **acp:** `acp/server.py` methods are protocol false positives, not test-only.
- **auth, events, jev, session:**
  - `auth/oauth.py:506` `get_cached_access_token`.
  - `events.py:120` `is_log_only`, `:304`/`:305`/`:307` `make_*_event` aliases, `:468` `derive_messages_from_events`.
  - `jev/client.py:180` `_reset_executor_for_tests`, `:421` `jev_cache_clear`, `:516` `jev_status_clear`.
  - `session.py:71` `subagents_dir`.
- **soul, subagents, teams, telemetry:**
  - `soul/agent.py:214` `checkpoint_count`.
  - `soul/session/store.py:216` `replay_events`, `:220` `validate_and_repair_invariants`.
  - `subagents/runner.py:89` `cancel_all`, `:397` `run_parallel_subagents`.
  - `teams/deadlock.py:90` `InterAgentWaitWatchdog` and `record_wait`.
  - `teams/manager.py:578` `reset_team_manager` along with `cancel_all_teammates`.
  - `telemetry/sink.py:43` `add_event`, `:113` `set_active_trace_id`, `:176` `get_metrics_summary`, `:193` `export_spans` (and `to_otel_span`).
- **tools, triage:**
  - `tools/file/replace.py:1424` `Edit` alias.
  - `tools/legacy/path_lock.py:92` `acquire_read_lock`.
  - `tools/shell/__init__.py:54` `clear_session_working_dir`.
  - `triage/engine.py:336`/`:393`/`:611` `reset_*`.
- **ui:**
  - `ui/shell/mcp_status.py:17` `render_mcp_console`.
  - `prompt.py:3473` `format_status_bar`, `:3487` `render_status_bar`.
  - `startup.py:12` `ShellStartupProgress`.
  - `_blocks.py:689` `summarize_thinking`, `:1325` `parse_plan_stats`, `:1462` `render_plan_preview`.
- **utils, web:**
  - `utils/common/file_history.py:281` `list_checkpoints` (`scripts/self_check.py` only).
  - `utils/common/invariants.py:166` `assert_session_invariants`. The `verify_*` helpers are partly live via `store.py:223`.
  - `model_capabilities.py:125` `get_default_reasoning_effort`.
  - `openai_thinking.py:47` `get_thinking_token_budget`.
  - `utils/path.py:142` `with_file_lock`.
  - `rich/diff_render.py:44` `format_diff_text`.
  - `web_providers.py:603` `register_web_search_provider`, `:607` `list_web_search_providers`.

## (C) Orphan and test-only modules

| Module | Contents | History | Recommendation |
|---|---|---|---|
| `coderai/__main__.py` | 6-line `python -m coderai` entry | `8e688ef` | Keep (runpy entry point, false positive) |
| `cli/__main__.py` | `python -m coderai.cli`, which installs crash handlers | `e4c9d50` (ACP wiring) | Keep (runpy/ACP entry point) |
| `background/ids.py` | `generate_task_id` | only `0b3fc0f` | Delete |
| `background/worker.py` | 179-line background worker, test-only | `0b3fc0f`→`5cad9aa` | Delete along with `tests/test_background_wire.py:18,64–79`, or wire into the job runner |
| `tools/test.py` | `Plus`/`Compare`/`Panic` toy tools; no agent YAML references them | `0b3fc0f` | Delete (Kimi test fixture leftover) |
| `ui/shell/export_import.py` | 5-line re-export of `utils.export` | `0b3fc0f`… | Delete |
| `utils/environment.py` | 158 lines, a verbatim copy of `subprocess_env.py:241–387` | `0b3fc0f` | Delete |
| `wire/serde.py` | 11-line re-export, test-only | `0b3fc0f` | Delete and point the test at `wire.types` |
| `coderai/app.py` | See the runtime cluster above | Deleted in `5e06ff6`, now untracked | Delete |
| `ui/print/visualize.py` | Print-mode renderer, test-only | `60dad36` | Delete or wire into `run_exec_session` |

`utils/pyinstaller.py` (used by `coderai.spec`) and `skills/*/scripts/*.py` (run as standalone scripts) are false positives.

## (D) Duplicated function bodies
There are 25 groups (AST-identical, more than 5 lines), about 337 redundant lines.

- **`utils/shell_quoting.py` (209 lines)** is `subprocess_env.py:1–209` verbatim; the only difference is that `subprocess_env` adds `get_clean_env`. That accounts for 10 of the groups: `find_git_bash_path`, `resolve_shell_path`, `get_shell_kind`, `build_shell_init_command`, `build_disable_extglob_command`, `windows_path_to_posix_path`, `posix_path_to_windows_path`, `is_sensitive_env_var`, `scrub_subprocess_env`, `build_shell_env`. Live importers are `prompt/__init__.py:40`, `tools/shell/__init__.py:29` and `tests/test_subagents.py:24`.
- **`utils/environment.py`** is `subprocess_env.py:241–387` verbatim: `kill_process_tree` (50 lines), `escalated_kill_process_tree` (31), `scrubbed_parent_env`, `is_process_alive`, `clamp_bash_timeout_ms`.
- **Other groups:**
  - `tools/file/read.py:823` = `read_media.py:210` (`_validate_path`).
  - `skill/flow/mermaid.py:255` = `d2.py:467` (`_infer_decision_nodes`).
  - `skill/flow/runner.py:51` and `:188` = `soul/coderaisoul.py:2564` and `:2723` (`FlowRunner.__init__` and `_match_flow_edge`).
  - `ui/shell/__init__.py:224` = `coderaisoul.py:1677` (`_index_slash_commands`).
  - `tools/file/write.py:291` = `replace.py:1433` (`__init__`).
  - `tools/agent/__init__.py:42` `_parse_opt_int` = `:466` `_to_int`.
  - `ui/shell/dispatch.py:430` `cmd_tokens` = `:1220` `cmd_usage`.
  - `tools/legacy/executor.py:391` = `:452` (`get_hook`).
  - `ui/print/visualize.py:128` = `:151` (`feed`).

## (E) Unused config keys and constants
**Bug:** `config.py:204` reads `typed.mcp.tool_call_timeout_ms`, but the field is at `typed.mcp.client`. The resulting `AttributeError` is swallowed at `:206`, so `_typed_global_knobs()` always returns `{}`. I confirmed this with `python -c`. As a result, `merge_all_available_skills`, `extra_skill_dirs`, `notifications.claim_stale_after_ms` and `mcp.client.tool_call_timeout_ms` from `config.toml` are all silently ignored. The bug is already present at HEAD.

**Never read:**
- `BackgroundConfig` (`config.py:1291–1304`): all 11 fields.
- `TypedConfig.default_yolo` `:1338` and `default_plan_mode` `:1340`.
- `OAuthRef.storage` `:1202`: the `"keyring"` option is never honoured.
- The `maxRetriesPerStep` translation `:186`.
- `typedConfigDefaultLocation` `:608`.

**Read only by the dead runtime:**
- `default_editor` (read in `Shell`).
- `skip_afk_prompt_injection` (read in `KimiSoul`).
- `services.moonshot_search` / `services.moonshot_fetch` (read in `from_typed_config`).

**Dead aliases:** `NotificationConfig` `:1313`, `McpConfig` `:1328`, `save_config` `:1557`, `PermissionScope` `:25`, `PROVIDER_REGISTRY` `:947`.

**Hook points never fired:** `HookPoint.PRE_STEP`, `POST_STEP`, `PRE_PROMPT`, `POST_PROMPT` and `STOP_CRITERIA` (`hooks/events.py:30–37`).

**Constants referenced only at their definition (21):**
- `orchestration.py:34` `SUBTASK_STOP_REASONS`, `agentspec.py:30` `OKABE_AGENT_FILE`, `sandbox.py:60` and `:61` `READ_SCOPES`/`WRITE_SCOPES`.
- `tools/file/read.py:30` `STREAM_MIN_SIZE`, `read_media.py:14` `ALLOWED_EXTS`, `web/fetch.py:57` `BLOCK_TAGS`.
- `wire/jsonrpc.py:150` `JSONRPC_OUT_METHODS`, `terminal/manager.py:20` `DEFAULT_MAX_BUFFER_CHARS`.
- `model_capabilities.py:23` `DEEPSEEK_V4_MODELS` and `:58` `ALL_REASONING_EFFORTS`, `llm_retry.py:15` `RETRYABLE_CODES`, `triage/engine.py:39` `JEV_MODEL_ID`.
- `cli/elapsed.py:117` `NOTIFICATION_SEVERITY_STYLE`, `prompts/__init__.py:6` `COMPACT`, `acp/types.py:11` `PROTOCOL_VERSION`.
- `ui/shell/prompt.py:90` and `:91` `PROMPT_SYMBOL_THINKING`/`PROMPT_SYMBOL_PLAN`, `console.py:50` `_NEUTRAL_MARKDOWN_THEME`, `_btw_panel.py:29` `_BTW_SHORT_ANSWER_LINES`.

**Other scans:**
- No `if False:` or `if 0:` blocks.
- No commented-out code blocks (AST-parse scan).
- Three empty `if TYPE_CHECKING: pass` stubs: `soul/slash.py:15`, `utils/clipboard.py:25`, `app.py:35`.
- Shims: the Kimi aliases `KimiToolset` (`soul/toolset.py:1020`), `CoderAISoul = KimiSoul` (`coderaisoul.py:2745`), and `App`/`KimiCLI` (`app.py:285`) all belong to the dead runtime. `tools/legacy/` is the live tool platform, not a shim.

## (F) TODO / FIXME
There are only 7 markers, and 5 of them are in resurrected dead code:
- `ui/shell/replay.py:165` FIXME (non-text tool results)
- `ui/shell/visualize/_live_view.py:792` and `:934`
- `ui/shell/__init__.py:762`
- `soul/coderaisoul.py:1021`

The two live ones are `acp/convert.py:163` and `ui/shell/prompt.py:1493` (`TODO(kaos)`: an async file-mention completer).

## (G) Test collection and stale doc references
- **Collection:** all 47 files (35 in `tests/`, 12 in `tests_e2e/`) collect cleanly, one process per file. An AST scan of all `from coderai… import` statements (including those inside functions), `import_module` strings and string `monkeypatch.setattr` targets found no missing symbols. The only two hits were deliberate `pytest.raises(ImportError)` checks at `test_ui_review_remediation.py:33` and `:36`.
- **The documented test command fails in this environment:** `--benchmark-disable` gives "unrecognized arguments" because `pytest-benchmark` (declared at `pyproject.toml:58`) isn't installed.
- **New tests that only anchor the dead runtime:**
  - `test_coderaisoul_lifecycle` (`KimiSoul`, `Runtime`, `Agent`, `Context`, `run_soul`)
  - `test_context_lifecycle` (`Context`)
  - `test_cancellation_resilience` (`run_soul`, `Context`)
  - `test_visualize_wire_loop` (`visualize`, `_live_view`, `_interactive`)
  - `test_runtime_parity:104` (`SessionSoul.checkpoint_count`, test-only)
- **The other new tests exercise live code:** `atomic_file_ops`, `hierarchical_config`, `jev_*`, `review_*`, `triage_engine`.
- **Deleted examples:** nothing references `custom-tools` or `coderai-cli-wire-messages` any more, and `examples/README.md` is already updated.
- **Stale doc path:** `clips/clip-004-shell-ui-flicker-mitigation.md:62` cites `coderai/visualize/_live_view.py`, which doesn't exist.
- **Artifacts:** `build/` (8.3 MB) and `coderai_agent.egg-info/` are git-ignored (`.gitignore:9` and `:6`) and have 0 tracked files. They aren't stale relative to the source, but `build/lib` includes the untracked `app.py`.

## (H) Vulture false positives by category
Out of 456 hits in `coderai/` (excluding the `_` keybinding functions, counted separately):

| Category | Count |
|---|---|
| Decorator-registered slash commands (`dispatch.py`, plus 4 in `soul/slash.py`, which are transitively dead) | 52 |
| prompt_toolkit keybindings (`_` functions in `prompt.py`, plus 4 named ones) | ~37 |
| ACP JSON-RPC methods (`acp/server.py`, dispatched by `acp.run_agent`) | 9 |
| Pydantic validators and serializers | 9 |
| Stdlib and third-party overrides (HTMLParser, `UIControl`, rich `Pager`) | 8 |
| `system.md` template variables (`soul/agent.py` `CODERAI_*`) | 7 |
| Duck-typed StreamWriter (`acp/kaos.py` `_NullWritable`) | 6 |
| PEP 562 module `__getattr__` | 4 |
| PyInstaller spec kwargs | 3 |
| Variables and attributes, of 143 total (dataclass and pydantic fields about 45, enum members about 13, aliases about 12) | ~70 false positives |

The rest of the 143 variables are real: dead constants (listed in E), write-only instance attributes (`acp/kaos.py:72`, `:188` and `:189`; `mcp/client.py` `_prompts`/`_resources`/`server_capabilities`; `wire/server.py` `_client_supports_plan_mode`), and unused locals and parameters (`subagents/runner.py:549` `continuable`, `ui/shell/app.py:1502` and `:1781`, `prompt.py:617` and `:618`).

Separately, about 190 extra dead callables surfaced when I applied transitive, alias-aware liveness. Most of them hide behind `__all__` re-exports; they are folded into section A.

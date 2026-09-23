<!-- Source: review agent 2f4df638-1ff2-483a-b9b3-02f5c8e25d3a · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# Code review: CoderAI agent loop, sessions and LLM layer

This was read-only; no files were changed. Every finding below was checked by reading the code, and some by introspection with `python3 -c`.

The worst problems are:
- **Compaction loop:** once a session crosses the auto-compaction threshold, compaction can run again on every step.
- **Cross-session approval leak:** one session's YOLO/AFK setting auto-approves tool calls for every other session in the process.
- **Empty summaries:** the compaction request can come back with no summary and still hide most of the history.
- **Half-finished refactor:** the uncommitted diff puts a `KimiSoul` runtime back into `coderaisoul.py` (about 1,750 lines), and it can't run as written.

---

## (A) Bugs / bad implementations

**A1. Auto-compaction can fire on every step once over the threshold — High**
- **Where:** `coderai/soul/coderaisoul.py:490`, `coderai/soul/compaction.py:210-222`, `coderai/soul/session/models.py:186-199`
- **Problem:** `token_count = max(activeTokens, estimate_text_tokens(messages))` counts over the raw `list_session_messages()`. That list still includes rows already hidden by earlier compactions, so the estimate never goes down.
  - `_find_safe_region` also picks the next region from that raw list.
  - Each compaction-summary row gets a new `uuid4()` id every time it is read, so summaries can never be hidden themselves. They pile up.
- **Impact:** after the first compaction, it likely triggers again every step: an extra LLM call each time, and history that keeps growing.
- **Fix:** estimate over `derive_messages(messages)` (or the converted request). Choose regions on the derived list. Give summary rows a stable id (for example the compactionId or event seq).

**A2. YOLO/AFK leaks across sessions through a global registry — High**
- **Where:** `coderai/soul/approval.py:1101-1107`, `coderai/soul/session/manager.py:243`, `coderai/soul/session/approval.py:9-23`
- **Problem:** `Approval.request()` approves automatically if `global_auto_approve_check()` is true, meaning if *any* `SessionManager` ever created in the process has YOLO or AFK on.
  - Managers are added to `_session_managers` but never removed. `unregister_session_manager` has no callers, and `dispose()` doesn't remove them either.
- **Impact:** a subagent's manager, or one that has already been disposed, can auto-approve dangerous tools everywhere else.
- **Fix:** delete the global check and use the per-session `check_auto_approve_for_session`. Unregister in `dispose` / `close_session_manager`.

**A3. The compaction request offers tools, and an empty summary still hides history — High**
- **Where:** `coderai/soul/compaction.py:343-359`, `408-419`
- **Problem:** the summary request includes `tools` and doesn't set `tool_choice`, so the model may answer with tool calls and no text.
  - `summary` then comes out as `""`, but a `compaction/summary` event is still written, hiding about the older two-thirds of the log.
  - The call also goes through `_create_completion` with no retry.
- **Fix:** send `tool_choice="none"`, or leave tools out. Abort if the summary is empty. Use `_create_completion_with_retry`.

**A4. `AgentLoop.run` swallows `CancelledError` — Medium-High**
- **Where:** `coderai/soul/coderaisoul.py:845-861`; also `coderai/soul/session/manager.py:2036-2037`
- **Problem:** the `except asyncio.CancelledError:` block records the interruption but never re-raises. A cancelled task therefore finishes normally, which breaks `asyncio.timeout` / `wait_for` / `TaskGroup` behaviour.
  - Separately, `_create_completion_with_retry` raises a hand-made `CancelledError()` to signal a user interrupt.
- **Fix:** add `raise` after the bookkeeping. Use a dedicated `SessionInterrupted` exception for user interrupts.

**A5. Unexpected exceptions leave the session stuck in "processing" — Medium**
- **Where:** `coderai/soul/coderaisoul.py:415-864`, `coderai/soul/session/manager.py:2136-2155`
- **Problem:** the loop only catches `CancelledError`. An error from `_compact_session`, `_append_tool_messages` or similar exits through `finally`:
  - status stays `processing`;
  - no `turn_end` event is emitted;
  - `compaction_begin()` has no matching `compaction_end()` because there is no try/finally around it.
- **Fix:** add an `except Exception` that marks the session failed and emits `turn_end("error")`. Wrap the compaction wire events in try/finally.

**A6. Early returns and a stale interrupt signal can silently drop the next prompt — Medium**
- **Where:** `coderai/soul/coderaisoul.py:200-221`, `375-383`, `417-418`, `455-456`; `coderai/soul/session/manager.py:828-833`
- **Problems:**
  - `interrupt_session` installs an already-set Event when no activation owns one.
  - `_claim_controller` reuses any existing Event, so the next turn hits `is_interrupted` at the top of the loop and returns. By then status was just overwritten to `processing`, and there is no `turn_end`. An interrupt that arrives just after a turn ends therefore kills the user's next prompt.
  - Reusing the Event also lets a second concurrent activation pop the first one's Event when it releases.
  - The no-client path (lines 385-413) emits `turn_start` with no matching `turn_end`.
- **Fix:** on a fresh user turn, replace an Event that is already set. Put `turn_end` and the final status in a `finally` block.

**A7. The output-token cap right after compaction uses the pre-compaction count — Medium**
- **Where:** `coderai/soul/coderaisoul.py:490`, `531-532`, `616-621`
- **Problem:** `token_count` isn't recomputed after `_compact_session`, yet it is passed to `apply_request_completion_cap`. Right after an "overflow"-triggered compaction, the cap can be tiny (at most about 5% of the context window). Together with A1, this can repeat every step.
- **Fix:** recompute the token estimate after compaction.

**A8. LLM streaming runs in a thread that can't be cancelled — Medium**
- **Where:** `coderai/soul/session/manager.py:2091-2103`, `coderai/soul/session/completion.py:107-160`
- **Problem:** the synchronous stream runs under `asyncio.to_thread`.
  - An interrupt isn't noticed until the whole stream finishes.
  - After cancellation the thread keeps streaming (and costing tokens) and keeps calling `on_chunk` into the UI.
- **Fix:** check a cancel flag on each chunk and call `resp.close()`, or move to `AsyncOpenAI`.

**A9. The fallback-model cascade builds inconsistent requests — Medium**
- **Where:** `coderai/soul/session/manager.py:2009-2053`
- **Problems:**
  - It uses `settings["reasoningEffort"]`, ignoring the session override and the adaptive effort logic.
  - It keeps the primary model's provider-specific keys (`extra_body`, and `max_tokens` vs `max_completion_tokens` chosen by name) for a different model family.
  - It reuses the same client and base URL for every fallback model.
  - `CONTEXT_OVERFLOW` moves on to the next model instead of compacting.
- **Fix:** build each fallback's request from scratch with one shared builder function. Treat `CONTEXT_OVERFLOW` as a signal to compact and retry.

**A10. Plan mode stays on for tool execution after ExitPlanMode — Medium**
- **Where:** `coderai/soul/session/manager.py:1833-1843`, `1890-1899`
- **Problem:** the ExitPlanMode result only flips the index entry's `planMode`. `SessionState.plan_mode` stays true (it was set at line 1295). `_append_tool_messages` falls back to that stale state, so later tools still run with `plan_mode=True`.
- **Fix:** a single `set_plan_mode()` that updates the entry, the state, and the soul together. The dead `sync_session_state_from_entry` is exactly the missing call.

**A11. The compaction summary lands after newer messages, as a mid-conversation system message — Medium** (medium confidence on how providers react)
- **Where:** `coderai/soul/compaction.py:409-419`, `coderai/soul/session/models.py:186-199`, `coderai/utils/common/message_converter.py:139-151`
- **Problem:** the summary event is appended at the end of the log, and the converter keeps log order. The model therefore sees the recent third of the conversation *before* the summary of older context, and the summary uses role `system` in mid-conversation, which some providers reject or ignore.
- **Fix:** have `derive_messages` place the summary where the first hidden message was, as a marked `user` message.

**A12. The revived `KimiSoul` compaction is broken — High for that code path**
- **Where:** `coderai/soul/coderaisoul.py:2349-2355` vs `coderai/soul/compaction.py:584-612`
- **Problems:**
  - `compact_context` reads `compaction_result.trace_id`, `.messages`, `.estimated_token_count` and `.usage`.
  - `SimpleCompaction.compact()` returns a plain `list`, and the imported `CompactionResult` dataclass has none of those fields (confirmed by introspection). This raises `AttributeError`.
  - `clear()` followed by `append_message()` isn't shielded, so a cancellation in between loses the history.
- **Fix:** make `SimpleCompaction` return a proper result type, and do the clear-and-rewrite as one atomic step.

**A13. Soul slash commands don't work on `KimiSoul` — Medium**
- **Where:** `coderai/soul/slash.py:29`, `46-57`, `146-156`, `181-184`
- **Problems:**
  - `/plan on|off|toggle` assigns to `soul.plan_mode`, which is a read-only property (`fset is None`), so it raises `AttributeError`. The right method, `set_plan_mode_from_manual`, is unused.
  - `/compact` looks for `.compact` or `.manager`, finds neither, and prints "complete" without doing anything.
  - `/init` imports `load_agents_md`, which doesn't exist anywhere.
- **Fix:** call `set_plan_mode_from_manual` and `compact_context`, and remove or fix `/init`.

**A14. `KimiSoul` passes the wrong history type to injection providers — Medium**
- **Where:** `coderai/soul/coderaisoul.py:1141`, `coderai/soul/dynamic_injections/plan_mode.py:109-121`
- **Problem:** providers expect a list of dicts but receive kosong `Message` objects. `_turns_since_reminder` skips anything that isn't a dict and returns "not found", so the **full** plan-mode reminder is injected on every step.
- **Fix:** convert with the same code that `SessionSoul.history_for_injections` uses.

**A15. Rebuilt file state marks current disk contents as "seen" — Low-Medium** (medium confidence)
- **Where:** `coderai/state.py:335-372`
- **Problem:** after a restart, the rebuild reads the *current* file contents and records them as what the model was shown. That defeats the "detect external modification" check described in the module docstring. It also does blocking file I/O inside the async `run`.
- **Fix:** rebuild versions and snippets only, and mark the state as unverified.

**A16. Fire-and-forget tasks with no reference kept — Low**
- **Where:** `coderai/soul/coderaisoul.py:1834-1846`, `1914-1929`, `2408-2420`; `coderai/soul/session/manager.py:355`
- **Problem:** hook and AFK-notify tasks are created without holding a reference, so they can be garbage-collected mid-run, and their exceptions are silently dropped.
- **Fix:** keep them in a module-level set and discard each on completion.

**A17. Model-name heuristics misclassify models — Low**
- **Where:** `coderai/utils/common/model_capabilities.py:192`; `coderai/utils/common/openai_thinking.py:35-44`, `85`, `107`
- **Problems:**
  - `"mini" in m` matches every `gemini-*` model, so Gemini Pro counts as "fast" and drops to `low` reasoning effort after step 1.
  - `normalize_reasoning_effort` turns `adaptive`/`auto` into `max`, so setting "auto" through `set_reasoning_effort` never reaches the adaptive branch.
  - `"minimal"` maps to `"high"` for GPT, the opposite of what was asked.
  - `"sol" in m` treats models like `solar-*` as GPT.
- **Fix:** match on tokens or prefixes, keep `auto` as a real value, and map `minimal` to `low`.

**A18. `resolve_session_id` is slow and can pick the wrong session — Low**
- **Where:** `coderai/soul/session/manager.py:665-732`
- **Problem:** it is called by nearly every method. On a miss, it reads every session's log and git history, then falls back to a substring match that can return a *different* session. That session could then be deleted or interrupted.
- **Fix:** use exact ids internally and keep fuzzy matching at the CLI boundary only.

**A19. Checkpoint counting rereads the whole log every step, without a lock — Low**
- **Where:** `coderai/soul/session/manager.py:929-955`
- **Problem:** `context_checkpoint_count` reads the whole log on every step, which is quadratic over a session. It runs without a lock, so two callers can mint the same checkpoint id.
- **Fix:** keep an in-memory counter guarded by `_seq_lock`.

---

## (B) Inconsistencies / duplication

**B1. Two agent runtimes in one file — High (maintenance)**
- **Where:** `coderai/soul/coderaisoul.py:144` (`AgentLoop`) and `:999` (`KimiSoul`, aliased as `CoderAISoul` at `:2745`)
- **Problem:** steering, D-Mail rewind, compaction, retries (tenacity in one, `llm_retry` in the other), injections (appended one by one vs combined) and repeat detection (`RepeatToolReminder` vs toolset dedup) are each implemented twice, differently.
  - `KimiSoul` is only built in the untracked `coderai/app.py:146-171`, which nothing imports. That code creates a `Runtime` without approval or notifications, so `KimiSoul.__init__` raises `AttributeError`, and `except (ImportError, AttributeError)` quietly falls back to `SessionSoul(None, …)`.
  - The committed `tool_context.py` docstring says that engine was removed.
  - There is also a stray `def type_check(soul: KimiSoul)` inside the `AgentLoop` class (`:876`).
- **Fix:** pick one runtime. Either finish `KimiSoul` in its own module or drop it.

**B2. Four overlapping "session/state" modules — Medium**
- **Where:** `coderai/session.py`, `coderai/soul/session/manager.py`, `coderai/session_state.py`, `coderai/state.py`
- **Problem:**
  - `coderai/session.py`: `Session` stores `work_dir_meta.sessions_dir/<uuid-with-dashes>/context.jsonl` plus `wire.jsonl`.
  - `SessionManager`: stores `~/.coderai/projects/<code>/<hex>/` plus an index.
  - `session_state.py`: its docstring says `<project>/.coderai/sessions/<id>/state.json`, but the manager writes it under `project_dir/<id>`.
  - `state.py`: file-snippet state, with names (`SessionStateManager`, `clear_session_state`) that collide with the persisted-state concepts.
  - The id formats and storage roots differ, so sessions made by one system can't be seen by the other.
- **Fix:** one session store. Rename `state.py` to something like `file_snippets.py`.

**B3. The plan file path is computed three different ways — Medium**
- **Where:** `coderai/soul/agent.py:151`, `coderai/tools/plan/heroes.py:8,267`, `coderai/tools/plan/enter.py:257`
- **Problem:**
  - `SessionSoul` uses `<project>/.coderai/plans/<session_id>.md`.
  - `KimiSoul` / `heroes` use `~/.coderai/plans/<slug>.md`.
  - `EnterPlanMode` passes `project_root=`, which `heroes.get_plan_file_path` doesn't accept. The `TypeError` is swallowed and no path is ever shown.
  - The reminders therefore point the model at a file the tool layer doesn't use.
- **Fix:** one resolver function.

**B4. Compaction thresholds are read three ways — Medium**
- **Where:** `coderai/soul/coderaisoul.py:491-502`, `coderai/soul/compaction.py:476`, `coderai/soul/coderaisoul.py:1788`
- **Problem:**
  - `AgentLoop` uses `compactionTriggerRatio` (0.85), `reservedContextSize` and `autoCompactWindow`.
  - `BasicCompaction.compact_if_needed` uses 0.75/0.95 defaults, no reserved space, and only `activeTokens`.
  - `KimiSoul` uses `should_auto_compact` with `loop_control`.
  - `_compact_session` then falls back to `compact_now` anyway, so `compact_if_needed` makes no difference.
- **Fix:** one trigger function with one config source.

**B5. Token estimators and truncation limits disagree — Low**
- **Where:** `coderai/soul/compaction.py:124-143`, `coderai/llm.py:335-338`, `coderai/llm.py:225`
- **Problem:** there are three token estimators:
  - `compaction.estimate_text_tokens` (characters ÷ 4, text only, ignores tool-call arguments);
  - `llm._estimate_text_tokens` (weights non-ASCII characters);
  - `estimate_openai_request_tokens`.

  Tool-result truncation limits also differ by stage: 32k when written, `MAX_TOOL_RESULT_CHARS` in derive, 16k in the converter (`message_converter.py:220`), and 2,000 in the compaction pruner.
- **Fix:** one estimator and one truncation limit.

**B6. The retryable status-code lists differ — Low**
- **Where:** `coderai/soul/coderaisoul.py:927`, `2423-2434`, and `utils/common/llm_retry`
- **Problem:** the `KimiSoul` retry loop doesn't retry 408, 409 or 529 (overloaded), while telemetry and the `SessionManager` path do.
- **Fix:** one shared classifier.

**B7. AFK reminders are never re-armed or retracted — Low**
- **Where:** `coderai/soul/dynamic_injections/afk_mode.py:31`, `coderai/soul/agent.py:132-134`
- **Problem:** `AFK_DISABLED_REMINDER` is never injected, so the model is never told the user is back. `SessionSoul.notify_compacted` has no callers, so the one-shot AFK and plan reminders aren't re-armed after compaction hides them.

**B8. Session index-entry creation is copy-pasted three times — Low**
- **Where:** `coderai/soul/session/manager.py:1047-1068`, `1165-1186`, `877-902`
- **Problem:** the three copies differ: status `"pending"` in two of them and `"ready"` in the third, which is dead code.

---

## (C) Verified dead code

Each item below has zero references in `coderai/`, `sdks/`, `scripts/` and `tests/` apart from its own definition (checked with `rg -w`, which also catches string/`getattr` uses).

- **`coderai/llm.py`:** `estimate_message_tokens` (330), `is_always_thinking` (411), `supports_media` (416), `augment_provider_with_env_vars` (421), `clone_llm_with_model_alias` (662), `check_provider_connectivity` alias (1415).
- **`coderai/soul/coderaisoul.py`:** `AgentLoop.type_check` (876), `add_injection_provider` (1132), `set_plan_mode_from_manual` (1303). The last one is exactly what `/plan` should be calling (A13).
- **`coderai/soul/approval.py`:** `clear_session_tickets` (319), `Approval.set_runtime` (1057), `set_runtime_afk` (1074).
- **`coderai/soul/session/manager.py`:** `sync_session_state_from_entry` (438), `grant_permission_ticket` / `list_active_permission_tickets` / `revoke_permission_ticket` (457-494), `_create_empty_session` (877), `sync_mcp_servers` (2336). The re-exports `_check_auto_approve_for_session`, `_global_auto_approve_check`, `register_session_manager` and `unregister_session_manager` (101-110) are also unused. `_global_afk_check` and `_check_afk_for_session` *are* used, by `tools/ask_user`.
- **`coderai/soul/session/approval.py`:** `register_session_manager` and `unregister_session_manager`. These being unused is the root of A2.
- **`coderai/soul/session/models.py`:** `copy_message_with` (288). **`coderai/soul/session/store.py`:** `delete_log` (206).
- **`coderai/soul/toolset.py`:** `hide` / `unhide` (239/246), `dedup_triggered` (317), `register_external_tool` (596), `has_deferred_mcp_tools` (647), `_check_oauth_tokens` (729), `_mark_oauth_unauthorized` (742).
- **`coderai/soul/dynamic_injections/plan_mode.py`:** `_has_plan_reminder` (97).
- **`coderai/soul/agent.py`:** `Runtime.labor_market` (44), a field that is never set or read.
- **`coderai/session.py`:** `Session.list_all` (268), `continue_` (278). They are mentioned only in the docstring.
- **`coderai/state.py`:** `_set_file_version` (241). **`coderai/events.py`:** `make_tool_call_event` alias (306), `make_request_header` (313).
- **`coderai/utils/common/`:**
  - `message_converter.py`: `build_messages` (192), `get_trailing_pending_tool_calls` (196).
  - `model_capabilities.py`: `DEEPSEEK_V4_MODELS`, `ALL_REASONING_EFFORTS`, `format_capability_badges`, `format_model_effort_tags`.
  - `openai_thinking.py`: `ReasoningEffortLevel`, `resolve_reasoning_key`.

**Vulture false positives to ignore:**
- the `BuiltinSystemPromptArgs.CODERAI_*` fields (read through `dataclasses.asdict` into the Jinja template);
- the `soul/slash.py` commands (registered by a decorator);
- `dynamic_injection.__getattr__` (a module-level `__getattr__` that lazy-loads names);
- `_validate_todos` (a pydantic validator);
- `wire_mtime` (a persisted model field);
- `ClientTransport` (used as a string bound in a `TypeVar`).

**Ruff:** all 25 B023 (loop-variable closure) hits in `coderaisoul.py:478-805` are false positives. Each lambda is passed to `_update_entry`, which calls it immediately (`manager.py:741`). There are no B904 hits in this scope.

---

## (D) Notable design smells

- **D1. Swallowed exceptions — Medium.** `manager.py` has about 60 `except Exception: pass` blocks and `coderaisoul.py` has 21. Two concrete risks:
  - A failed dynamic injection may already have appended some messages, then gets skipped silently (`coderaisoul.py:583-584`).
  - A failure inside `maybe_run_ralph` is swallowed and the normal turn runs anyway, which can do the work twice (`manager.py:1355-1362`).
- **D2. Index writes without a lock — Low-Medium.** Session-index read-modify-write has no file lock (`manager.py:734-746`, `store.py:117`). Two CLI or daemon processes in one project will lose each other's updates.
- **D3. Mutable per-call state on a shared engine — Low.** `_pending_custom_instruction` is stored on the shared `BasicCompaction` instance (`compaction.py:452-460`), so concurrent compactions of different sessions can race.
- **D4. Ad-hoc attributes and tangled imports — Low.**
  - Steer queues are created lazily with `hasattr` (`manager.py:919-927`) instead of in `__init__`.
  - Mid-module `# noqa: E402` imports and private `_underscore` re-exports mean tools depend on `SessionManager` internals (`ask_user` imports `_global_afk_check` from `manager`).
- **D5. Redundant multimodal check — Low.** The final line of `supports_multimodal` repeats a check already made above it, so any unknown model defaults to multimodal and images get sent to text-only models (`model_capabilities.py:184`).

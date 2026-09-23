<!-- Source: review agent bd150703-2eef-4bf0-9e79-104419cbcfe9 · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# CoderAI tool layer: read-only review

I found four High-severity problems. The executor can run a tool twice, spawning a subagent never asks for approval, the persistent bash shell always reports success, and the output sanitizer silently changes file contents returned by `read`.

The live system is the legacy registry and executor. The newer kosong tool classes can't be loaded, and much of the uncommitted work is going into them.

No repo files were changed. Each finding was checked by reading the code with `rg`/`git diff`, and many were confirmed by running small Python or bash snippets in isolation (marked "Reproduced"). Line numbers are for the current working tree.

---

## (A) Bugs and bad implementations (including security)

**A1. High: a tool can run twice, the second time blocking the event loop.** `coderai/tools/legacy/executor.py:546-556`
- **Problem:** `_invoke` runs a sync handler with `asyncio.to_thread(...)`, and on `except TypeError` runs it again directly on the event loop. Any `TypeError` raised inside the handler therefore triggers a second run. Reproduced: the probe handler ran 2 times.
- **Impact:** Side effects happen twice (bash commands, writes, subagent spawns), and the retry blocks the event loop.
- **Fix:** Remove the fallback. If you need to detect a signature mismatch, check the signature once at registration (`inspect.signature`), not by catching `TypeError` at runtime.

**A2. High: every subagent spawn skips approval, and the child runs without permission checks.** `coderai/soul/approval.py:619-621,631-632`, `coderai/tools/agent/__init__.py:158,236,440`, `coderai/subagents/runner.py:958,1042`
- **Problem:** The approval code reads `args.get("mode")`, which defaults to `"read_only"` and gives `scopes=[]`. The Task, subagent and subagent_fork schemas don't have a `mode` parameter, so the model never sends one. The child then runs `execute_tool_calls` with no permission checks. In read_only mode it blocks only `write` and `edit`; `bash`, `pwsh`, `str_replace_editor`, `terminal_send` and `schedule_create` still run. `allowed_tools` filters the list offered to the model, not what can execute.
- **Impact:** The model can get a shell or file write with no approval by delegating to a subagent, even outside YOLO or AFK mode.
- **Fix:** Enforce read_only in the child executor with a denylist covering the shell, terminal, str_replace_editor and schedule tools. Also route child tool calls through the parent's permission policy, or give subagent spawns the most permissive scope the child can use.

**A3. High: the persistent bash shell never reads the exit code or working directory.** `coderai/tools/shell/__init__.py:333`
- **Problem:** The f-string contains `%%d:%%s`. f-strings don't unescape `%%`, so `printf` prints a literal `%d:%s` and the end-marker regex never matches. Reproduced with bash.
- **Impact:** `exit_code` stays `None`, so failing commands return `ok=True`, and `cd` inside the persistent shell is never tracked.
- **Fix:** Use `%d:%s` in the f-string, and add a test that checks a non-zero exit code.

**A4. High (confidence high on mechanism, medium on frequency): the sanitizer rewrites file contents returned by `read`.** `coderai/tools/legacy/sanitizer.py:30,69`, applied at `executor.py:348,383,831`
- **Problem:** The `openai_api_key` pattern `sk-(?:proj-)?[A-Za-z0-9_-]{24,}` has no word boundary. Reproduced: `disk-usage-monitoring-service-config` becomes `di[REDACTED_OPENAI_KEY]`. The `database_uri_password` pattern uses a greedy `(.*)` that consumes up to the last `@` on the line, such as an email address later on.
- **Impact:** The model sees altered file text. `edit` then fails to match, and a `write` built from that text puts the redaction placeholders into the source file.
- **Fix:** Add `\b` or a negative lookbehind for letters and digits before `sk-`, and make the password capture non-greedy and exclude `@`. Consider not sanitizing `read` output, or only redacting high-confidence patterns there.

**A5. Medium: the model's bash `timeout_ms` is validated and then ignored.** `coderai/tools/shell/__init__.py:465-474` vs `:610-624`
- **Problem:** `_execute_shell_command` takes its timeout only from `context.bash_timeout_ms` or the default of 10 minutes. `clamp_bash_timeout_ms` has a 60-second minimum and no maximum.
- **Impact:** A short timeout from the model does nothing, and hung commands hold a turn for up to 10 minutes.
- **Fix:** Pass the model's `timeout_ms` into `initial_timeout_ms`, clamped between the minimum and a hard maximum.

**A6. Medium: the redirect check blocks valid commands but doesn't stop real escapes.** `coderai/tools/shell/__init__.py:127-161`, called at `:494,1069`
- **Problem:** `resolve_exec_cwd` runs before the sandbox mode is checked. Reproduced: in `danger-full-access` mode, `echo hi > /tmp/x.txt` and `>/dev/stderr` are refused. `echo "a > /etc/b"` is also refused, even though the redirect is inside a string. Meanwhile `cp x /etc/passwd` and `tee /etc/foo` pass.
- **Impact:** Users get false denials, and the check gives a false sense of security.
- **Fix:** Skip the check when the mode is `danger-full-access`, and leave containment to Seatbelt/bwrap. Otherwise, parse redirects with `shlex`/bashlex instead of a regex.

**A7. Medium: foreground bash output is buffered with no limit.** `coderai/tools/shell/__init__.py:746-788`
- **Problem:** `reader` appends every chunk to `stdout_chunks` with no size cap. Background jobs do have a cap (`_append_chunk` with `MAX_CAPTURE_CHARS`).
- **Impact:** A command like `yes`, or a verbose build, can use up memory before the timeout fires.
- **Fix:** Use a ring buffer or head-and-tail buffer capped at `MAX_CAPTURE_CHARS`, as the background path does.

**A8. Medium: a background `cd` outside the root breaks every later foreground command.** `coderai/tools/shell/__init__.py:945`
- **Problem:** `bg_worker` calls `_update_session_cwd` without the `resolve_exec_cwd` check that the foreground path runs (`:392,546`).
- **Impact:** After a background `cd /tmp && …`, all later foreground bash calls fail with "CWD rejected" for the rest of the session.
- **Fix:** Apply the same check before updating the session's working directory, and ignore invalid values.

**A9. Medium: `pwsh` skips the protections that bash has.** `coderai/tools/shell/__init__.py:1039-1201`
- **Problem:** It has no `build_shell_env`, so API keys leak into the child environment. It ignores the session and isolated working directories. It has a hardcoded 120-second timeout that kills only the direct child. Background mode leaks the log file handle (`:1104`), doesn't use `start_new_session`, doesn't handle the job-cap `RuntimeError`, and never marks the job complete.
- **Impact:** Secrets are exposed, grandchild processes are orphaned, and the job-cap problem in A10 builds up.
- **Fix:** Share bash's launcher: build the environment the same way, resolve the working directory, create a process group, and add a completion callback.

**A10. Medium: `pwsh` and `terminal_send` background jobs never finish.** `coderai/tools/legacy/terminal.py:118-140`, pwsh background path
- **Problem:** `store.start(...)` is called but nothing ever calls `store.complete(...)`. `rg "complete("` finds calls only in bash and the agent tool.
- **Impact:** The jobs show as running forever and count against `resolve_max_running_jobs()`. Eventually every background bash call and subagent in the session fails with "background job cap reached".
- **Fix:** Add a watcher that completes the job when the process or PTY exits, or don't register these as jobs.

**A11. Medium: `edit` defeats its own "read before edit" and "modified since read" checks.** `coderai/tools/file/replace.py:146-170` vs `:194-202`
- **Problem:** On the path-based route, the tool reads the file itself and calls `mark_file_read` and `record_observation` just before the checks run.
- **Impact:** Edits based on an old view of the file are never caught, so the model can overwrite changes made by the user.
- **Fix:** Don't record an observation inside `edit`. Compare against the last observation from `read`.

**A12. Medium: `.py` edits are refused if the result still has a syntax error.** `coderai/tools/file/replace.py:331-341`
- **Problem:** `ast.parse(updated_content)` rejects the edit even when the original file already failed to parse.
- **Impact:** A file with two syntax errors can't be fixed one step at a time, and neither can a file mid-refactor.
- **Fix:** Only reject when the original parsed and the new content doesn't.

**A13. Medium: `edit` and `str_replace_editor` corrupt files that aren't UTF-8.** `coderai/utils/path.py` (`read_text_file_with_metadata`, `detect_encoding`, `write_text_file`)
- **Problem:** Files are decoded as UTF-8 with `errors="replace"` and written back. Only UTF-16LE is detected. `write_text_file` converts the whole file to CRLF if any CRLF is present.
- **Impact:** Latin-1 and UTF-16BE files are silently corrupted, and files with mixed line endings are rewritten.
- **Fix:** Refuse to edit when decoding produces replacement characters or the file looks binary, and preserve line endings per line.

**A14. Medium: `schedule_create`'s schema advertises a parameter the handler ignores.** `coderai/tools/legacy/registry.py:1246` vs `coderai/tools/legacy/schedule.py:14-17`, `coderai/schedule.py:73-78`
- **Problem:** The schema has `cron_expression`, which is never read. The parameters the manager requires, `at` and `every_seconds`, aren't in the schema.
- **Impact:** Recurring schedules can't be created through the advertised schema; they always fail with "Exactly one of …".
- **Fix:** Put `at` and `every_seconds` in the schema, or implement `cron_expression`.

**A15. Medium: the `enter_plan_mode` plan-file workflow is broken in the live system.** `coderai/tools/plan/enter.py:252-258`, `coderai/tools/plan/heroes.py:267`
- **Problem:** The handler calls `get_plan_file_path(session_id, project_root=...)`, but the function takes only `session_id`. The `TypeError` is swallowed, so the plan-file hint is never shown. The plan file also lives under `~/.coderai/plans`, outside the workspace, so the default `workspace-write` sandbox blocks `write` from creating it.
- **Impact:** The model is told to "write plan to file", but it can't find the file or write to it.
- **Fix:** Correct the call, and either put plans inside the workspace or explicitly allow the plans directory in `check_file_write_access`.

**A16. Medium: `todo_write` with `merge=False` always fails.** `coderai/tools/todo/__init__.py:118`
- **Problem:** `"explanation": args.get("merge") and "todo_write"` evaluates to `False`, and the validator rejects it with "explanation must be a string". Reproduced. Merge behavior isn't implemented at all.
- **Fix:** Pass `args.get("explanation")` through, and either implement `merge` or remove it from the schema.

**A17. Medium: the Linux read-only sandbox allows network access.** `coderai/sandbox.py:352-378`
- **Problem:** The bwrap command has `--unshare-pid` but no `--unshare-net`. The macOS Seatbelt read-only profile denies network (`:245`).
- **Impact:** Read-only mode can send data out on Linux but not on macOS.
- **Fix:** Add `--unshare-net` in `read-only` mode.

**A18. Medium: the session tools can delete the live store's temp files.** `coderai/tools/session/query.py:138`, `coderai/soul/session/store.py:33`
- **Problem:** Without a session manager (subagent contexts, for example), `_open` creates a new `JsonlSessionStore`, whose constructor runs `cleanup_orphan_tmps()`.
- **Impact:** A supposedly read-only tool can delete `*.tmp-*` files while the live store is in the middle of an atomic write, which risks losing session data.
- **Fix:** Add a `cleanup=False` constructor option for readers.

**A19. Medium: `ApprovalRuntime` is disconnected, so its cancel and pending features do nothing.** `coderai/approval_runtime/runtime.py:67,93,184`
- **Problem:** `create_request` and `wait_for_response` are never called; `Approval.request` (`soul/approval.py:1092-1131`) goes straight over the wire.
- **Impact:** `list_pending()` is always empty. `cancel_by_source` (`coderaisoul.py:1605`) and the UI's pending-approval loop do nothing, and "approve for session" is never cached there.
- **Fix:** Route `Approval.request` through the runtime, or delete it.

**A20. Medium (confidence medium): `Think` inserts a message in the wrong place.** `coderai/tools/think/__init__.py:24-29`
- **Problem:** It calls `mgr._append_message(...)` to add an assistant message while tools are running, which lands between the assistant's tool-call message and the tool results. The `isThought` metadata is never read.
- **Impact:** Strict providers may reject the out-of-order history.
- **Fix:** Return the thought as the tool result only.

**A21. Medium (latent): importing `coderai.tools.dmail` on its own fails.** `coderai/tools/dmail/__init__.py:15,19`
- **Problem:** It imports from `coderai.soul.denwarenji` before defining `NAME`, and `coderaisoul.py:111` imports `NAME` from dmail. Reproduced: `python -c "import coderai.tools.dmail"` fails with a circular `ImportError`.
- **Impact:** It's hidden only because callers happen to import `coderaisoul` first.
- **Fix:** Define `NAME` before the soul import, or import `DenwaRenji` lazily.

**A22. Low: `ToolExecutor`'s timeout doesn't stop the thread.** `executor.py:580`
- **Problem:** `wait_for(to_thread(...))` stops waiting, but the thread keeps running and the path lock is released.
- **Impact:** A later call can write the same file at the same time as the still-running one.
- **Fix:** Have handlers support cancellation, or keep the lock until the thread finishes.

**A23. Low: smaller live-path issues.**
- **Web tools:** `int(args["max_length"])` in `web/fetch.py` and `int(args["max_results"])` in `web/search.py:51` are outside `try` blocks.
- **Custom search script:** it runs unsandboxed with the full environment (`web_providers.py:114`).
- **Bash profiles:** Seatbelt profile temp files are never deleted.
- **Undo history:** `_UNDO_HISTORY` in `replace.py` is global rather than per-session, and `_handle_undo` skips the sandbox and observation checks.
- **Hardcoded model:** a `"gpt-6-luna"` fallback appears at `replace.py:671,776` and `read_media.py:87`.
- **Lint:** Ruff B904 at `approval_runtime/runtime.py:110` and `sandbox.py:50`.

---

## (B) Inconsistencies and duplication

**Which tool system is live.**
- **Live (legacy registry and executor):** `SessionManager` (`soul/session/manager.py:66,177`) and `subagents/runner.py:584` use `ToolExecutor`, which uses `ToolRegistry._register_builtins()` with the `handle_*_tool(args, context)` functions. The exposed tools are:
  - Shell and jobs: bash, pwsh, job_list, job_output, job_kill
  - Files: glob, grep, read, write, edit, str_replace_editor
  - User and web: AskUserQuestion, WebSearch, WebFetch
  - Agents: Task, subagent, subagent_fork, send_message, interrupt_agent, list_agents, report, wait_agent, spawn_teammate
  - Terminals: terminal_open, terminal_send, terminal_read, terminal_signal, terminal_close, terminal_list
  - Planning and state: skill, UnderstandImage, UpdatePlan, todo_write, Think, SendDMail, exit_plan_mode, enter_plan_mode, goal
  - Schedules: schedule_create, schedule_list, schedule_delete
  - Team tasks: team_task_create, team_task_get, team_task_list, team_task_update
  - Session tools (new, uncommitted): session_search (alias session_query), session_trace, session_event_search, session_event_read
- **Dead (kosong `CoderAIToolset`):** `soul/agent.py:259-285` passes names like `"bash"` to `_load_tool`, and `tool_path.rsplit(":",1)` raises `ValueError` (reproduced). Its only caller, the untracked `coderai/app.py`, isn't imported anywhere. Every `CallableTool2` class is unreachable: WriteFile, StrReplaceFile, ReadFile, Glob, Grep, ReadMediaFile, Shell, Agent, AskUserQuestion, ExitPlanMode, EnterPlanMode, SendDMail, and Plus/Compare/Panic.

**B1. Medium: the live `enter_plan_mode` description names tools that don't exist.** `coderai/tools/plan/enter_description.md:12-35`
- **Problem:** It tells the model to use `EnterPlanMode`, `ExitPlanMode`, `ReadFile` and `Agent(subagent_type="explore")`. The live names are `enter_plan_mode`, `exit_plan_mode`, `read`, and `Task`/`subagent`.
- **Impact:** The model gets "unknown tool" errors.
- **Fix:** Use the registry names.

**B2. Medium: the uncommitted diff puts large new features into the dead system.**
- **Where:** `plan/enter.py`, `plan/__init__.py`, `ask_user/__init__.py`, `dmail/__init__.py`, `file/write.py` (async atomic write, `Params` alias), and `shell/__init__.py:1282-1346` (a `kaos.exec` fast path).
- **Why it's half-finished:** The shell fast path falls back to `handle_bash_tool` on any exception after launch, which runs the command again. `enter.py` also removed its comment warning about an import cycle and moved soul imports to the top level.
- **Fix:** Either wire up `CoderAIToolset` with real `module:Class` paths, or stop adding to it and delete it.

**B3. Medium: schemas and handlers disagree on parameters.**
- **`mode`:** Task, subagent and subagent_fork read `mode` but don't declare it (see A2).
- **WebFetch:** `raw`, `max_length` and `use_cache` are hidden.
- **Number types:** `read`'s `offset` and `limit` are typed "number", but the handler rejects floats. bash's `timeout_ms` is also typed "number".
- **Validation:** `validate_arguments` allows unknown keys and doesn't check array-item enums.
- **Fix:** Generate the schemas from the handlers' validators, or add a parity test.

**B4. Low: tool `.md` files describe the dead classes, not the live schemas.**
- **Problem:** `write.md` says "completely overwrite", but the dead `WriteFile` class has `path` and `mode` (overwrite/append), while the live tool uses `file_path`. `replace.md` describes `str_replace_editor` commands, but the dead `StrReplaceFile` takes `path`/`edit`. Registry descriptions are inline strings, so these files are loaded only by the dead classes, apart from `enter_description.md`.

**B5. Low: `extract_key_argument` uses the dead tool names.** `coderai/tools/__init__.py:40`
- **Problem:** It matches `Shell`, `ReadFile`, `WriteFile`, `StrReplaceFile`, `SearchWeb`, `FetchURL`, `TaskOutput`, `SetTodoList` and `Agent`, so every live tool hits the default case.
- **Impact:** Key arguments aren't shown for live tools.

**B6. Low: relative paths resolve inconsistently.**
- **Problem:** `read`, `grep` and `glob` resolve relative paths against `project_root`, while `write` and `edit` use `isolated_cwd`. The executor's path lock uses `project_root`.
- **Impact:** In worktree-isolated agents, reads and writes can refer to different files, and the lock may not match the file being written.

**B7. Low: duplicated utility modules.**
- **Shell quoting:** `utils/shell_quoting.py` is byte-for-byte the same as lines 1-209 of `utils/subprocess_env.py`.
- **Process tree and timeouts:** `utils/environment.py` repeats that code and isn't imported anywhere.
- **Environment scrubbing:** there are two implementations with different patterns, `scrub_subprocess_env` (live) and `scrubbed_parent_env` (unused).
- **Atomic writes:** there are two implementations. `utils/io.atomic_write_text` (new) uses `mkstemp`, so files become 0600, and only the dead system uses it. `utils/path.write_file_atomic` preserves file permissions.

**B8. Low: registry features that do nothing.**
- **Rate limiting:** the executor checks only `tool_def.rate_limit` (`executor.py:482`), but tools set only `rate_limited_id`.
- **Guards:** registry `guard()` guards are never checked; the executor reads only `hooks.guards`.
- **Plan mode:** `context.plan_mode` is filled in (`manager.py:1864`), but no tool reads it, so plan mode is enforced only by approval, which subagents skip.

---

## (C) Verified dead code

Each item was checked with `rg` for references outside its own definition.
- **Test fixtures:** `coderai/tools/test.py` holds kosong test tools Plus, Compare and Panic (Panic sleeps 2 seconds and raises). Nothing imports it or refers to it in yaml or PyInstaller files. It was added in commit `0b3fc0f`.
- **The whole kosong system:** every `CallableTool2` class listed in (B), `CoderAIToolset.load_tools`, and `coderai/app.py`. The Pydantic validators vulture flagged in those classes (`label_not_reserved`, `options_labels_unique`, `_validate_background_fields`) are dead only because their classes are.
- **Unused registry methods:** `set_session_mask`, `clear_session_mask`, `suppress_tool`, `is_tool_suppressed`, `get_tool`, `restrict`, `guard`, `on_change`.
- **Unused module:** `utils/environment.py`.
- **Unused names in `utils` and `sandbox.py`:**
  - `scrubbed_parent_env`, `windows_path_to_posix_path`, `WINDOWS_GIT_LOCATIONS`
  - `BASH_TIMEOUT_INCREMENT_MS`, `BASH_TIMEOUT_DECREMENT_MS`
  - `WriteFileAtomicOptions`, `shorten_home`, `find_project_root`
  - `is_within_directory` (used only by the dead system)
  - `sandbox.READ_SCOPES`, `WRITE_SCOPES`, `sandbox_available`
- **Unused names in the tool modules:**
  - `read.STREAM_MIN_SIZE`, `read._validate_line_offset`
  - `read_media.ALLOWED_EXTS`, `replace._find_line_numbers`
  - `tools/utils.n_chars`, `file/utils.async_write_file_with_locks`
  - `legacy/types.defer_context`, `legacy/types.conclude_turn`
  - `web/fetch.BLOCK_TAGS`
- **Vulture false positives:** the `handle_*` methods on the HTMLParser subclass in `web/fetch.py`; they're called by the parser.
- **Unreachable feature:** UTF-16 support in `read`, because `is_binary_buffer` flags UTF-16 files as binary first.

---

## (D) Design smells

1. **Two complete tool systems in parallel,** with new features going into the unusable one. This is the main source of the drift in (B). Pick one.
2. **Approval lives entirely upstream.** The executor always gets `permission_decision="allow"` (`manager.py:1857`), and subagents skip the upstream step altogether (A2). Permission checks belong in the executor.
3. **Untrusted shell config runs after the environment is scrubbed.** The shell sources `~/.bashrc`/`~/.zshrc`, which can re-export the secrets `build_shell_env` just removed.
4. **Sanitizing is global and lossy.** One regex list applies to every tool, including exact-content file reads (A4).
5. **Production code checks for test doubles.** `file/utils.check_file_write_access` looks for MagicMock strings.
6. **Spill references don't survive a restart.** `spill.py` stores output in a temp directory that `atexit` deletes (not further verified).
7. **`replace.py` is over 1,300 lines,** mixing edit, `str_replace_editor`, undo and an LLM fallback, with imports scattered through the file (around lines 845-853).
8. **`read` is inefficient on large files.** It reads the file twice (`read_bytes` and then a text read), has no size limit, and treats `.svg` files as images.

**Not reviewed in depth:** the glob, read_media and ask_user handlers, `legacy/terminal.py` beyond line 150, and `spill.py`.

Now I'm drafting the report structure with sections A through D, keeping each finding tight with file:line references, severity ratings, explanation and impact, plus a fix—aiming for roughly 30 findings, a brief live stack map, and noting verification status and confidence for each.

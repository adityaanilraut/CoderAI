# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
make dev          # or: pip install -e .

# Run
coderAI chat      # launches the Textual TUI (pure Python)
make run          # alias for coderAI chat

# Test
make test         # or: pytest
pytest tests/test_agent.py::TestClassName::test_method_name   # single test

# Lint & format
make lint         # ruff check (required; same as CI)
make typecheck    # mypy coderAI/ (required; CI gate — see "Typing ratchet")
make format       # ruff format coderAI/
make check        # Runs format, lint, typecheck, and test sequentially

# Setup & utilities
make setup        # interactive setup wizard
make quickstart   # complete setup for new developers
make clean        # remove build artifacts
make dist         # build Python distribution
```

## Typing ratchet

`mypy coderAI/` is a **CI gate** (runs after ruff, before pytest). The base
config in `pyproject.toml` is lenient, but a `[[tool.mypy.overrides]]` block
lists modules held to **strict** typing (`disallow_untyped_defs`,
`check_untyped_defs`, `warn_unused_ignores`). The strict set is
`system.*`, `llm.*`, `context.*`, `cli.*`, most of `core.*` (see
`pyproject.toml` for the exact list), and the TUI controller modules
(`tui.commands`, `tui.controller`, `tui.streaming`, `tui.serializers`,
`tui.state`, `tui.tool_metadata`).

**The strict-module list only grows — never remove a module from it.** When a
module becomes clean under strict checking, add it; once added it must stay
green. New code in strict modules must be fully annotated. The remaining
untyped surface (rest of `core/`, `tools/*`, `tui/*`) is migrated incrementally
in that order; do not regress an already-strict module to land a change
elsewhere.

`requests`/`yaml` are the only deps without bundled stubs — covered by
`types-requests`/`types-PyYAML` in the `dev` extra, so a clean run requires
`pip install -e ".[dev]"`.

## Architecture

CoderAI is a pure-Python AI coding agent CLI. The Click entry point in
`coderAI/cli.py` dispatches one-shot subcommands (config, history, models,
status, doctor, …) using Rich helpers in `coderAI/cli/utils.py`. `coderAI chat`
launches an in-process Textual TUI (`coderAI/tui/`) that drives the
agent loop and renders the streaming timeline.

The orchestration is split across modules under `coderAI/core/` and `coderAI/context/` so each concern is independently testable:

- `coderAI/core/agent.py` — `Agent` class: lifecycle, persona loading, provider wiring, session state, sub-agent spawning.
- `coderAI/core/agent_loop.py` — the per-turn execution loop (retry/backoff for transient LLM errors, JSON-arg coercion, iteration cap). Retry/error constants (`MAX_RETRIES_PER_ITERATION=3`, `MAX_CONSECUTIVE_ERRORS=5`, transient-error regex for 429/5xx) live in `coderAI/system/error_policy.py` and are imported from there.
- `coderAI/core/tool_executor.py` — `ToolExecutor`: confirmation UX for gated tools. Routes through `UIBridge` when the Textual UI is attached, otherwise prompts in the terminal.
- `coderAI/core/tool_routing.py` — dispatches a tool-call `function.name` to either `ToolRegistry` or an MCP server. MCP functions use the wire format `mcp__<server>__<tool>` (server must not contain `__`; tool may).
- `coderAI/context/context_controller.py` — token estimation, truncation, summarization. Reserves `RESPONSE_TOKEN_RESERVE=1024` and `TOOL_OVERHEAD_TOKENS=512` when budgeting.

Per-turn flow (`Agent.process_message()` → `agent_loop`):

1. User input → inject pinned context → context compression once estimated tokens exceed `context_window - RESPONSE_TOKEN_RESERVE - TOOL_OVERHEAD_TOKENS` (so summarization runs reactively when the window is genuinely full, not at an arbitrary 70% mark)
2. LLM call with retry logic (max 3 retries, exponential backoff for transient errors)
3. If tool calls returned → read-only tools run in parallel (`asyncio.gather`), mutating tools run sequentially
4. Tool results fed back to LLM → loop continues until final text response (max 50 iterations). Read-only tools run in parallel; `delegate_task` is domain-routed — read-only delegations (≤4 parallel), browser (≤3 parallel), desktop/workspace serial; other mutating tools run one at a time.
5. Session saved to `~/.coderAI/history/`

**Runtime shape of `coderAI chat`:**

1. `coderAI/cli.py` → `coderAI/cli/main.py` → `chat()` calls `coderAI.tui.run_chat_app(...)`.
2. The Textual app (`coderAI/tui/app.py::CoderAIApp`) creates an `Agent`
   and an `UIBridge`, passing the app's `on_event` callback so events
   land on the UI thread.
3. `BridgeStreamingHandler` forwards LLM token deltas as phased `turn` events.
4. `UIBridge` subscribes to `event_emitter` and dispatches slash commands
   queued by `coderAI/tui/slash.py`.

**Key components:**
- `coderAI/core/agent.py` — Core orchestrator (agentic loop, context management, sub-agent spawning, session lifecycle). Uses whatever `streaming_handler` the embedding process sets; defaults to `None` (non-streaming fallback).
- `coderAI/cli.py` / `coderAI/cli/` — Click CLI. `chat` launches the Textual TUI; other commands render with Rich.
- `coderAI/tui/` — Textual interactive chat (`app.py`, `listeners.py` event reducer, `timeline_render.py` timeline row writers, `diff_render.py`, `slash.py`, `state.py`, `session_setup.py`, `theme.py`). There is no `tui/lib/` shim — rendering lives beside the app.
- `coderAI/tui/controller.py` — In-process controller (`UIBridge`): subscribes to `event_emitter`, forwards events to the UI via `on_event`, and dispatches slash commands back into the agent. See [`CHAT_EVENTS.md`](CHAT_EVENTS.md).
- `coderAI/tui/streaming.py` — `BridgeStreamingHandler`: emits one phased `turn` event per assistant turn so the Textual timeline streams incrementally.
- `coderAI/tui/tool_metadata.py` — Tool category, risk level, and approval-preview helpers for the controller and modals.
- `coderAI/llm/` — LLM providers (openai, anthropic, groq, deepseek, gemini, lmstudio, ollama), all extending `base.LLMProvider`. Instantiation goes through `llm/factory.py::create_provider(model, config)` — do not construct providers directly from `agent.py`.
- `coderAI/tools/` — 92 tools extending `tools/base.Tool`. Registration is automatic via `tools/discovery.py::discover_tools()`, which walks the `coderAI.tools` package and instantiates every `Tool` subclass whose `__init__` takes no required args. Tools requiring constructor args (e.g. `ManageContextTool`, which needs the `Agent`) are registered manually in `Agent._create_tool_registry()`.
- `coderAI/system/safeguards.py` — reusable validators that run before dangerous actions: interactive-command detection (blocks REPLs invoked via non-interactive pipes), project-directory validation, git-scope guards (prevent operations leaking to a parent repo), staging blocklist for junk files (`.DS_Store`, `__pycache__`, `.coderAI/`, …).
- `coderAI/system/project_layout.py` — `find_dot_coderai_subdir()` resolves `.coderAI/<subdir>` across project root, cwd, and the package dir (for dev installs). Use this instead of hardcoding `.coderAI/` paths.
- `coderAI/tui/commands.py` — `UIBridge` command handlers, plus plain-text reference output for `/show <topic>` slash commands (`/show models`, `/show cost`, `/show status`, `/show config`, `/show info`, `/show tasks`).
- `coderAI/cli/utils.py` — Rich display helpers for one-shot CLI subcommands. Not used by the Textual chat UI.
- `coderAI/system/config.py` — Pydantic-based `ConfigManager` reading from `~/.coderAI/config.json` then env vars.
- `coderAI/core/agents.py` — `AgentPersona` loader for `.coderAI/agents/*.md` files with YAML frontmatter.
- `coderAI/core/agent_tracker.py` — Singleton `AgentTracker` for observability (status, tokens, cost, cancellation).
- `coderAI/context/context.py` — Pinned-file context manager with relevance filtering.
- `coderAI/system/cost.py` — Per-model token cost tracking; enforces `budget_limit` from config.
- `coderAI/system/history.py` — `Session` + `HistoryManager`; sessions in `~/.coderAI/history/`.
- `coderAI/tools/notepad.py` — Shared inter-agent notepad tool.
- `coderAI/system/error_policy.py` — Central home for retry/error constants and the transient-error regex; modules import from here instead of redefining.
- `coderAI/system/hooks_manager.py` — Loads `.coderAI/hooks.json` and runs pre/post-tool shell hooks around tool execution.
- `coderAI/system_prompt.py` — Builds the agent system prompt (loads static MDX prompt templates from `coderAI/prompts/`, formats dynamic tool docs, and appends project-level rules from `.coderAI/rules/*.md`).
- `coderAI/context/code_chunker.py` — Splits source files into semantic chunks (AST-aware for Python, regex for JS/TS, sliding window fallback).
- `coderAI/context/code_indexer.py` — ChromaDB-backed semantic code index with incremental updates via file-hash manifests.
- `coderAI/embeddings/` — Embedding provider abstraction (OpenAI `text-embedding-3-small` by default; no local provider yet).
- `.github/workflows/ci.yml` — On push/PR: matrix of (ubuntu-latest, macos-latest) × (Python 3.10, 3.12). Installs `pip install -e ".[dev]"`, then runs `ruff format --check coderAI/`, `ruff check coderAI/`, `mypy coderAI/`, `pytest -q --cov-fail-under=…`, and a `coderAI --version` smoke test. `make test` mirrors the pytest + smoke portion; `make check` runs the full sequence.
- `.github/workflows/release.yml` — On tagged releases (`v*`), builds the Python wheel + sdist with `python -m build`, attaches them to the GitHub Release, and publishes the wheel to PyPI via trusted publishing.

**Tool categories** (`coderAI/tools/`):
- `filesystem.py` — read_file, write_file, search_replace, apply_diff, list_directory, glob_search, **move_file, copy_file, delete_file, create_directory**, file_stat/chmod/chown/readlink
- `multi_edit.py` — multi_edit (batch search/replace in one file)
- `terminal.py` — run_command (safety blocklist), run_background, **list_processes, kill_process, read_bg_output**
- `git.py` — git_add, git_status, git_diff, git_commit, git_log, git_branch, git_checkout, git_stash, **git_push, git_pull, git_merge, git_rebase, git_revert, git_reset, git_show, git_remote, git_blame, git_cherry_pick, git_tag, git_fetch**
- `search.py` — text_search, grep, symbol_search
- `semantic_search.py` — semantic_search (natural-language code search via embeddings)
- `web.py` — web_search, read_url, download_file, http_request, **wikipedia_search, read_feed, sitemap_discover**
- `browser.py` — browser_navigate … browser_close (requires Playwright extra)
- `desktop.py` — run_applescript, get_accessibility_tree, click_ui_element, type_keystrokes (macOS only)
- `memory.py` — save_memory, recall_memory, **delete_memory**
- `subagent.py` — delegate_task (max depth 3, retried 2×)
- `mcp.py` — mcp_connect, mcp_disconnect, mcp_call_tool, mcp_list, mcp_list_resources, mcp_read_resource, mcp_list_prompts, mcp_get_prompt (connected servers expose functions as `mcp__<server>__<tool>`)
- `undo.py` — undo, undo_history
- `context_manage.py` — pin/unpin files into the pinned-context manager (takes `Agent` at construction → registered manually)
- `planning.py`, `tasks.py` — in-session plan + task list management
- `notepad.py` — shared inter-agent notepad
- `skills.py` — `use_skill` loads a workflow from `.coderAI/skills/*.md`
- `project.py`, `format.py`, `lint.py`, `repl.py`, `vision.py` — project-info, code formatting, linting, Python REPL, image/vision helpers
- `package_manager.py`, `refactor.py`, `testing.py` — package install/remove, rename_symbol/find_references refactor, test runner dispatch

**Agent personas** are `.md` files in `.coderAI/agents/` with YAML frontmatter (`name`, `description`, `tools`, `model`). Built-in personas: planner, code-reviewer, architect, security-reviewer, tdd-guide, and others. The `delegate_task` tool spawns these as isolated sub-agents.

**Project-level config** lives in `.coderAI/`:
- `agents/*.md` — persona definitions
- `skills/*.md` — reusable skill workflows (loaded by `use_skill` tool)
- `rules/*.md` — project rules injected into system prompt automatically
- `hooks.json` — pre/post tool hooks (shell commands run around tool execution)
- `config.json` — project-scoped config overrides

**Configuration** is read from `~/.coderAI/config.json` then overridden by environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `CODERAI_DEFAULT_MODEL`, `CODERAI_TEMPERATURE`, `CODERAI_ALLOW_LOCAL_URLS=1` for SSRF bypass, etc.). Per-project instructions go in `CODERAI.md` at project root.

## Interactive chat commands

Inside `coderAI chat` (the Textual TUI), slash commands are available:
`/help`, `/model <name>`, `/clear`, `/compact`, `/reasoning`, `/yolo`, `/verbose`, `/agents`, `/show`, `/think`, `/exit`.
They are routed by `coderAI/tui/slash.py` to the in-process `UIBridge`,
which dispatches them to the agent. Reference output (`/show models`,
`/show cost`, `/show status`, `/show info`, `/show tasks`, `/show config`)
is rendered as plain text by `coderAI/tui/commands.py`. The full
event catalog lives in [`CHAT_EVENTS.md`](CHAT_EVENTS.md).

## Model Aliases

Short aliases resolved by `coderAI/llm/anthropic.py` (`MODEL_ALIASES`):
- `opus` → `claude-opus-4-7`
- `sonnet` → `claude-sonnet-4-6`
- `haiku` → `claude-haiku-4-5-20251001`

Friendly versioned aliases also supported: `claude-4.7-opus`, `claude-4.6-sonnet`, `claude-4.5-haiku`, `claude-4-sonnet`, `claude-4-opus`, `claude-4-haiku`, `claude-3.7-sonnet`, `claude-3.5-sonnet`, `claude-3.5-haiku`, `claude-3-opus`.

## Adding a New LLM Provider

1. Create `coderAI/llm/newprovider.py` implementing `LLMProvider` from `base.py`
2. Add a branch in `coderAI/llm/factory.py::create_provider` that matches on model name (or a prefix/SUPPORTED_MODELS set on the class)
3. Expose config fields in `coderAI/system/config.py` (API key, endpoint, default model)

## Adding a New Tool

1. Create a class extending `Tool` in `coderAI/tools/` with a no-arg `__init__` — `tools/discovery.py` will pick it up automatically
2. **Declare a safety class** (Phase 4.1 — `ToolRegistry.validate_classifications()` refuses to start if you don't): `is_read_only = True` if safe to run in parallel; `requires_confirmation = True` for dangerous ops; `is_egress = True` for network egress; or `safe = True` for a mutating tool that only touches the agent's own internal state (plan/tasks/notepad/memory). A mutating tool that declares none is treated as requiring confirmation (fail-closed). Do **not** add high-risk tools to `permissions.HIGH_RISK_NO_BLANKET` casually — those can never be blanket-allowed by name.
3. If the tool needs the `Agent` (e.g. for pinned context), register it manually in `Agent.__init__` after `discover_tools()` — the discovery walker skips classes whose `__init__` has required args

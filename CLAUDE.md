# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
make dev          # or: pip install -e .

# Run
coderAI chat      # launches the Ink UI (downloads a prebuilt binary if missing)
make run          # alias for coderAI chat

# Ink UI (TypeScript + React) — only needed when contributing to the UI
make ui-install       # bun install
make ui-dev           # bun run src/cli.tsx (hot-reload)
make ui-compile       # single-platform standalone binary -> ui/dist/coderai-ui
make ui-compile TARGET=bun-linux-x64   # cross-compile
make ui-compile-all   # all supported platforms

# Test
make test         # or: pytest
pytest tests/test_agent.py::TestClassName::test_method_name   # single test

# Lint & format
make lint         # ruff check (required; same as CI)
make typecheck    # mypy coderAI/ (optional; not fully clean yet)
make format       # black coderAI/ (line length: 100)

# Setup & utilities
make setup        # interactive setup wizard
make quickstart   # complete setup for new developers
make clean        # remove build artifacts
make dist         # build Python distribution
```

## Architecture

CoderAI is an AI coding agent CLI with a split-process UI: the Python
package implements the agent and one-shot utility commands, and the
interactive chat UI is a TypeScript + React (Ink) app compiled to a
standalone per-platform binary. The two halves talk over NDJSON on stdio.

The orchestration is split across four modules so each concern is independently testable:

- `coderAI/agent.py` — `Agent` class: lifecycle, persona loading, provider wiring, session state, sub-agent spawning.
- `coderAI/agent_loop.py` — the per-turn execution loop (retry/backoff for transient LLM errors, JSON-arg coercion, iteration cap). Constants: `MAX_RETRIES_PER_ITERATION=3`, `MAX_CONSECUTIVE_ERRORS=3`, transient-error regex for 429/5xx.
- `coderAI/tool_executor.py` — `ToolExecutor`: confirmation UX for gated tools. Routes through the IPC server when the Ink UI is attached, otherwise prompts in the terminal.
- `coderAI/tool_routing.py` — dispatches a tool-call `function.name` to either `ToolRegistry` or an MCP server. MCP functions use the wire format `mcp__<server>__<tool>` (server must not contain `__`; tool may).
- `coderAI/context_controller.py` — token estimation, truncation, summarization. Reserves `RESPONSE_TOKEN_RESERVE=1024` and `TOOL_OVERHEAD_TOKENS=512` when budgeting.

Per-turn flow (`Agent.process_message()` → `agent_loop`):

1. User input → inject pinned context → proactive context compression if >70% full
2. LLM call with retry logic (max 3 retries, exponential backoff for transient errors)
3. If tool calls returned → read-only tools run in parallel (`asyncio.gather`), mutating tools run sequentially
4. Tool results fed back to LLM → loop continues until final text response (max 50 iterations). Read-only tools run in parallel; `delegate_task` runs in parallel up to **5** concurrent sub-agents per turn (additional delegations are queued in batches of 5); other mutating tools run one at a time.
5. Session saved to `~/.coderAI/history/`

**Runtime shape of `coderAI chat`:**

1. `coderAI/cli.py` → `chat()` calls `binary_manager.ensure_binary()` to
   locate (or download + verify) the Ink binary.
2. The Ink binary spawns `python -m coderAI.ipc.entry` as a subprocess.
3. `coderAI/ipc/entry.py` creates an `Agent`, swaps its streaming handler
   for `IPCStreamingHandler`, and starts `IPCServer.run()`.
4. `IPCServer` subscribes to `event_emitter` and forwards events as
   NDJSON on stdout; commands arrive as NDJSON on stdin.
5. The Ink components in `ui/src/` render events and send commands via
   `ui/src/rpc/agentClient.ts`.

**Key components:**
- `coderAI/agent.py` — Core orchestrator (agentic loop, context management, sub-agent spawning, session lifecycle). Uses whatever `streaming_handler` the embedding process sets; defaults to `None` (non-streaming fallback).
- `coderAI/cli.py` — Click CLI. `chat` launches the Ink binary; the remaining commands (`config`, `history`, `models`, `status`, `cost`, `tasks`, `setup`, `info`) still render with Rich.
- `coderAI/binary_manager.py` — Resolves the Ink binary. Checks `$CODERAI_UI_BINARY`, then a local `ui/dist/coderai-ui` dev build, then a versioned cache at `~/.coderAI/bin/`, finally downloads from GitHub Releases with SHA256 verification.
- `coderAI/ipc/jsonrpc_server.py` — NDJSON event/command bridge between the Python agent and the Ink UI. See [`ui/PROTOCOL.md`](ui/PROTOCOL.md).
- `coderAI/ipc/streaming.py` — `IPCStreamingHandler`: mirrors the old Rich streaming-handler contract but emits `stream_delta` events over the bridge.
- `coderAI/ipc/entry.py` — `python -m coderAI.ipc.entry` entry point invoked by the Ink binary. Honors `CODERAI_MODEL`, `CODERAI_RESUME`, `CODERAI_AUTO_APPROVE`.
- `coderAI/llm/` — LLM providers (openai, anthropic, groq, deepseek, lmstudio, ollama), all extending `base.LLMProvider`. Instantiation goes through `llm/factory.py::create_provider(model, config)` — do not construct providers directly from `agent.py`.
- `coderAI/tools/` — 35+ tools extending `tools/base.Tool`. Registration is automatic via `tools/discovery.py::discover_tools()`, which walks the `coderAI.tools` package and instantiates every `Tool` subclass whose `__init__` takes no required args. Tools requiring constructor args (e.g. `ManageContextTool`, which needs the `Agent`) are registered manually in `Agent`.
- `coderAI/safeguards.py` — reusable validators that run before dangerous actions: interactive-command detection (blocks REPLs invoked via non-interactive pipes), project-directory validation, git-scope guards (prevent operations leaking to a parent repo), staging blocklist for junk files (`.DS_Store`, `__pycache__`, `.coderAI/`, …).
- `coderAI/project_layout.py` — `find_dot_coderai_subdir()` resolves `.coderAI/<subdir>` across project root, cwd, and the package dir (for dev installs). Use this instead of hardcoding `.coderAI/` paths.
- `coderAI/ipc/chat_reference.py` — plain-text renderings of `models`, `cost`, `status`, `config show`, `info`, `tasks list` for the Ink UI slash commands (keeps Ink free of Rich dependencies).
- `coderAI/ui/` — Rich helpers for one-shot CLI commands only (`display.py`). The interactive Rich chat loop was removed when Ink became the sole interactive UI.
- `coderAI/config.py` — Pydantic-based `ConfigManager` reading from `~/.coderAI/config.json` then env vars.
- `coderAI/agents.py` — `AgentPersona` loader for `.coderAI/agents/*.md` files with YAML frontmatter.
- `coderAI/agent_tracker.py` — Singleton `AgentTracker` for observability (status, tokens, cost, cancellation).
- `coderAI/context.py` — Pinned-file context manager with relevance filtering.
- `coderAI/cost.py` — Per-model token cost tracking; enforces `budget_limit` from config.
- `coderAI/history.py` — `Session` + `HistoryManager`; sessions in `~/.coderAI/history/`.
- `coderAI/notepad.py` — Shared in-memory notepad for inter-agent communication.
- `ui/` — TypeScript + Ink UI source. `ui/src/App.tsx` is the root component; `ui/src/rpc/agentClient.ts` spawns the Python agent; `ui/scripts/compile.ts` drives `bun build --compile` honoring `BUN_TARGET` / `PLATFORM` env vars.
- `.github/workflows/ci.yml` — On push/PR: installs `pip install -e ".[dev]"`, runs `ruff check coderAI/`, `pytest`, `test_installation.py`, and `coderAI --version`.
- `.github/workflows/release.yml` — Cross-compiles the Ink binary for darwin-arm64/x64, linux-x64/arm64, windows-x64 on tagged releases, publishes GitHub Release assets with SHA256 sidecars, and uploads the pure-Python wheel to PyPI.

**Tool categories** (`coderAI/tools/`):
- `filesystem.py` — read_file, write_file, search_replace, apply_diff, list_directory, glob_search
- `terminal.py` — run_command (safety blocklist), run_background
- `git.py` — git_add, git_status, git_diff, git_commit, git_log, git_branch, git_checkout, git_stash
- `search.py` — text_search, grep
- `web.py` — web_search (DuckDuckGo), read_url, download_file
- `subagent.py` — delegate_task (max depth 3, retried 2×)
- `mcp.py` — mcp_connect, mcp_call, mcp_list (connected servers expose functions as `mcp__<server>__<tool>`)
- `undo.py` — undo, undo_history
- `context_manage.py` — pin/unpin files into the pinned-context manager (takes `Agent` at construction → registered manually)
- `planning.py`, `tasks.py` — in-session plan + task list management
- `memory.py`, `notepad.py` — persistent memory + shared inter-agent notepad
- `skills.py` — `use_skill` loads a workflow from `.coderAI/skills/*.md`
- `project.py`, `format.py`, `lint.py`, `repl.py`, `vision.py` — project-info, code formatting, linting, Python REPL, image/vision helpers

**Agent personas** are `.md` files in `.coderAI/agents/` with YAML frontmatter (`name`, `description`, `tools`, `model`). Built-in personas: planner, code-reviewer, architect, security-reviewer, tdd-guide, and others. The `delegate_task` tool spawns these as isolated sub-agents.

**Project-level config** lives in `.coderAI/`:
- `agents/*.md` — persona definitions
- `skills/*.md` — reusable skill workflows (loaded by `use_skill` tool)
- `rules/*.md` — project rules injected into system prompt automatically
- `hooks.json` — pre/post tool hooks (shell commands run around tool execution)
- `config.json` — project-scoped config overrides

**Configuration** is read from `~/.coderAI/config.json` then overridden by environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CODERAI_DEFAULT_MODEL`, `CODERAI_TEMPERATURE`, etc.). Per-project instructions go in `CODERAI.md` at project root.

## Interactive chat commands

Inside `coderAI chat` (the Ink UI), slash commands are available:
`/help`, `/model <name>`, `/tokens`, `/context`, `/compact`, `/agents`, `/auto-approve`, `/clear`, `/exit`.
These are dispatched by the Ink frontend and map to commands in the
NDJSON protocol at [`ui/PROTOCOL.md`](ui/PROTOCOL.md). Read-only reference output
(`/models`, `/cost`, `/status`, `/info`, `/tasks`, `/config show`) is rendered
by `coderAI/ipc/chat_reference.py` so Ink never imports Rich.

## Model Aliases

Current Claude model aliases in `coderAI/agents.py` (and Anthropic provider):
- `opus` → `claude-4.6-opus`
- `sonnet` → `claude-4-sonnet`
- `haiku` → `claude-4.5-haiku`

## Adding a New LLM Provider

1. Create `coderAI/llm/newprovider.py` implementing `LLMProvider` from `base.py`
2. Add a branch in `coderAI/llm/factory.py::create_provider` that matches on model name (or a prefix/SUPPORTED_MODELS set on the class)
3. Expose config fields in `coderAI/config.py` (API key, endpoint, default model)

## Adding a New Tool

1. Create a class extending `Tool` in `coderAI/tools/` with a no-arg `__init__` — `tools/discovery.py` will pick it up automatically
2. Set `is_read_only = True` if safe to run in parallel; set `requires_confirmation = True` for dangerous ops
3. If the tool needs the `Agent` (e.g. for pinned context), register it manually in `Agent.__init__` after `discover_tools()` — the discovery walker skips classes whose `__init__` has required args

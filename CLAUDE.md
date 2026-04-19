# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
make dev          # or: pip install -e .

# Run
coderAI chat      # launches the Ink UI (downloads a prebuilt binary if missing)
coderAI "your prompt"
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
make lint         # ruff check + mypy
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

The main execution loop lives in `coderAI/agent.py` (`Agent.process_message()`):

1. User input → inject pinned context → proactive context compression if >70% full
2. LLM call with retry logic (max 3 retries, exponential backoff for transient errors)
3. If tool calls returned → read-only tools run in parallel (`asyncio.gather`), mutating tools run sequentially
4. Tool results fed back to LLM → loop continues until final text response (max 50 iterations)
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
- `coderAI/llm/` — LLM providers (openai, anthropic, groq, deepseek, lmstudio, ollama), all extending `base.LLMProvider`.
- `coderAI/tools/` — 35+ tools extending `tools/base.Tool`, registered in `ToolRegistry`.
- `coderAI/ui/` — Rich helpers for one-shot CLI commands only (`display.py`). The interactive Rich chat loop was removed when Ink became the sole interactive UI.
- `coderAI/config.py` — Pydantic-based `ConfigManager` reading from `~/.coderAI/config.json` then env vars.
- `coderAI/agents.py` — `AgentPersona` loader for `.coderAI/agents/*.md` files with YAML frontmatter.
- `coderAI/agent_tracker.py` — Singleton `AgentTracker` for observability (status, tokens, cost, cancellation).
- `coderAI/context.py` — Pinned-file context manager with relevance filtering.
- `coderAI/cost.py` — Per-model token cost tracking; enforces `budget_limit` from config.
- `coderAI/history.py` — `Session` + `HistoryManager`; sessions in `~/.coderAI/history/`.
- `coderAI/notepad.py` — Shared in-memory notepad for inter-agent communication.
- `ui/` — TypeScript + Ink UI source. `ui/src/App.tsx` is the root component; `ui/src/rpc/agentClient.ts` spawns the Python agent; `ui/scripts/compile.ts` drives `bun build --compile` honoring `BUN_TARGET` / `PLATFORM` env vars.
- `.github/workflows/release.yml` — Cross-compiles the Ink binary for darwin-arm64/x64, linux-x64/arm64, windows-x64 on tagged releases, publishes GitHub Release assets with SHA256 sidecars, and uploads the pure-Python wheel to PyPI.

**Tool categories** (`coderAI/tools/`):
- `filesystem.py` — read_file, write_file, search_replace, apply_diff, list_directory, glob_search
- `terminal.py` — run_command (safety blocklist), run_background
- `git.py` — git_add, git_status, git_diff, git_commit, git_log, git_branch, git_checkout, git_stash
- `search.py` — text_search, grep
- `web.py` — web_search (DuckDuckGo), read_url, download_file
- `subagent.py` — delegate_task (max depth 3, retried 2×)
- `mcp.py` — mcp_connect, mcp_call, mcp_list
- `undo.py` — undo, undo_history

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
`/help`, `/model <name>`, `/tokens`, `/context`, `/compact`, `/agents`, `/clear`, `/exit`.
These are dispatched by the Ink frontend and map to commands in the
NDJSON protocol at [`ui/PROTOCOL.md`](ui/PROTOCOL.md).

## Model Aliases

Current Claude model aliases in `coderAI/agents.py` (and Anthropic provider):
- `opus` → `claude-4.6-opus`
- `sonnet` → `claude-4-sonnet`
- `haiku` → `claude-4.5-haiku`

## Adding a New LLM Provider

1. Create `coderAI/llm/newprovider.py` implementing `LLMProvider` from `base.py`
2. Register it in `coderAI/llm/__init__.py` and `coderAI/config.py`

## Adding a New Tool

1. Create a class extending `Tool` in `coderAI/tools/`
2. Register it in `coderAI/tools/__init__.py` via `ToolRegistry`
3. Set `is_read_only = True` if safe to run in parallel; set `requires_confirmation = True` for dangerous ops

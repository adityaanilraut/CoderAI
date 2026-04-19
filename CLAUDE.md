# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
make dev          # or: pip install -e .

# Run
coderAI chat
coderAI "your prompt"
make run          # alias for coderAI chat

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
make dist         # build distribution
```

## Architecture

CoderAI is an AI coding agent CLI. The main execution loop lives in `coderAI/agent.py` (`Agent.process_message()`):

1. User input → inject pinned context → proactive context compression if >70% full
2. LLM call with retry logic (max 3 retries, exponential backoff for transient errors)
3. If tool calls returned → read-only tools run in parallel (`asyncio.gather`), mutating tools run sequentially
4. Tool results fed back to LLM → loop continues until final text response (max 50 iterations)
5. Session saved to `~/.coderAI/history/`

**Key components:**
- `coderAI/agent.py` — Core orchestrator (agentic loop, context management, sub-agent spawning, session lifecycle)
- `coderAI/cli.py` — Click CLI (`chat`, `config`, `history`, `models`, `status`, `cost`, `tasks`, `setup` commands)
- `coderAI/llm/` — LLM providers (openai, anthropic, groq, deepseek, lmstudio, ollama), all extending `base.LLMProvider`
- `coderAI/tools/` — 35+ tools extending `tools/base.Tool`, registered in `ToolRegistry`
- `coderAI/ui/` — Rich terminal UI: `display.py` (output), `interactive.py` (chat loop + slash commands), `streaming.py`
- `coderAI/config.py` — Pydantic-based `ConfigManager` reading from `~/.coderAI/config.json` then env vars
- `coderAI/agents.py` — `AgentPersona` loader for `.coderAI/agents/*.md` files with YAML frontmatter
- `coderAI/agent_tracker.py` — Singleton `AgentTracker` for observability (status, tokens, cost, cancellation)
- `coderAI/context.py` — Pinned-file context manager with relevance filtering
- `coderAI/cost.py` — Per-model token cost tracking; enforces `budget_limit` from config
- `coderAI/history.py` — `Session` + `HistoryManager`; sessions in `~/.coderAI/history/`
- `coderAI/notepad.py` — Shared in-memory notepad for inter-agent communication

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

Inside `coderAI chat`, slash commands are available:
`/help`, `/model <name>`, `/tokens`, `/context`, `/compact`, `/agents`, `/clear`, `/exit`

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

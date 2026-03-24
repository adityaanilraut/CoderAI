# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install in development mode
make dev          # or: pip install -e .

# Run
coderAI chat
coderAI "your prompt"

# Test
make test         # or: pytest

# Lint & format
make lint         # ruff check coderAI/
make format       # black coderAI/ (line length: 100)

# Other
make clean        # remove build artifacts
make dist         # build distribution
```

Run a single test: `pytest tests/test_agent.py::TestClassName::test_method_name`

## Architecture

CoderAI is an AI coding agent CLI. The main execution loop lives in `coderAI/agent.py` (Agent class):

1. User input → Agent processes with LLM via a provider in `coderAI/llm/`
2. If LLM returns tool calls → execute via `ToolRegistry` in `coderAI/tools/base.py`
3. Tool results are fed back to LLM → loop continues until final response
4. Sessions are persisted in `~/.coderAI/history/`

**Key components:**
- `coderAI/agent.py` — Core orchestrator (message loop, tool execution, session management)
- `coderAI/cli.py` — Click-based CLI entry point
- `coderAI/llm/` — LLM provider implementations (openai, anthropic, groq, deepseek, lmstudio, ollama), all extending `base.LLMProvider`
- `coderAI/tools/` — MCP tool implementations, each extends `tools/base.Tool` and registers in `ToolRegistry`
- `coderAI/ui/` — Rich terminal UI (display, interactive prompt, streaming)
- `coderAI/config.py` — ConfigManager reading from `~/.coderAI/config.json` and env vars
- `coderAI/agents.py` — AgentPersona loader; discovers `.coderAI/agents/*.md` files with YAML frontmatter

**Agent personas** are `.md` files in `.coderAI/agents/` with YAML frontmatter specifying `name`, `description`, `tools`, and `model`. The `delegate_task` tool spawns these specialized agents.

**Configuration** is read from `~/.coderAI/config.json` or environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CODERAI_DEFAULT_MODEL`, etc.). Project-level instructions can be placed in a `CODERAI.md` file.

## Model Aliases

Current Claude model aliases in `coderAI/agents.py`:
- `opus` → `claude-4.6-opus`
- `sonnet` → `claude-4-sonnet`
- `haiku` → `claude-4.5-haiku`

## Adding a New LLM Provider

1. Create `coderAI/llm/newprovider.py` implementing `LLMProvider` from `base.py`
2. Register it in `coderAI/llm/__init__.py` and `coderAI/config.py`

## Adding a New Tool

1. Create a class extending `Tool` in `coderAI/tools/`
2. Register it in `coderAI/tools/__init__.py` via `ToolRegistry`

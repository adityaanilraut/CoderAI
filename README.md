# 🤖 CoderAI

<p align="center">
  <strong>Autonomous AI Pair Programming in Your Terminal</strong>
</p>

<p align="center">
  <a href="#key-features">Key Features</a> •
  <a href="#quickstart">Quickstart</a> •
  <a href="#interactive-cli--slash-commands">Interactive CLI</a> •
  <a href="#core-tools">Core Tools</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#security--permissions">Security</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#development">Development</a>
</p>

---

## Overview

**CoderAI** is a terminal coding agent built with a clean, decoupled architecture: a **UI-agnostic engine** (`coderai.core`) paired with an **interactive terminal UI** (`coderai.cli`).

CoderAI is designed from the ground up for safety, deterministic execution, and developer velocity:

- **Snippet-Scoped File Editing**: `read` yields an anchored `snippet_id`; `edit` strictly targets that snippet with real-time file-version checks to prevent hallucinations, out-of-context changes, and stale overwrites.
- **Bounded Agent Loop**: Deterministic agent turns (`stream → tool_calls → permissions → execute → loop`) with loop-guards and token-threshold compaction.
- **Granular Side-Effect Permissions**: 10 fine-grained permission scopes (`read-in-cwd`, `write-in-cwd`, `mutate-git-log`, `network`, `mcp`, etc.) evaluated before any tool executes.
- **Plan Mode**: Enforces a strict read-only boundary during exploration, planning, and task breakdown.
- **GitFileHistory Checkpointing & Instant Undo**: Every turn is automatically snapshotted into `.git_history`, allowing one-command rollback (`/undo`) and diff inspection (`/diff`).
- **Frontier LLM Support with Thinking Mode**: Native routing and reasoning token rendering for OpenAI (`gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-4o`), Anthropic (`claude-3-7-sonnet`, `claude-3-5-sonnet`), DeepSeek (`deepseek-v4-pro`, `deepseek-r1`), Google Gemini (`gemini-2.5-pro`, `gemini-2.5-flash`), and local Ollama/OpenRouter endpoints.
- **Model Context Protocol (MCP)**: Native stdio MCP client for connecting external custom tools.
- **Context Expansion via `@file`**: Seamlessly reference workspace files in your prompts with auto-attached context.
- **Minimal Core Dependencies**: Pruned to `rich`, `openai`, and `requests`.

---

## Installation

### From Source (Editable Mode)

```bash
# Clone the repository
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI

# Install in editable mode
pip install -e .

# Verify installation
coderai --version
```

### Install with Development Dependencies

```bash
pip install -e ".[dev]"
```

---

## Quickstart

### 1. Set Your API Key

```bash
# Standard OpenAI API Key
export OPENAI_API_KEY="sk-..."

# Or specify a custom base URL (e.g. OpenRouter, DeepSeek, Ollama, Gemini)
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

You can also create a `.env` file or configure settings in `~/.coderai/settings.json` or `.coderai/settings.json`.

### 2. Launch Interactive REPL

```bash
coderai
```

### 3. Launch in Plan Mode

```bash
coderai --plan
```

### 4. Run a One-Shot Task

```bash
coderai "Refactor auth middleware to support JWT refresh tokens"
```

### 5. Auto-Approve Permissions for CI / Scripting

```bash
coderai --yes "Run unit tests and fix any failing assertions in tests/test_core.py"
```

---

## Interactive CLI & Slash Commands

When running `coderai`, you enter an interactive REPL featuring an ASCII banner, real-time token status bar, thinking block display, and live diff rendering.

### Slash Commands

| Command | Description |
|---|---|
| `/continue` | Continue bounded multi-step agent execution |
| `/plan` | Toggle Plan Mode on/off with visual indicator |
| `/undo` | Revert workspace files and history to the previous turn checkpoint |
| `/diff` | Show syntax-highlighted unified diff of changes made since session start |
| `/model [name]` | Open interactive model selector or switch directly to a named model |
| `/sessions` | Open interactive session browser with quick resume |
| `/resume <id>` | Resume a saved session directly by session ID |
| `/new` | Start a fresh session in the current project |
| `/skills` | Browse active and workspace-discovered skills |
| `/skill <name>` | Load a skill into the active session |
| `/help`, `/?` | Display the interactive command help menu |
| `/exit`, `/quit` | Exit session and display the exit summary card |

### Context Expansion (`@file` Mentions)

You can mention files directly in your prompts:

```text
coderai> Explain the architecture in @coderai/core/session.py and how it connects to @coderai/core/permissions.py
```

CoderAI automatically detects the files, reads their contents, and injects them into the prompt context.

---

## Core Tools

CoderAI provides a focused, high-leverage built-in tool surface:

| Tool | Description |
|---|---|
| **`read`** | Reads files with offset and line limits, image support, and generates anchored `snippet_id` metadata. |
| **`edit`** | Performs precise scoped string replacement anchored to a `snippet_id` with staleness detection and whitespace tolerance. |
| **`write`** | Creates new files or atomically overwrites existing files. |
| **`bash`** | Executes shell commands with working directory tracking, timeout enforcement, side-effect detection, and background job support (`run_in_background`). |
| **`AskUserQuestion`** | Prompts the user interactively with single-choice, multiple-choice, or free-form questions when design decisions arise. |
| **`UpdatePlan`** | Manages a real-time markdown task checklist rendered directly in the terminal. |
| **`WebSearch`** | Queries the web for live documentation, APIs, and error solutions. |
| **`UnderstandImage`** | Analyzes local PNG/JPEG/WebP images for visual context. |
| **`subagent`** | Spawns isolated child agents for specialized exploration or refactoring tasks. |

---

## Architecture

CoderAI maintains a strict boundary between the engine (`coderai.core`) and presentation (`coderai.cli`):

```
coderai/
├── main.py                     # Console entry point -> coderai.cli.app.main
├── py.typed                    # PEP 561 typing marker
│
├── cli/                        # Interactive Presentation Layer (Rich UI)
│   ├── app.py                  # CLI argument parser, REPL, permissions prompt
│   ├── ascii_art.py            # Brand banner and header rendering
│   ├── diff_render.py          # Syntax-highlighted diff preview
│   ├── exit_summary.py         # Session metrics & duration summary
│   ├── file_mention.py         # @file path detection and context expansion
│   ├── interactive_menu.py     # Interactive model, session, and skill pickers
│   ├── plan_render.py          # Plan checklist table rendering
│   ├── status_bar.py           # Dynamic status bar (model, tokens, plan status)
│   ├── thinking.py             # Reasoning / thinking block renderer
│   ├── tool_card.py            # Tool execution cards with timing & results
│   └── welcome.py              # Welcome screen & quick-start guide
│
└── core/                       # UI-Agnostic Core Engine
    ├── session.py              # SessionManager (bounded agent loop, JSONL persistence)
    ├── prompt.py               # System prompt builder, tool schemas, skills loader
    ├── permissions.py          # Side-effect permission scopes & policy evaluation
    ├── settings.py             # Settings resolver (project + user + environment)
    ├── state.py                # Snippet manager, file versioning, staleness detection
    ├── openai_client.py        # Client factory & multi-provider auto-routing
    ├── subagent.py             # Subagent orchestrator
    │
    ├── tools/                  # Built-in tool handlers
    │   ├── read.py             # File reader & snippet creator
    │   ├── edit.py             # Scoped string replacer
    │   ├── write.py            # File writer
    │   ├── bash.py             # Shell executor
    │   ├── ask_user_question.py# Interactive user questions
    │   ├── update_plan.py      # Implementation plan checklist
    │   ├── web_search.py       # Web search handler
    │   ├── understand_image.py # Image inspection
    │   └── subagent.py         # Subagent tool interface
    │
    ├── mcp/                    # Model Context Protocol (MCP)
    │   ├── client.py           # Stdio MCP client
    │   └── manager.py          # Dynamic tool discovery & dispatch
    │
    └── common/                 # Core Utilities
        ├── file_history.py     # Git-backed checkpointing, diff, and undo
        ├── message_converter.py# OpenAI message formatting & turn recovery
        ├── model_capabilities.py# Frontier model registry & feature detection
        ├── openai_thinking.py  # Reasoning tokens extractor
        ├── shell_utils.py      # Shell side-effect analyzer
        ├── process_tree.py     # Subprocess tree cleanup
        └── bash_timeout.py     # Command execution timeout wrapper
```

---

## Security & Permissions

CoderAI follows a **defense-in-depth, fail-closed** security model. Every tool invocation evaluates side-effect scopes before execution.

### Permission Scopes

| Scope | Description | Default Policy |
|---|---|---|
| `read-in-cwd` | Reading files within workspace | `allow` |
| `read-out-cwd` | Reading files outside workspace | `ask` |
| `write-in-cwd` | Writing files within workspace | `ask` (or `allow` based on config) |
| `write-out-cwd` | Writing files outside workspace | `ask` |
| `delete-in-cwd` | Deleting files within workspace | `ask` |
| `delete-out-cwd` | Deleting files outside workspace | `deny` |
| `query-git-log` | Inspecting git status/log | `allow` |
| `mutate-git-log` | Committing or altering git branches | `ask` |
| `network` | Web search and outbound HTTP | `ask` |
| `mcp` | MCP tool execution | `ask` |

---

## Configuration

Settings are resolved in order of precedence: **CLI arguments > Environment variables > Project config (`.coderai/settings.json`) > User config (`~/.coderai/settings.json`)**.

### Configuration Example (`settings.json`)

```json
{
  "model": "gpt-5.6-luna",
  "temperature": 0.2,
  "thinkingEnabled": true,
  "reasoningEffort": "max",
  "permissions": {
    "defaultMode": "allowAll",
    "allow": ["read-in-cwd", "query-git-log"],
    "ask": ["write-in-cwd", "write-out-cwd", "delete-in-cwd", "mutate-git-log", "network", "mcp"],
    "deny": ["delete-out-cwd"]
  },
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    }
  }
}
```

### Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` / `CODERAI_API_KEY` | LLM provider API key |
| `OPENAI_BASE_URL` / `CODERAI_BASE_URL` | API endpoint URL (OpenAI, DeepSeek, Ollama, etc.) |
| `CODERAI_MODEL` | Default model identifier |
| `CODERAI_THINKING_ENABLED` | Enable reasoning/thinking tokens (`true` / `false`) |
| `CODERAI_REASONING_EFFORT` | Reasoning effort (`low`, `medium`, `high`, `max`) |
| `CODERAI_DEBUG_LOG_ENABLED` | Enable verbose engine debug logging |

---

## Development

CoderAI includes a comprehensive offline test and verification suite.

```bash
# Format code
make format

# Run linter
make lint

# Run type checks
make typecheck

# Run test suite
make test

# Run offline engine self-check
python scripts/self_check_core.py

# Clean build artifacts
make clean
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

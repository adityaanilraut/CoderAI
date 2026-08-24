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
  <a href="#development">Development</a> •
  <a href="#documentation">Documentation</a>
</p>

---

## Overview

**CoderAI** is an autonomous terminal coding agent built with a clean, decoupled architecture: a **UI-agnostic engine** (`coderai.core`) paired with a high-performance **interactive terminal UI** (`coderai.cli`).

CoderAI is designed from the ground up for safety, deterministic execution, and developer velocity:

- **Snippet-Scoped File Editing**: `read` yields an anchored `snippet_id`; `edit` strictly targets that snippet with real-time file-version checks to prevent hallucinations, out-of-context changes, and stale overwrites.
- **Bounded Agent Loop**: Deterministic agent turns (`stream → tool_calls → permissions → execute → post-process → loop`) with loop-guards and token-threshold compaction.
- **Granular Permissions & OS Sandboxing**: 10 fine-grained permission scopes (`read-in-cwd`, `write-in-cwd`, `mutate-git-log`, `network`, `mcp`, etc.), 3 security presets (`read-only`, `workspace-write`, `danger-full-access`), and native OS sandboxing (macOS Seatbelt, Linux Bubblewrap).
- **Plan Mode**: Strict read-only boundary during exploration, planning, and task breakdown.
- **GitFileHistory Checkpointing & Instant Undo**: Every turn is automatically snapshotted into `.git_history`, allowing one-command rollback (`/undo`) and diff inspection (`/diff`).
- **High-Performance Search & Discovery**: Bundled ripgrep binary (`glob` / `grep`) with automatic Python fallback and spill-to-disk locators for large search results.
- **Multi-Agent Orchestration & Teams Swarm**: Continuable background subagents (`subagent`, `send_message`), agent team task boards (`spawn_teammate`, `team_task_*`), and automated verification loops (`ralph`).
- **Developer Subsystems**: Language Server Protocol (`lsp`), persistent PTY terminals (`terminal_*`), async workflow scripts (`workflow`), in-process Python execution (`code_mode`), and full-text session search (`session_query`).
- **Frontier LLM Support with Thinking Mode**: Native routing and reasoning token rendering for OpenAI (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`), DeepSeek (`deepseek-v4-pro`, `deepseek-v4-flash`), Google Gemini (`gemini-3.7-flash`), Anthropic (`claude-3-7-sonnet`), OpenRouter, and local Ollama endpoints.
- **Model Context Protocol (MCP)**: Native stdio, SSE, and streamable-http MCP client for connecting external custom tools, prompts, and resources.
- **Context Expansion via `@file`**: Seamlessly reference workspace files in your prompts with auto-attached context and line-range slicing.
- **Minimal Core Dependencies**: Pruned to `rich`, `openai`, `requests`, and `pyyaml`.

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
# or using shorthand alias
cai --version
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
# or
cai
```

### 3. Launch in Plan Mode

```bash
coderai --plan
```

### 4. Run a One-Shot Task

```bash
coderai "Refactor auth middleware to support JWT refresh tokens"
```

One-shot prompts default to the `core` tool preset. Use `--preset` to select one of:
- `full`: all built-in tools
- `core`: shell, file editing, reads, writes, glob, and grep
- `shell_edit`: only `bash` and `str_replace_editor`

### 5. Auto-Approve Permissions for CI / Scripting

```bash
coderai --yes "Run unit tests and fix any failing assertions in tests/test_core.py"
```

---

## Interactive CLI & Slash Commands

When running `coderai`, you enter an interactive REPL featuring an ASCII banner, real-time token status bar, thinking block display, live diff rendering, and command autocompletion.

### Slash Commands

| Command | Description |
|---|---|
| `/continue` | Continue bounded multi-step agent execution |
| `/plan` | Toggle Plan Mode on/off with visual indicator |
| `/undo` | Revert workspace files and history to the previous turn checkpoint |
| `/diff` | Show syntax-highlighted unified diff of changes made since session start |
| `/model [name]` | Open interactive model selector or switch directly to a named model |
| `/sessions` | Open interactive session browser (resume, delete, fork) |
| `/resume <id>` | Resume a saved session directly by session ID |
| `/fork [id]` | Fork current or specified session into a new branch/session |
| `/delete <id>` | Delete a saved session from workspace storage |
| `/new` | Start a fresh session in the current project |
| `/goal [action]` | View or manage session goals and milestones |
| `/permission [preset]` | View or set permission preset (`read-only`, `workspace-write`, `danger-full-access`) |
| `/init` | Generate or update `AGENTS.md` contributor guidelines for the workspace |
| `/skills` | Browse active and workspace-discovered skills |
| `/skill <name>` | Load a skill into the active session |
| `/mcp` | Inspect connected Model Context Protocol (MCP) servers, tools, prompts, and resources |
| `/tokens`, `/cost` | Display detailed token usage breakdown and active context analytics |
| `/compact` | Compress conversation history to reclaim context window tokens |
| `/config` | View resolved workspace and user settings |
| `/history` | View turn-by-turn conversation timeline |
| `/export [file]` | Export session conversation to Markdown or JSON |
| `/thinking [mode]` | Toggle reasoning traces between full trace and concise summary |
| `/raw [mode]` | Alias of `/thinking` for lite / normal / raw-scrollback display |
| `/clear` | Clear terminal screen and refresh prompt status |
| `/help`, `/?` | Display categorized interactive command help menu |
| `/exit`, `/quit` | Exit session and display the exit summary card |

### Tab Autocompletion & Command History

CoderAI includes full `readline` and fuzzy autocompletion:
- **Tab autocompletion**: Press `Tab` on `/` commands, `/model` targets, and `@` file paths.
- **Persistent history**: Arrow `Up` / `Down` to cycle through previous prompts across sessions (persisted at `~/.coderai/history`).

### Context Expansion (`@file` Mentions)

Mention workspace files directly anywhere in your prompts:

```text
coderai> Explain the architecture in @coderai/core/session.py and how it connects to @coderai/core/permissions.py
```

CoderAI automatically detects referenced files and attaches their contents to the prompt context. Specific line ranges can also be targeted with `@file.py:10-30` or `@file.py:L25`.

---

## Core Tools

CoderAI provides a rich, versatile tool surface:

| Tool | Category | Description |
|---|---|---|
| **`read`** | File Ops | Reads files with offset and line limits, image support, and generates anchored `snippet_id` metadata. |
| **`edit`** | File Ops | Performs precise scoped string replacement anchored to a `snippet_id` with staleness detection. |
| **`write`** | File Ops | Creates new files or atomically overwrites existing files. |
| **`str_replace_editor`** | File Ops | Universal file editor (`view`, `create`, `str_replace`, `insert`, `undo_edit`). |
| **`glob`** | Search | Fast file pattern discovery with result caps and disk spilling. |
| **`grep`** | Search | High-performance content search via bundled ripgrep binary. |
| **`bash`** | Process | Shell execution with timeout enforcement, process group isolation, and background jobs. |
| **`job_list` / `job_output` / `job_kill`** | Process | Background job management and streaming output inspection. |
| **`pwsh`** | Process | Cross-platform PowerShell command execution. |
| **`terminal_*`** | PTY | Interactive pseudoterminal sessions (`terminal_open`, `terminal_send`, `terminal_read`, `terminal_signal`, `terminal_close`, `terminal_list`). |
| **`subagent` / `subagent_fork`** | Multi-Agent | Continuable background subagents and one-shot task delegation. |
| **`send_message` / `interrupt_agent`** | Multi-Agent | Inter-agent messaging and subagent control plane. |
| **`spawn_teammate` / `team_task_*`** | Swarm | Agent team collaboration with shared task board and synchronization (`wait_agent`). |
| **`lsp`** | Code Intel | Language Server Protocol integration for definitions, references, and symbol navigation. |
| **`goal` / `todo_write`** | Planning | Session goal tracking and structured task breakdowns. |
| **`workflow`** | Automation | Multi-phase asynchronous Python workflow scripting engine. |
| **`ralph`** | Verification | Automated test-driven feedback and completion verification harness. |
| **`code_mode`** | Execution | Sandboxed in-process Python execution for data manipulation and analysis. |
| **`session_query`** | Search | Full-text query and indexing across past conversation sessions. |
| **`WebSearch` / `WebFetch`** | Web | Live web search and URL fetching with SSRF protection and Markdown conversion. |
| **`UnderstandImage`** | Media | Image understanding for local visual assets. |
| **`AskUserQuestion`** | User | Interactive questionnaires and user decision modals. |
| **`skill`** | Skills | Loads discovered workspace skills (`SKILL.md`). |

---

## Architecture

```
coderai/
├── main.py                         # Console entry point -> coderai.cli.app.main
├── py.typed                        # PEP 561 typing marker
│
├── cli/                            # Interactive Presentation Layer (Rich UI)
│   ├── app.py                      # CLI argument parser, REPL, permissions prompt
│   ├── commands.py                 # Canonical slash-command catalog
│   ├── session_factory.py          # Shared SessionManager construction
│   ├── ascii_art.py                # Brand banner and header rendering
│   ├── completer.py                # Readline autocompletion & fuzzy matching
│   ├── diff_render.py              # Syntax-highlighted diff preview
│   ├── exit_summary.py             # Session metrics & duration summary
│   ├── export_render.py            # Session export formatting
│   ├── file_mention.py             # @file path detection and context expansion
│   ├── fuzzy.py                    # Fuzzy subsequence ranking
│   ├── interactive_menu.py         # Interactive model, session, and skill pickers
│   ├── plan_render.py              # Plan checklist table rendering
│   ├── status_bar.py               # Dynamic status bar (model, tokens, plan status)
│   ├── thinking.py                 # Reasoning / thinking block renderer
│   ├── tool_card.py                # Tool execution cards with timing & results
│   └── welcome.py                  # Welcome screen & quick-start guide
│
├── core/                           # UI-Agnostic Core Engine
│   ├── agents.py                   # Continuable subagent manager
│   ├── agent_loop.py               # Turn/step activation controller
│   ├── goals.py                    # Session goals store (/goal)
│   ├── hooks.py                    # PreToolUse hook execution
│   ├── jobs.py                     # Background process job store
│   ├── openai_client.py            # Client factory & multi-provider auto-routing
│   ├── permissions.py              # Side-effect permission scopes & policy evaluation
│   ├── prompt.py                   # System prompt builder & tool schemas
│   ├── prompt_sections.py          # Stable tool order & prompt section builder
│   ├── sandbox.py                  # OS sandbox (Seatbelt / bwrap)
│   ├── schedule.py                 # Schedule & timer subsystem
│   ├── session.py                  # SessionManager public lifecycle APIs
│   ├── session_store.py            # Canonical JSONL session storage
│   ├── session_log.py              # Message derivation helpers
│   ├── settings.py                 # Settings resolver (project + user + environment)
│   ├── spill.py                    # Spill-to-file store & locators
│   ├── state.py                    # Snippet manager, file versioning, staleness detection
│   ├── subagent.py                 # Subagent orchestrator
│   ├── web_providers.py            # Pluggable web search backends
│   │
│   ├── code_mode/                  # Interactive Python execution engine
│   ├── common/                     # Core Utilities (history, process tree, error logger)
│   ├── lsp/                        # Language Server Protocol client
│   ├── mcp/                        # Model Context Protocol (stdio, SSE, streamable-http)
│   ├── network/                    # Outbound HTTP, cache, SSRF, sanitizer
│   ├── session_query/              # Session history search over JSONL
│   ├── skill/                      # Skill discovery and loading
│   ├── teams/                      # Agent swarm & task board
│   ├── terminal/                   # PTY terminal manager
│   ├── tools/                      # Built-in tool implementations
│   └── workflow/                   # Multi-phase workflow scripting engine
│
├── vendor/                         # Bundled binaries (ripgrep)
└── skills/                         # Built-in agent skills
```

---

## Security & Permissions

CoderAI follows a **defense-in-depth, fail-closed** security model.

### Permission Scopes

| Scope | Description | Default Policy |
|---|---|---|
| `read-in-cwd` | Reading files within workspace | `allow` |
| `read-out-cwd` | Reading files outside workspace | `ask` |
| `write-in-cwd` | Writing files within workspace | `ask` |
| `write-out-cwd` | Writing files outside workspace | `ask` |
| `delete-in-cwd` | Deleting files within workspace | `ask` |
| `delete-out-cwd` | Deleting files outside workspace | `deny` |
| `query-git-log` | Inspecting git status/log | `allow` |
| `mutate-git-log` | Committing or altering git branches | `ask` |
| `network` | Web search and outbound HTTP | `ask` |
| `mcp` | MCP tool execution | `ask` |

### Permission Presets

- **`read-only`**: Blocks all mutations and confines execution to read operations with an active OS sandbox.
- **`workspace-write`**: Grants write access only within the workspace with an active OS sandbox.
- **`danger-full-access`**: Full access with user confirmation.

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
  "toolsPreset": "core",
  "permissions": {
    "preset": "workspace-write",
    "allow": ["read-in-cwd", "query-git-log"],
    "ask": ["write-in-cwd", "write-out-cwd", "delete-in-cwd", "mutate-git-log", "network", "mcp"],
    "deny": ["delete-out-cwd"]
  },
  "mcpServers": {
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./app.db"]
    },
    "remote-docs": {
      "url": "https://mcp.example.com/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer token"
      }
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
| `CODERAI_TOOLS_PRESET` | Tool preset (`full`, `core`, `shell_edit`) |
| `CODERAI_PERMISSION_PRESET` | Permission preset (`read-only`, `workspace-write`, `danger-full-access`) |
| `CODERAI_THINKING_ENABLED` | Enable reasoning/thinking tokens (`true` / `false`) |
| `CODERAI_REASONING_EFFORT` | Reasoning effort (`off`, `low`, `medium`, `high`, `max`) |
| `CODERAI_RG_PATH` | Path to custom ripgrep executable (defaults to bundled binary) |
| `CODERAI_DEBUG_LOG_ENABLED` | Enable verbose engine debug logging |

### Migration Notes

- Project configuration and tracked agent content now use lowercase `.coderai`; rename `.coderAI` directories before upgrading.
- Workspace skills use the singular filename `SKILL.md`; rename legacy `SKILLS.md` files.
- Use `--prompt` (or a positional prompt) instead of the removed `--message` flag.
- Use `--preset` instead of the removed `--tools-preset` spelling.
- Tool presets accept only `full`, `core`, or `shell_edit`.
- Settings JSON uses the documented camelCase keys (`baseURL`, `apiKey`, `thinkingEnabled`, `reasoningEffort`, and `toolsPreset`); environment overrides use `CODERAI_*`.

---

## Development

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

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md)
- [Tool Reference](docs/TOOLS.md)
- [Permissions & Security Model](docs/PERMISSIONS.md)
- [Model Context Protocol (MCP)](docs/MCP.md)
- [Architectural Reference Matrix](docs/PARITY_REFERENCE.md)

---

## Acknowledgements

CoderAI builds upon and adapts architectural concepts, protocol designs, and patterns from **[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)**, released under the MIT License.

We gratefully acknowledge the DeepSeek Harness project and its contributors for pioneering:
- **Snippet-Anchored Precision Editing**: The robust `read` (anchored `snippet_id`) to `edit` pattern with whitespace tolerance and staleness detection.
- **Bounded Agent Loop & Token Compaction**: Deterministic turn cycles, multi-turn loop guards, and intelligent context compaction.
- **Side-Effect Permission Scopes**: The 10-scope fail-closed security architecture (`read-in-cwd`, `mutate-git-log`, `mcp`, etc.).
- **Interactive Terminal Workflow**: Advanced input buffering, dynamic status bar, and interactive menu patterns.

In compliance with the MIT License terms, original copyright notices and attribution are maintained across adapted core modules.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

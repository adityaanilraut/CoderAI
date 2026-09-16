# 🤖 CoderAI

<p align="center">
  <strong>Autonomous AI Pair Programming & Multi-Agent Swarm in Your Terminal</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Benchmark-100%25%20SWT--Bench-success" alt="Benchmark">
</p>

<p align="center">
  <a href="#overview">Overview</a> •
  <a href="#-benchmark--performance">Benchmark</a> •
  <a href="#quickstart">Quickstart</a> •
  <a href="#interactive-cli--slash-commands">Interactive CLI</a> •
  <a href="#agent-roles--swarms">Agent Roles & Swarms</a> •
  <a href="#core-tools">Core Tools</a> •
  <a href="#security--permissions">Security</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#development">Development</a> •
  <a href="#license">License</a>
</p>

---

## Overview
<a id="key-features"></a>

**CoderAI** is an autonomous terminal AI pair programmer designed for high reliability, deterministic tool execution, token efficiency, and developer velocity. It couples a headless core engine (`coderai.core`) with a rich interactive terminal interface (`coderai.cli`).

- 🎯 **Snippet-Anchored Editing**: Precise, hash-anchored edits with real-time staleness detection to eliminate hallucinated overwrites.
- 🔄 **Bounded Agent Loop**: Deterministic turn execution with loop guards, repeat reminders, and token compaction.
- 🛡️ **Defense-in-Depth Security**: 10 fine-grained permission scopes, 3 security presets, and native OS sandboxing (Seatbelt / Bubblewrap).
- ⏪ **Turn Checkpoints & Instant Undo**: Automatic `.git_history` snapshots enable one-command rollback (`/undo`) and diff inspection (`/diff`).
- ⚡ **High-Performance Search**: Bundled native `ripgrep` binary with spill-to-disk locators for large search outputs.
- 🤖 **Multi-Agent Teams & Swarm**: Continuable background subagents, shared team task boards, and automated verification loops (`ralph`).
- 🧠 **Frontier Models & MCP**: Native reasoning token support (OpenAI, DeepSeek, Gemini, Claude) and Model Context Protocol integration.

---

## 📊 Benchmark & Performance: SWT-Bench Verified

CoderAI was evaluated on a comprehensive 10-task benchmark combining **SWT-Bench Real-World Repository Tasks** (targeting large-scale production codebases like `psf/requests`, `pallets/flask`, and `sympy/sympy`) and **Complex Local Engineering Challenges** (concurrency write-ahead logging, dependency graph cycles, TTL LRU caches, markdown table formatters, and spiral matrices) head-to-head against leading coding agent harnesses (**Claude Code** and **OpenCode**), all running on the frontier model **`deepseek-v4-flash`**.

---

### 🏆 Head-to-Head Benchmark Summary

| Metric                         | 🤖 CoderAI         | 🟣 Claude Code | 🟢 OpenCode  |                    Winner                    |
| ------------------------------ | ------------------ | -------------- | ------------ | :------------------------------------------: |
| **Success Rate (Accuracy)**    | **100.0% (10/10)** | 80.0% (8/10)   | 80.0% (8/10) |                🥇 **CoderAI**                |
| **Resolved Tasks**             | **10 / 10**        | 8 / 10         | 8 / 10       |                🥇 **CoderAI**                |
| **Average Execution Time**     | **73.8s**          | 115.3s         | 86.9s        |        🥇 **CoderAI** _(36% faster)_         |
| **Median Execution Time**      | **54.7s**          | 79.7s          | 80.6s        |        🥇 **CoderAI** _(31% faster)_         |
| **P90 Execution Time**         | **130.2s**         | 240.3s         | 130.6s       |      🥇 **CoderAI** _(46% faster tail)_      |
| **Average Total Tokens**       | **467,064**        | 611,771        | 729,681      |     🥇 **CoderAI** _(36% fewer tokens)_      |
| **KV Cache Hit Rate**          | **95.9%**          | 94.0%          | 94.5%        |                🥇 **CoderAI**                |
| **Total API Cost**             | **$0.1069**        | $0.1602        | $0.1611      |       🥇 **CoderAI** _(33.5% savings)_       |
| **Time to First Token (TTFT)** | **3.4s**           | 12.8s          | 58.3s        |        🥇 **CoderAI** _(3.8x faster)_        |
| **Total Reasoning Tokens**     | **32,338**         | 70,599         | 54,325       |     🥇 **CoderAI** _(54% fewer wasted)_      |
| **Total Tool Execution Time**  | **73.7s**          | 149.7s         | 900.6s       | 🥇 **CoderAI** _(12.2x faster tool runtime)_ |
| **Tool Time Ratio**            | **10.0%**          | 13.0%          | 103.7%       |      🥇 **CoderAI** _(Lean execution)_       |

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

### 1. Configure Providers, API Keys & Models (Interactive Setup)

You can run the interactive setup wizard directly from the command line or from within CoderAI:

```bash
# Interactive setup wizard
coderai setup

# Or configure non-interactively
coderai setup --provider deepseek --key sk-... --setup-model deepseek-v4-pro
coderai setup --provider gemini --key AIzaSy... --setup-model gemini-3.7-flash
coderai setup --base-url http://localhost:11434/v1 --setup-model qwen2.5-coder:32b --provider ollama

# Test active provider connectivity
coderai setup --test

# View configured keys & endpoints status table
coderai setup --status
```

Alternatively, you can export environment variables in your shell or `.env` file:
```bash
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
export GEMINI_API_KEY="AIzaSy..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 2. Launch Interactive REPL

```bash
coderai
# or
cai
```

Inside the REPL, you can type `/setup` at any time to add or update API keys, switch default models, or configure local LLM endpoints.

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

### 6. Launch with Specialized Agent Roles

```bash
# Launch interactive session with a specific role
coderai --agent architect
coderai --agent tdd-guide

# Launch with custom role specification file
coderai --agent-file .coderai/agents/code-reviewer.md
```

---

## Interactive CLI & Slash Commands

When running `coderai`, you enter an interactive REPL featuring an ASCII banner, real-time token status bar, thinking block display, live diff rendering, and command autocompletion.

### Slash Commands

| Command                | Description                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `/setup`               | Interactive setup wizard: configure API keys, providers, local endpoints & models     |
| `/continue`            | Continue bounded multi-step agent execution                                           |
| `/plan`                | Toggle Plan Mode on/off with visual indicator                                         |
| `/undo`                | Revert workspace files and history to the previous turn checkpoint                    |
| `/diff`                | Show syntax-highlighted unified diff of changes made since session start              |
| `/model [name]`        | Open interactive model selector or switch directly to a named model                   |
| `/sessions`            | Open interactive session browser (resume, delete, fork)                               |
| `/resume <id>`         | Resume a saved session directly by session ID                                         |
| `/fork [id]`           | Fork current or specified session into a new branch/session                           |
| `/delete <id>`         | Delete a saved session from workspace storage                                         |
| `/new`                 | Start a fresh session in the current project                                          |
| `/goal [action]`       | View or manage session goals and milestones                                           |
| `/permission [preset]` | View or set permission preset (`read-only`, `workspace-write`, `danger-full-access`)  |
| `/init`                | Generate or update `AGENTS.md` contributor guidelines for the workspace               |
| `/agent [role]`        | View or switch active agent role (`architect`, `tdd-guide`, `code-reviewer`, etc.)    |
| `/agents [action]`     | Inspect subagent runs and browse discovered role specifications (`/agents roles`)    |
| `/schedule [action]`   | View or manage scheduled reminders and background cron jobs                           |
| `/skills`              | Browse active and workspace-discovered skills                                         |
| `/skill <name>`        | Load a skill into the active session                                                  |
| `/mcp`                 | Inspect connected Model Context Protocol (MCP) servers, tools, prompts, and resources |
| `/tokens`, `/cost`     | Display detailed token usage breakdown and active context analytics                   |
| `/compact`             | Compress conversation history to reclaim context window tokens                        |
| `/config`              | View resolved workspace and user settings                                             |
| `/history`             | View turn-by-turn conversation timeline                                               |
| `/export [file]`       | Export session conversation to Markdown or JSON                                       |
| `/thinking [mode]`     | Toggle reasoning traces between full trace and concise summary                        |
| `/raw [mode]`          | Alias of `/thinking` for lite / normal / raw-scrollback display                       |
| `/clear`               | Clear terminal screen and refresh prompt status                                       |
| `/login` / `/logout`   | Log in (platform + key + model) / log out (clear credentials)                         |
| `/version`             | Show CLI version                                                                      |
| `/changelog`           | Show recent changelog (`/release-notes`)                                              |
| `/feedback [text]`     | Submit feedback (falls back to GitHub Issues)                                         |
| `/reload`              | Reload configuration without exiting                                                  |
| `/debug`               | Context debug info (messages/tokens/checkpoints/history)                              |
| `/usage`               | API usage / quota with progress bar (`/status`, `/quota`)                             |
| `/hooks`               | Show configured hooks                                                                 |
| `/task`                | Interactive background-task browser (list/detail/output)                              |
| `/upgrade`             | Upgrade coderai-agent via pip                                                         |
| `/skill:<name>`        | Load a skill via colon syntax (args passthrough)                                      |
| `/help`, `/?`          | Display categorized interactive command help menu                                     |
| `/exit`, `/quit`       | Exit session and display the exit summary card                                        |

### Tab Autocompletion & Command History

CoderAI includes full `readline` and fuzzy autocompletion:

- **Tab autocompletion**: Press `Tab` on `/` commands, `/model` targets, and `@` file paths.
- **Persistent history**: Arrow `Up` / `Down` to cycle through previous prompts across sessions (persisted at `~/.coderai/history`).

### Context Expansion (`@file` Mentions)

Mention workspace files directly anywhere in your prompts:

```text
coderai> Explain the architecture in @coderai/soul/session/manager.py and how it connects to @coderai/soul/approval.py
```

CoderAI automatically detects referenced files and attaches their contents to the prompt context. Specific line ranges can also be targeted with `@file.py:10-30` or `@file.py:L25`.

---

## Agent Roles & Swarms

CoderAI supports dynamic discovery of specialized markdown agent specifications (`.coderai/agents/*.md`, `.agents/agents/*.md`, `~/.agents/agents/*.md`), as well as decentralized multi-agent swarms:

| Role / Spec | Type | Mode | Description |
|---|---|---|---|
| `default` | Bundled | Primary | Full software engineering tool suite. |
| `okabe` | Bundled | Extended | Experimental persona with advanced toolsets. |
| `architect` | Discovered | General | Systems architect designing components, interfaces, and boundary layers. |
| `build-error-resolver` | Discovered | General | Pinpoints root causes of compiler, build, and typecheck errors. |
| `code-reviewer` | Discovered | Read-Only | Read-only security, correctness, and architecture review. |
| `planner` | Discovered | Read-Only | Generates actionable implementation plans with phased milestones. |
| `security-reviewer` | Discovered | Read-Only | Security auditor auditing OWASP vulnerabilities, authorization, and sanitization. |
| `tdd-guide` | Discovered | General | Test-driven development specialist writing failing reproduction tests first. |

### Swarm Coordination
- **`spawn_teammate`**: Decentralized agent spawning with isolated inboxes and priority actor channels.
- **`TeamTaskBoard`**: DAG-validated task boards preventing circular dependencies and tracking blocked/in-progress statuses.
- **`wait_agent`**: Synchronization barriers supporting completion, message settlement, or timeout-based join.

---

## Core Tools

CoderAI provides a rich, versatile tool surface:

| Tool                                       | Category     | Description                                                                                                                                    |
| ------------------------------------------ | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| **`read`**                                 | File Ops     | Reads files with offset and line limits, image support, and generates anchored `snippet_id` metadata.                                          |
| **`edit`**                                 | File Ops     | Performs precise scoped string replacement anchored to a `snippet_id` with staleness detection.                                                |
| **`write`**                                | File Ops     | Creates new files or atomically overwrites existing files.                                                                                     |
| **`str_replace_editor`**                   | File Ops     | Universal file editor (`view`, `create`, `str_replace`, `insert`, `undo_edit`).                                                                |
| **`glob`**                                 | Search       | Fast file pattern discovery with result caps and disk spilling.                                                                                |
| **`grep`**                                 | Search       | High-performance content search via bundled ripgrep binary.                                                                                    |
| **`bash`**                                 | Process      | Shell execution with timeout enforcement, process group isolation, and background jobs.                                                        |
| **`job_list` / `job_output` / `job_kill`** | Process      | Background job management and streaming output inspection.                                                                                     |
| **`pwsh`**                                 | Process      | Cross-platform PowerShell command execution.                                                                                                   |
| **`terminal_*`**                           | PTY          | Interactive pseudoterminal sessions (`terminal_open`, `terminal_send`, `terminal_read`, `terminal_signal`, `terminal_close`, `terminal_list`). |
| **`subagent` / `subagent_fork`**           | Multi-Agent  | Continuable background subagents and one-shot task delegation.                                                                                 |
| **`send_message` / `interrupt_agent`**     | Multi-Agent  | Inter-agent messaging and subagent control plane.                                                                                              |
| **`spawn_teammate` / `team_task_*`**       | Swarm        | Agent team collaboration with shared task board and synchronization (`wait_agent`).                                                            |
| **`lsp`**                                  | Code Intel   | Language Server Protocol integration for definitions, references, and symbol navigation.                                                       |
| **`goal` / `todo_write`**                  | Planning     | Session goal tracking and structured task breakdowns.                                                                                          |
| **`workflow`**                             | Automation   | Multi-phase asynchronous Python workflow scripting engine.                                                                                     |
| **`ralph`**                                | Verification | Automated test-driven feedback and completion verification harness.                                                                            |
| **`code_mode`**                            | Execution    | Sandboxed in-process Python execution for data manipulation and analysis.                                                                      |
| **`session_query`**                        | Search       | Full-text query and indexing across past conversation sessions.                                                                                |
| **`WebSearch` / `WebFetch`**               | Web          | Live web search and URL fetching with SSRF protection and Markdown conversion.                                                                 |
| **`UnderstandImage`**                      | Media        | Image understanding for local visual assets.                                                                                                   |
| **`AskUserQuestion`**                      | User         | Interactive questionnaires and user decision modals.                                                                                           |
| **`Think`**                                | Reasoning    | Scratch-pad reasoning notes appended to the log (no I/O).                                                                                     |
| **`SendDMail`**                            | Steering     | Inject a directive into the running turn (time-leap steering).                                                                                 |
| **`skill`**                                | Skills       | Loads discovered workspace skills (`SKILL.md`; `/skill:<name>` colon syntax).                                                                  |

---

## Security & Permissions

CoderAI follows a **defense-in-depth, fail-closed** security model.

### Permission Scopes

| Scope            | Description                         | Default Policy |
| ---------------- | ----------------------------------- | -------------- |
| `read-in-cwd`    | Reading files within workspace      | `allow`        |
| `read-out-cwd`   | Reading files outside workspace     | `ask`          |
| `write-in-cwd`   | Writing files within workspace      | `ask`          |
| `write-out-cwd`  | Writing files outside workspace     | `ask`          |
| `delete-in-cwd`  | Deleting files within workspace     | `ask`          |
| `delete-out-cwd` | Deleting files outside workspace    | `deny`         |
| `query-git-log`  | Inspecting git status/log           | `allow`        |
| `mutate-git-log` | Committing or altering git branches | `ask`          |
| `network`        | Web search and outbound HTTP        | `ask`          |
| `mcp`            | MCP tool execution                  | `ask`          |

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
    "ask": [
      "write-in-cwd",
      "write-out-cwd",
      "delete-in-cwd",
      "mutate-git-log",
      "network",
      "mcp"
    ],
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

| Variable                               | Description                                                              |
| -------------------------------------- | ------------------------------------------------------------------------ |
| `OPENAI_API_KEY` / `CODERAI_API_KEY`   | LLM provider API key                                                     |
| `OPENAI_BASE_URL` / `CODERAI_BASE_URL` | API endpoint URL (OpenAI, DeepSeek, Ollama, etc.)                        |
| `CODERAI_MODEL`                        | Default model identifier                                                 |
| `CODERAI_TOOLS_PRESET`                 | Tool preset (`full`, `core`, `shell_edit`)                               |
| `CODERAI_PERMISSION_PRESET`            | Permission preset (`read-only`, `workspace-write`, `danger-full-access`) |
| `CODERAI_THINKING_ENABLED`             | Enable reasoning/thinking tokens (`true` / `false`)                      |
| `CODERAI_REASONING_EFFORT`             | Reasoning effort (`off`, `low`, `medium`, `high`, `max`)                 |
| `CODERAI_RG_PATH`                      | Path to custom ripgrep executable (defaults to bundled binary)           |
| `CODERAI_DEBUG_LOG_ENABLED`            | Enable verbose engine debug logging                                      |

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
python scripts/self_check.py

# Clean build artifacts
make clean
```

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

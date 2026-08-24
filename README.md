# 🤖 CoderAI

<p align="center">
  <strong>Autonomous AI Pair Programming in Your Terminal</strong>
</p>

<p align="center">
  <a href="#key-features">Key Features</a> •
  <a href="#-benchmark--performance">Benchmark & Performance</a> •
  <a href="#quickstart">Quickstart</a> •
  <a href="#interactive-cli--slash-commands">Interactive CLI</a> •
  <a href="#core-tools">Core Tools</a> •
  <a href="docs/ARCHITECTURE.md">Architecture</a> •
  <a href="#security--permissions">Security</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#development">Development</a> •
  <a href="#documentation">Documentation</a>
</p>

---

## Overview

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

> [!IMPORTANT]
> **Executive Summary & Verdict**:
>
> - 🎯 **100.0% Task Resolution (10/10)**: CoderAI achieved a perfect resolution score, outperforming Claude Code (**80.0%**) and OpenCode (**80.0%**).
> - ⚡ **36.0% Faster Mean Execution**: Completed tasks in an average of **73.8s** vs Claude Code's **115.3s** and OpenCode's **86.9s**.
> - 💰 **33.5% Lower Total API Cost**: Incurred only **$0.1069** total API cost across all 10 tasks vs Claude Code's **$0.1602** and OpenCode's **$0.1611**.
> - 🚀 **3.4s Time to First Token (TTFT)**: Streamed initial tokens **3.76x faster** than Claude Code (12.8s) and **17.1x faster** than OpenCode (58.3s).
> - 🧠 **54.2% Fewer Wasted Reasoning Tokens**: Required only 32,338 reasoning tokens compared to 70,599 (Claude Code) and 54,325 (OpenCode).
> - 📈 **95.9% Cache Hit Rate**: Stable prompt prefix structure maximizes LLM KV-cache reuse across turns.

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

### 📈 Visual Benchmark Graphs

#### Success Rate (%) [Higher is Better]

```text
CoderAI      | ████████████████████████████████████████  100.0% (10/10) [PERFECT]
Claude Code  | ████████████████████████████████          80.0%  (8/10)  [2 Failed]
OpenCode     | ████████████████████████████████          80.0%  (8/10)  [2 Failed]
```

#### Average Execution Speed (Seconds) [Lower is Better]

```text
CoderAI      | ▍▍▍▍▍▍▍▍▍▍▍▍▍▍                         73.8s  (Fastest mean turnaround)
OpenCode     | ▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍                       86.9s  (+17.8% slower)
Claude Code  | ▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍▍                 115.3s (+56.2% slower)
```

#### Total API Cost ($ USD) [Lower is Better]

```text
CoderAI      | ▓▓▓▓▓▓▓▓▓▓▓▓                           $0.1069 (33.5% total cost savings)
Claude Code  | ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                     $0.1602
OpenCode     | ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                     $0.1611
```

#### Time to First Token (TTFT in Seconds) [Lower is Better]

```text
CoderAI      | █                                      3.4s   (Instant responsiveness)
Claude Code  | ████                                   12.8s  (3.8x higher latency)
OpenCode     | ██████████████████                     58.3s  (17.1x higher latency)
```

#### Tool Execution Overhead (Total Tool Time in Seconds) [Lower is Better]

```text
CoderAI      | █                                      73.7s  (Native ripgrep & fast I/O)
Claude Code  | ██                                     149.7s (2.0x higher tool latency)
OpenCode     | ████████████                           900.6s (12.2x higher tool latency)
```

---

### 📋 Detailed Per-Task Performance Breakdown

| #      | Task Identifier           | Repository / Domain         |              CoderAI              |            Claude Code            |             OpenCode              | CoderAI Advantage                                                               |
| ------ | ------------------------- | --------------------------- | :-------------------------------: | :-------------------------------: | :-------------------------------: | ------------------------------------------------------------------------------- |
| **1**  | `psf__requests-5414`      | `psf/requests` (SWT-Bench)  | ✅ **PASS**<br>`106.5s · $0.0127` | ❌ **FAIL**<br>`126.8s · $0.0190` | ✅ **PASS**<br>`129.2s · $0.0205` | Solved 22.7s faster with **50% fewer tokens** than OpenCode; Claude failed.     |
| **2**  | `psf__requests-6028`      | `psf/requests` (SWT-Bench)  | ✅ **PASS**<br>`50.0s · $0.0067`  | ✅ **PASS**<br>`236.0s · $0.0299` | ❌ **FAIL**<br>`143.1s · $0.0221` | **4.7x faster** and **77% cheaper** than Claude; OpenCode failed.               |
| **3**  | `pallets__flask-5014`     | `pallets/flask` (SWT-Bench) | ✅ **PASS**<br>`128.6s · $0.0304` | ✅ **PASS**<br>`78.1s · $0.0156`  | ✅ **PASS**<br>`63.9s · $0.0154`  | Thorough contextual exploration on complex Flask routing structures.            |
| **4**  | `sympy__sympy-20590`      | `sympy/sympy` (SWT-Bench)   | ✅ **PASS**<br>`94.6s · $0.0141`  | ❌ **FAIL**<br>`81.2s · $0.0143`  | ✅ **PASS**<br>`103.2s · $0.0208` | Solved with **39% fewer tokens** & 32% lower cost than OpenCode; Claude failed. |
| **5**  | `sympy__sympy-12481`      | `sympy/sympy` (SWT-Bench)   | ✅ **PASS**<br>`144.9s · $0.0250` | ✅ **PASS**<br>`278.6s · $0.0310` | ✅ **PASS**<br>`69.0s · $0.0162`  | Nearly **2x faster** turnaround than Claude Code.                               |
| **6**  | `local__dependency_graph` | Local Concurrency / DAG     | ✅ **PASS**<br>`30.3s · $0.0026`  | ✅ **PASS**<br>`45.9s · $0.0085`  | ✅ **PASS**<br>`84.4s · $0.0149`  | **2.8x faster** than OpenCode, **69% cheaper** than Claude.                     |
| **7**  | `local__job_queue_wal`    | Local WAL / Storage         | ✅ **PASS**<br>`51.4s · $0.0049`  | ✅ **PASS**<br>`118.1s · $0.0133` | ❌ **FAIL**<br>`69.4s · $0.0087`  | **2.3x faster** than Claude; OpenCode failed write-ahead logging verification.  |
| **8**  | `local__lru_cache_ttl`    | Local Data Structures       | ✅ **PASS**<br>`35.7s · $0.0031`  | ✅ **PASS**<br>`50.3s · $0.0085`  | ✅ **PASS**<br>`45.3s · $0.0094`  | Lowest latency (35.7s) and **67% cheaper** than alternatives.                   |
| **9**  | `local__markdown_table`   | Local Parser / Formatting   | ✅ **PASS**<br>`58.0s · $0.0045`  | ✅ **PASS**<br>`77.0s · $0.0101`  | ✅ **PASS**<br>`81.4s · $0.0120`  | **55% lower cost** than Claude Code ($0.0045 vs $0.0101).                       |
| **10** | `local__matrix_spiral`    | Local Algorithms            | ✅ **PASS**<br>`37.5s · $0.0031`  | ✅ **PASS**<br>`61.3s · $0.0100`  | ✅ **PASS**<br>`79.9s · $0.0213`  | **2.1x faster** and **85% cheaper** than OpenCode.                              |

---

### 💡 Key Architectural Insights: Why CoderAI Wins

#### 1. Snippet-Scoped Anchored Editing vs Destructive Full-File Rewrites

Competitors often rely on whole-file replacements or unanchored diffs, leading to hallucinated line offsets, merge conflicts, and massive token wastage. CoderAI's `read` -> `snippet_id` -> `edit` pipeline anchors edits directly to content hashes. This prevented the failure modes seen in Claude Code (`requests-5414`, `sympy-20590`) and OpenCode (`requests-6028`, `job_queue_wal`).

#### 2. Lean Prompt Architecture & Zero-Overhead Token Streaming (3.4s TTFT)

CoderAI structures system prompt sections deterministically and maintains a consistent prefix layout. This yields a **95.9% KV cache hit rate** and allows the model to start streaming tokens in just **3.4 seconds**—compared to 12.8s for Claude Code and 58.3s for OpenCode.

#### 3. Bounded Agent Turns with Intelligent Compaction

CoderAI uses deterministic multi-turn loops with repeat-action guards and token threshold compaction. CoderAI consumed only **32,338 reasoning tokens** across the entire benchmark compared to **70,599** for Claude Code (a 54.2% reduction in wasted reasoning loops).

#### 4. Bundled Ripgrep & Spill-to-Disk Policy

CoderAI executes file discovery and regex searches using native bundled `ripgrep` binaries and spills oversized outputs to disk locators. Total tool execution time was **73.7s**, compared to Claude Code's **149.7s** and OpenCode's **900.6s** (where in-memory subprocess pipes created severe bottlenecks).

---

### 🔬 Reproducing the Benchmark

To run the automated SWT-Bench and Local benchmark suite:
have not included benchmark files in this repo check my other repo

```bash
# Run 10 benchmark tasks comparing CoderAI, Claude Code, and OpenCode
python3 benchmark.py --tasks 10 --harnesses coderai,claude,opencode
```

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

| Command                | Description                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------- |
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
| `/help`, `/?`          | Display categorized interactive command help menu                                     |
| `/exit`, `/quit`       | Exit session and display the exit summary card                                        |

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
| **`skill`**                                | Skills       | Loads discovered workspace skills (`SKILL.md`).                                                                                                |

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
python scripts/self_check_core.py

# Clean build artifacts
make clean
```

---

## Documentation

- [Architecture & System Design](docs/ARCHITECTURE.md)
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

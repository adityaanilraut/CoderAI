# 🤖 CoderAI

> **AI pair programming in your terminal** — clean, UI-agnostic core engine with snippet-scoped editing, bounded agent loops, granular permission gating, and minimal dependencies.

---

## Overview

CoderAI is a production-grade terminal coding assistant architected around **DeepCode's UI-agnostic engine**. It is built on a clean boundary between the execution engine (`coderai/core`) and the interactive presentation layer (`coderai/cli`).

Unlike legacy agent architectures, CoderAI uses:
- **Snippet-scoped editing**: `read` yields a unique `snippet_id` and line window; `edit` operations are strictly anchored to the active snippet to prevent out-of-context hallucinations and stale overwrites.
- **Bounded agent loop**: Deterministic turn execution (`stream → tool_calls → permissions → execute → loop`) with loop-guards and token-threshold compaction.
- **Side-effect permissions**: Granular security scopes (`read-in-cwd`, `write-in-cwd`, `mutate-git-log`, `network`, `delete-out-cwd`, etc.) evaluated per tool call before execution.
- **Session persistence**: JSONL event streams and index metadata persisted per project under `~/.coderai/projects/<project-code>/`.
- **Dynamic MCP client**: Native stdio Model Context Protocol (MCP) server support for custom tools.
- **Minimal dependencies**: Pruned to `rich`, `openai`, `requests` and strict core requirements.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI

# Install in editable mode
pip install -e .

# Verify installation
coderai --version
```

---

## Quickstart

### 1. Configure Model & API Key

Set your API key via environment variable:
```bash
export OPENAI_API_KEY="sk-..."
# Optional custom base URL (e.g. OpenRouter, Ollama, DeepSeek, local LLMs)
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

Or configure via `~/.coderai/settings.json` or `.coderai/settings.json`:
```json
{
  "model": "gpt-4o",
  "permissions": {
    "defaultMode": "allowAll",
    "allow": ["read-in-cwd"],
    "ask": ["write-in-cwd", "mutate-git-log"],
    "deny": ["network"]
  },
  "mcpServers": {
    "filesystem-extra": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
```

### 2. Run Interactive REPL

```bash
coderai
```

### 3. Non-Interactive / One-Shot Command

```bash
coderai "Refactor the login handler to support async/await"
```

---

## CLI Options

```text
usage: coderai [-h] [--model MODEL] [--message MESSAGE] [--resume RESUME]
               [--yes] [--verbose] [--version]
               [prompt ...]

coderai — AI pair programming in your terminal.

positional arguments:
  prompt                initial prompt (non-interactive when provided)

options:
  -h, --help            show this help message and exit
  --model MODEL, -m MODEL
                        LLM model to use (e.g., gpt-4o, claude-3-5-sonnet)
  --message MESSAGE     send a single message and exit
  --resume RESUME       resume an existing session by ID
  --yes, -y             auto-approve all tool execution permissions
  --verbose, -v         print debug information
  --version             show program's version number and exit
```

---

## Interactive Slash Commands

Inside the `coderai>` REPL:

| Command | Description |
|---|---|
| `/help`, `/?` | Display available commands |
| `/sessions` | List recent saved sessions with status and summaries |
| `/resume <id>` | Resume a previous session by session ID |
| `/new` | Start a fresh session in the current project |
| `/skills` | List bundled and project-specific skills |
| `/undo` | Revert to previous user prompt checkpoint |
| `/exit`, `/quit` | Exit the CLI |

---

## Core Tools

CoderAI provides a focused, high-leverage built-in tool suite:

* **`read`**: Reads files with line offsets, limits, image support, and generates `snippet_id` anchors for subsequent edits.
* **`edit`**: Scoped string replacement requiring a valid `snippet_id`, exact match verification, tab stripping, `replace_all` guards, and LLM diagnostic/correction fallback.
* **`write`**: Creates new files or overwrites existing files.
* **`bash`**: Executes shell commands with persistent working directory tracking, timeout enforcement, permission checks, and background process execution (`run_in_background`).
* **`AskUserQuestion`**: Prompts the user interactively when ambiguity or design decisions arise.
* **`UpdatePlan`**: Maintains and updates a live Markdown implementation task checklist.
* **`WebSearch`**: Performs web queries for up-to-date documentation and references.
* **`UnderstandImage`**: Analyzes local PNG/JPEG/WebP images for non-multimodal models.

---

## Architecture

```
coderai/
  main.py                     # Console entry point -> coderai.cli.app.main
  cli/
    app.py                    # Terminal UI, REPL, argument parsing, permission prompts
  core/                       # UI-agnostic engine (no UI dependencies)
    session.py                # SessionManager (bounded agent loop, JSONL persistence, undo)
    prompt.py                 # System prompt, tools schema, runtime context, skills
    permissions.py            # Side-effect scopes (allow/deny/ask, permission computation)
    settings.py               # Settings resolver (user + project + environment)
    state.py                  # Snippet manager, file versioning, state rebuild
    openai_client.py          # OpenAI client factory and configuration
    tools/                    # Built-in tool handlers (read, edit, write, bash, etc.)
    mcp/                      # Stdio MCP client and dynamic tool manager
    common/                   # File utilities, shell helpers, process management, GitFileHistory
```

---

## Development & Verification

Run the test and verification suite:

```bash
# Lint checks
python -m ruff check coderai/ tests/ scripts/

# Type validation
python -m mypy coderai/

# Unit tests
python -m pytest -q

# Offline engine self-check
python scripts/self_check_core.py
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.

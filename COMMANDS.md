# CoderAI Commands Reference

Complete reference for CLI commands and interactive slash commands.

## Table of Contents
- [CLI Commands](#cli-commands)
- [Interactive Slash Commands](#interactive-slash-commands)
- [Configuration Keys](#configuration-keys)
- [Environment Variables](#environment-variables)
- [Tool Quick Reference](#tool-quick-reference)

---

## CLI Commands

Run from your terminal as `coderAI <command>`.

### `coderAI` / `coderAI chat`
Start an interactive chat session in the Ink UI.

```bash
coderAI
coderAI chat

# Specific model
coderAI chat -m claude-4-sonnet
coderAI chat -m opus
coderAI chat -m gpt-5.4-mini

# Resume a previous session
coderAI chat --resume <session-id>

# Skip tool confirmation prompts (use with care)
coderAI chat --auto-approve
coderAI chat --yolo   # alias
```

On first run, downloads the prebuilt Ink UI binary for your platform and caches it in `~/.coderAI/bin/`. Set `$CODERAI_UI_BINARY` to use a local binary instead.

---

### `coderAI models`
List all available models and providers.

```bash
coderAI models
```

---

### `coderAI config`
Manage configuration.

```bash
# Show all settings (API keys are masked)
coderAI config show

# Set a value
coderAI config set default_model claude-4-sonnet
coderAI config set temperature 0.5
coderAI config set budget_limit 5.0

# Reset to defaults
coderAI config reset
```

---

### `coderAI history`
Manage conversation sessions.

```bash
coderAI history list             # List all past sessions
coderAI history delete <id>      # Delete a specific session
coderAI history clear            # Delete all sessions (asks for confirmation)
```

---

### `coderAI status`
Print system diagnostics — API key status, default model, config directory, session count.

```bash
coderAI status
```

---

### `coderAI cost`
Show per-model pricing and current session cost.

```bash
coderAI cost
```

---

### `coderAI tasks`
List in-progress tasks tracked by the agent.

```bash
coderAI tasks list
```

---

### `coderAI info`
Show agent info: version, current model, registered tools.

```bash
coderAI info
```

---

### `coderAI setup`
Run the interactive setup wizard to configure API keys and defaults.

```bash
coderAI setup
```

---

## Interactive Slash Commands

These commands are typed inside an active `coderAI chat` session.

| Command | Description |
|---|---|
| `/help` | Show all available slash commands |
| `/model <name>` | Switch the LLM model for the current session |
| `/tokens` | Show token usage and estimated cost for the session |
| `/context` | List files currently pinned to the context window |
| `/compact` | Force-compress conversation history to reclaim context space |
| `/agents` | Show all active agents (main + any sub-agents) and their status |
| `/auto-approve` | Toggle auto-approval of tool confirmations on/off |
| `/clear` | Clear the conversation history and start fresh |
| `/exit` | End the session |

**Reference-only slash commands** (output rendered inline, no side effects):

| Command | Description |
|---|---|
| `/models` | List available models |
| `/cost` | Show pricing and session cost |
| `/status` | Show session and agent status |
| `/info` | Show agent and tool info |
| `/tasks` | Show the task list |
| `/config show` | Show current configuration |

---

## Configuration Keys

Stored in `~/.coderAI/config.json`. Set via `coderAI config set <key> <value>` or environment variables.

| Key | Default | Description |
|---|---|---|
| `default_model` | `claude-4-sonnet` | Default LLM model for new sessions |
| `temperature` | `0.7` | Sampling temperature (0.0–2.0) |
| `max_tokens` | `4096` | Max output tokens per LLM response |
| `context_window` | `128000` | Token budget for the context window |
| `max_iterations` | `50` | Max agentic loop iterations per message |
| `reasoning_effort` | `medium` | Reasoning depth — `high`, `medium`, `low`, `none` |
| `streaming` | `true` | Enable streaming token output |
| `save_history` | `true` | Persist sessions to `~/.coderAI/history/` |
| `budget_limit` | `0` | Max USD per session (`0` = unlimited) |
| `max_file_size` | `1048576` | Max file size readable by `read_file` (bytes) |
| `max_glob_results` | `200` | Max results returned by `glob_search` |
| `max_command_output` | `10000` | Max characters captured from `run_command` output |
| `max_tool_output` | `8000` | Max characters of any tool result kept in context |
| `web_tools_in_main` | `true` | Allow web tools (`web_search`, `read_url`, `http_request`, `download_file`) in the main agent |
| `log_level` | `WARNING` | Log verbosity — `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `lmstudio_endpoint` | `http://localhost:1234/v1` | LM Studio API endpoint |
| `lmstudio_model` | `local-model` | LM Studio model name |
| `ollama_endpoint` | `http://localhost:11434/v1` | Ollama API endpoint |
| `ollama_model` | `llama3` | Ollama model name |

**Project-level overrides** (`.coderAI/config.json` in the project root) accept a subset of the above keys — everything except API keys.

---

## Environment Variables

Environment variables take precedence over `~/.coderAI/config.json`.

| Variable | Maps to config key |
|---|---|
| `ANTHROPIC_API_KEY` | `anthropic_api_key` |
| `OPENAI_API_KEY` | `openai_api_key` |
| `GROQ_API_KEY` | `groq_api_key` |
| `DEEPSEEK_API_KEY` | `deepseek_api_key` |
| `CODERAI_DEFAULT_MODEL` | `default_model` |
| `CODERAI_TEMPERATURE` | `temperature` |
| `CODERAI_MAX_TOKENS` | `max_tokens` |
| `CODERAI_REASONING_EFFORT` | `reasoning_effort` |
| `CODERAI_MAX_ITERATIONS` | `max_iterations` |
| `CODERAI_MAX_TOOL_OUTPUT` | `max_tool_output` |
| `CODERAI_BUDGET_LIMIT` | `budget_limit` |
| `CODERAI_LOG_LEVEL` | `log_level` |
| `CODERAI_PROJECT_INSTRUCTION_FILE` | `project_instruction_file` |
| `LMSTUDIO_ENDPOINT` | `lmstudio_endpoint` |
| `OLLAMA_ENDPOINT` | `ollama_endpoint` |
| `CODERAI_UI_BINARY` | Override path to the Ink UI binary |
| `CODERAI_MODEL` | Model override for the IPC entry point |
| `CODERAI_RESUME` | Session ID to resume (IPC entry point) |
| `CODERAI_AUTO_APPROVE` | `"1"` to skip all tool confirmations |
| `CODERAI_ALLOW_LOCAL_URLS` | `"1"` to allow SSRF-protected web tools to reach localhost |

---

## Tool Quick Reference

All 54+ tools available to the agent. Confirmation required (`✓`) means the agent asks before running.

### Filesystem

| Tool | Confirm | Description |
|---|---|---|
| `read_file` | — | Read file contents (optional line range) |
| `write_file` | ✓ | Create or overwrite a file |
| `search_replace` | ✓ | Find-and-replace with verification |
| `apply_diff` | ✓ | Apply a unified diff patch |
| `list_directory` | — | List directory contents |
| `glob_search` | — | Find files by glob pattern |
| `move_file` | ✓ | Move or rename a file/directory |
| `copy_file` | ✓ | Copy a file or directory tree |
| `delete_file` | ✓ | Delete a file or directory |
| `create_directory` | — | Create directories (like `mkdir -p`) |

### Terminal

| Tool | Confirm | Description |
|---|---|---|
| `run_command` | ✓ | Execute a shell command |
| `run_background` | ✓ | Start a background process |
| `list_processes` | — | List tracked background processes |
| `kill_process` | ✓ | Terminate a process by PID |

### Git

| Tool | Confirm | Description |
|---|---|---|
| `git_add` | ✓ | Stage specific files |
| `git_status` | — | Working tree status |
| `git_diff` | — | View diffs |
| `git_commit` | ✓ | Create a commit |
| `git_log` | — | View commit history |
| `git_branch` | ✓ | List/create/delete branches |
| `git_checkout` | ✓ | Switch or create branches |
| `git_stash` | ✓ | Stash/restore changes |
| `git_push` | ✓ | Push to remote (`--force-with-lease`) |
| `git_pull` | ✓ | Fetch and merge/rebase from remote |
| `git_merge` | ✓ | Merge a branch |
| `git_rebase` | ✓ | Rebase; supports `--abort`/`--continue` |
| `git_revert` | ✓ | Create a revert commit |
| `git_reset` | ✓ | Reset HEAD (soft/mixed/hard) |
| `git_show` | — | Show commit details and diff |
| `git_remote` | ✓ | Manage remotes |
| `git_blame` | — | Annotate lines with commit/author |
| `git_cherry_pick` | ✓ | Apply specific commits |
| `git_tag` | ✓ | List/create/delete tags |

### Search

| Tool | Confirm | Description |
|---|---|---|
| `text_search` | — | Fast recursive text search |
| `grep` | — | Regex search with context |

### Web & HTTP

| Tool | Confirm | Description |
|---|---|---|
| `web_search` | — | DuckDuckGo search |
| `read_url` | — | Fetch a URL and return text |
| `download_file` | — | Download a file from a URL |
| `http_request` | — | Generic HTTP client (any method, headers, body) |

### Memory

| Tool | Confirm | Description |
|---|---|---|
| `save_memory` | — | Store a key-value pair persistently |
| `recall_memory` | — | Retrieve or search memories |
| `delete_memory` | ✓ | Delete a memory entry |

### Code Quality

| Tool | Confirm | Description |
|---|---|---|
| `lint` | — | Auto-detect and run linter |
| `format` | ✓ | Auto-detect and run formatter |

### Project, Context & Tasks

| Tool | Confirm | Description |
|---|---|---|
| `project_context` | — | Auto-detect project type and structure |
| `manage_context` | — | Pin/unpin files from the context window |
| `manage_tasks` | — | Persistent TODO list management |

### Multi-Agent & Collaboration

| Tool | Confirm | Description |
|---|---|---|
| `delegate_task` | ✓ | Spawn an isolated sub-agent |
| `notepad` | — | Shared inter-agent notepad |

### Execution & Planning

| Tool | Confirm | Description |
|---|---|---|
| `python_repl` | ✓ | Run Python code in an isolated subprocess |
| `plan` | — | Structured multi-step execution plans |
| `use_skill` | — | Load a skill workflow |

### Vision

| Tool | Confirm | Description |
|---|---|---|
| `read_image` | — | Read and encode an image for analysis |

### MCP

| Tool | Confirm | Description |
|---|---|---|
| `mcp_connect` | ✓ | Connect to an external MCP server |
| `mcp_call_tool` | ✓ | Call a tool on a connected server |
| `mcp_list` | — | List connected servers and tools |

### Undo

| Tool | Confirm | Description |
|---|---|---|
| `undo` | ✓ | Revert the last file modification |
| `undo_history` | — | View recent file change history |

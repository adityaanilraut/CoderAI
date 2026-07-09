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

### Global options

These apply when invoking the root command without a subcommand (equivalent to `coderAI chat`), or as documented on specific commands:

| Option | Description |
|--------|-------------|
| `-v`, `--version` | Print version and exit (not verbose logging) |
| `--verbose` | Enable debug logging |
| `-m`, `--model` | Model override for this session |
| `-r`, `--resume` | Resume a session by ID |
| `--continue` | Resume the most recently updated session |

---

### `coderAI` / `coderAI chat`
Start an interactive chat session in the Textual TUI.

```bash
coderAI
coderAI chat

# Specific model
coderAI chat -m claude-4-sonnet
coderAI chat -m opus
coderAI chat -m gpt-5.4-mini

# Resume a previous session
coderAI chat --resume <session-id>

# Resume the latest session
coderAI chat --continue
coderAI chat --continue-session   # alias

# Load a persona at startup (.coderAI/agents/<name>.md)
coderAI chat --persona code-reviewer
coderAI chat -p architect

# Skip tool confirmation prompts (use with care)
coderAI chat --auto-approve
coderAI chat --yolo   # alias
```

Requires the `textual` package (installed with `pip install coderAI`). No separate UI binary download.

---

### `coderAI run`
Run a single prompt non-interactively and exit — no Textual TUI. For CI, scripting, git hooks, piping, and evals. Drives the same `Agent`/`ExecutionLoop` core as `chat`, but with no UIBridge and no streaming, so stdout receives one clean final answer (or `--json`).

```bash
# Prompt as an argument
coderAI run "refactor utils.py to use pathlib"

# Prompt piped via stdin (or the explicit "-" sentinel)
echo "list the open TODOs" | coderAI run
coderAI run -

# Structured result (response, success, session_id, model, cost_usd, blocked_tools)
coderAI run --json "what is 2+2"

# Resume prior context
coderAI run --resume <session-id> "continue where we left off"
coderAI run --continue "and now add tests"

# Allow mutating tools (see deny-on-mutate below)
coderAI run --yolo "fix the failing test and commit"
coderAI run --auto-approve "..."   # alias for --yolo

# Other options
coderAI run -m opus "..."               # model override
coderAI run -p code-reviewer "..."      # load a persona
coderAI run --max-iterations 10 "..."   # cap the agent loop
coderAI run --trust-workspace "..."     # CI: trust this repo's .coderAI hooks/config (DANGEROUS)
```

**Deny-on-mutate (default).** With no TTY to confirm mutations, a run that needs a mutating tool (e.g. `write_file`, `run_command`, `git_push`) is blocked cleanly instead of prompting: the tool call is denied, the run exits non-zero, and stderr prints which tools were blocked (`--json` lists them under `blocked_tools`). Pass `--yolo`/`--auto-approve` to allow mutations.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success — completed without a blocked mutation |
| `1` | A mutating tool was blocked (non-yolo), missing API key, interrupted, or an agent/runtime error |
| `2` | Usage error — no prompt provided, or both `--resume` and `--continue` given |

---

### `coderAI mcp`
Manage MCP (Model Context Protocol) servers written to `~/.coderAI/mcp_servers.json` — the same file the setup wizard writes and that `coderAI chat` auto-connects on startup. Servers added here become available the next time you start a chat.

```bash
coderAI mcp list                                   # Show configured servers (+ auth status)
coderAI mcp add fetch -- npx -y @scope/server      # stdio launcher after '--'
coderAI mcp add remote --transport sse -- https://example.com/sse
coderAI mcp add api --http https://host/mcp -H "Authorization: Bearer TOKEN"
coderAI mcp remove <name>                          # Remove a server (and its saved creds)
coderAI mcp login <name>                           # OAuth login for an HTTP server (opens browser)
coderAI mcp logout <name>                          # Revoke + delete saved OAuth credentials
coderAI mcp resources <name>                        # List resources exposed by a server
coderAI mcp prompts <name>                          # List prompt templates exposed by a server
```

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

### `coderAI doctor`
Run a post-install health check: Python version, config directory writability, default model, API keys (masked), Textual availability, and history directory.

```bash
coderAI doctor
```

Exit code `1` if any check fails; `0` if all pass (warnings alone do not fail). Recommended after `coderAI setup`.

---

### `coderAI cost`
Show per-model pricing and configured budget limit. Live session cost is tracked inside chat (`/tokens`).

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

### `coderAI index`
Build or update the semantic code search index.

```bash
coderAI index                    # Index the whole project
coderAI index -p src/ -p lib/    # Index specific paths
coderAI index --force            # Re-index everything (ignore cache)
```

---

### `coderAI search`
Search the codebase with natural language.

```bash
coderAI search "authentication middleware"
coderAI search "rate limiting" -f "*.py" -n 20
```

Requires the index to be built first (`coderAI index`).

---

### `coderAI set-model`
Set the default model for new sessions.

```bash
coderAI set-model sonnet
coderAI set-model gpt-5.4-mini
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
| `/pin <path>` | Pin a file to context |
| `/unpin <path>` | Unpin a file from context |
| `/compact` | Force-compress conversation history to reclaim context space |
| `/agents` | Show all active agents (main + any sub-agents) and their status |
| `/persona [name\|default\|list]` | List, apply, or clear an agent persona |
| `/skills` | List available project skill workflows |
| `/reasoning <high\|medium\|low\|none>` | Set thinking budget for reasoning models |
| `/yolo` | Toggle auto-approve for high-risk tools |
| `/verbose` | Toggle verbose mode (show all tool outputs) |
| `/show <topic>` | Show reference info (e.g. `/show plan`, `/show config`, `/show cost`) |
| `/copy` | Copy the last assistant response to clipboard (via OSC-52) |
| `/code-search <query>` | Search the codebase semantically and view results inline |
| `/think` | Toggle thinking/reasoning display |
| `/clear` | Clear the conversation history and start fresh |
| `/allow-tool <tool-name> [scope]` | Always allow a tool this session (high-risk tools need a scope) |
| `/disallow-tool <tool-name>` | Remove a per-session tool allowlist entry |
| `/allowed-tools` | List tools already allowlisted this session |
| `/undo` | Undo last tool action |
| `/rewind <n> [--files]` | Rewind conversation to a past turn |
| `/mcp <name>` | List MCP servers or toggle one on/off |
| `/plan` | Show current execution plan in the right panel |
| `/export` | Export session to markdown |
| `/search <query>` | Search conversation transcript |
| `/retry` | Restart the agent after a crash |
| `/resume [id]` | Resume a saved session |
| `/kill <id-or-name>` | Cancel a sub-agent |
| `/init` | Scaffold `.coderai/` directory in the current project root |
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
| `max_tokens` | `8192` | Max output tokens per LLM response |
| `context_window` | `128000` | Token budget for the context window |
| `max_iterations` | `50` | Max agentic loop iterations per message |
| `reasoning_effort` | `medium` | Reasoning depth — `high`, `medium`, `low`, `none` |
| `streaming` | `true` | Enable streaming token output |
| `save_history` | `true` | Persist sessions to `~/.coderAI/history/` |
| `tui_notifications` | `true` | Ring terminal bell + emit OSC 9 notification when terminal is unfocused and needs attention |
| `budget_limit` | `0` | Max USD per session (`0` = unlimited) |
| `max_file_size` | `1048576` | Max file size readable by `read_file` (bytes) |
| `max_glob_results` | `200` | Max results returned by `glob_search` |
| `max_command_output` | `10000` | Max characters captured from `run_command` output |
| `max_tool_output` | `8000` | Max characters of any tool result kept in context |
| `web_tools_in_main` | `true` | Allow web tools in the main agent (`web_search`, `read_url`, `http_request`, `download_file`, `wikipedia_search`, `read_feed`, `sitemap_discover`) |
| `gemini_api_key` | — | Google Gemini API key (or set `GEMINI_API_KEY`) |
| `browser_headless` | `true` | Run Playwright browser in headless mode |
| `browser_timeout` | `30.0` | Browser operation timeout (seconds) |
| `browser_allowed_domains` | — | Comma-separated domain allowlist (blank = all allowed) |
| `approval_timeout_seconds` | `300` | Seconds before approval prompts auto-deny (0 = wait forever) |
| `project_root` | `.` | Project root directory path |
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
| `GEMINI_API_KEY` | `gemini_api_key` |
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
| `CODERAI_TUI_NOTIFICATIONS` | `tui_notifications` |
| `CODERAI_THEME` | `dark` or `light` for the Textual chat UI |
| `CODERAI_MODEL` | Model override for the IPC entry point |
| `CODERAI_RESUME` | Session ID to resume (IPC entry point) |
| `CODERAI_AUTO_APPROVE` | `"1"` to skip all tool confirmations |
| `CODERAI_ALLOW_LOCAL_URLS` | `"1"` to allow SSRF-protected web tools to reach localhost |
| `CODERAI_ALLOW_OUTSIDE_PROJECT` | `"1"` to allow file/terminal/refactor tools outside the project root |

---

## Tool Quick Reference

All **91 tools** available to the agent when optional dependencies are installed (90 auto-discovered plus `manage_context`). Browser tools require `pip install coderAI[browser]`; PDF extraction in `read_url` requires `pip install coderAI[web]`; desktop tools are macOS-only. Batch edits use `search_replace` with an `edits` list. Confirmation required (`✓`) means the agent asks before running.

### Filesystem

| Tool | Confirm | Description |
|---|---|---|
| `read_file` | — | Read file contents (optional line range) |
| `write_file` | ✓ | Create or overwrite a file |
| `search_replace` | ✓ | Find-and-replace with verification (batch via `edits`) |
| `apply_diff` | ✓ | Apply a unified diff patch |
| `list_directory` | — | List directory contents |
| `glob_search` | — | Find files by glob pattern |
| `move_file` | ✓ | Move or rename a file/directory |
| `copy_file` | ✓ | Copy a file or directory tree |
| `delete_file` | ✓ | Delete a file or directory |
| `create_directory` | ✓ | Create directories (like `mkdir -p`) |
| `file_stat` | — | Get file metadata (size, permissions, mtime) |
| `file_chmod` | ✓ | Change file permissions |
| `file_chown` | ✓ | Change file ownership |
| `file_readlink` | — | Read symlink targets |

### Terminal

| Tool | Confirm | Description |
|---|---|---|
| `run_command` | ✓ | Execute a shell command |
| `run_background` | ✓ | Start a background process |
| `list_processes` | — | List tracked background processes |
| `kill_process` | ✓ | Terminate a process by PID |
| `read_bg_output` | — | Read buffered output from a `run_background` process |

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
| `git_fetch` | ✓ | Fetch objects and refs from a remote |

### Search

| Tool | Confirm | Description |
|---|---|---|
| `text_search` | — | Fast recursive text search |
| `grep` | — | Regex search with context |
| `symbol_search` | — | Find function/class/variable definitions by name |
| `semantic_search` | — | Natural-language code search via embeddings |

### Web & HTTP

| Tool | Confirm | Description |
|---|---|---|
| `web_search` | — | Web search (DuckDuckGo and other backends) |
| `read_url` | — | Fetch a URL and return text (PDF with optional `pypdf`) |
| `download_file` | ✓ | Download a file from a URL |
| `http_request` | ✓ | Generic HTTP client (any method, headers, body) |
| `wikipedia_search` | — | Search Wikipedia and return article summaries |
| `read_feed` | — | Parse RSS/Atom feeds from a URL |
| `sitemap_discover` | — | Discover pages via `sitemap.xml` / `robots.txt` |

### Memory

| Tool | Confirm | Description |
|---|---|---|
| `save_memory` | — | Store a key-value pair persistently |
| `recall_memory` | — | Retrieve or search memories |
| `delete_memory` | ✓ | Delete a memory entry |

### Code Quality

| Tool | Confirm | Description |
|---|---|---|
| `lint` | ✓ | Auto-detect and run linter |
| `format` | ✓ | Auto-detect and run formatter |
| `run_tests` | ✓ | Auto-detect and run project tests |

### Refactoring

| Tool | Confirm | Description |
|---|---|---|
| `refactor` | ✓ | Cross-file `rename_symbol` or `find_references` (writes via `write_file` pipeline; use `dry_run=true` first) |

### Package Management

| Tool | Confirm | Description |
|---|---|---|
| `package_manager` | ✓ | Install, remove, or list packages (pip, npm, cargo, …) |

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
| `mcp_disconnect` | ✓ | Disconnect from an MCP server |
| `mcp_call_tool` | ✓ | Call a tool on a connected server |
| `mcp_list` | — | List connected servers, tools, resources, and prompts |
| `mcp_list_resources` | — | List resources exposed by a connected server |
| `mcp_read_resource` | — | Read a resource (by URI) from a connected server |
| `mcp_list_prompts` | — | List prompt templates exposed by a connected server |
| `mcp_get_prompt` | — | Fetch a prompt template (with arguments) from a server |

### Browser Automation

*Requires `pip install coderAI[browser]` and `playwright install chromium`.*

| Tool | Confirm | Description |
|---|---|---|
| `browser_navigate` | — | Navigate to a URL |
| `browser_snapshot` | — | Capture accessibility tree with element refs |
| `browser_click` | ✓ | Click an element by snapshot ref |
| `browser_type` | ✓ | Type into an input by ref |
| `browser_select_option` | ✓ | Select a dropdown option by ref |
| `browser_get_content` | — | Extract page content (markdown, text, or HTML) |
| `browser_screenshot` | — | Take a PNG screenshot |
| `browser_evaluate` | ✓ | Execute JavaScript in the page |
| `browser_wait` | — | Wait for text or a timeout |
| `browser_close` | — | Close the browser session |

### Desktop Automation (macOS only)

| Tool | Confirm | Description |
|---|---|---|
| `run_applescript` | ✓ | Execute AppleScript or JXA |
| `get_accessibility_tree` | — | Retrieve the macOS accessibility UI tree |
| `click_ui_element` | ✓ | Click a UI element via System Events |
| `type_keystrokes` | ✓ | Simulate typing or key presses |

### Undo

| Tool | Confirm | Description |
|---|---|---|
| `undo` | ✓ | Revert the last file modification |
| `undo_history` | — | View recent file change history |

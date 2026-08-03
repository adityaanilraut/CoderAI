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

Requires the `textual` package (installed with `pip install coderai-agent`). No separate UI binary download.

---

### `coderAI run`
Run a single prompt non-interactively and exit — no Textual TUI. For CI, scripting, git hooks, piping, and evals. Drives the same `Agent`/`ExecutionLoop` core as `chat`. Stdout contains only the selected output format; diagnostics are written to stderr.

```bash
# Prompt as an argument
coderAI run "refactor utils.py to use pathlib"

# Prompt piped via stdin (or the explicit "-" sentinel)
echo "list the open TODOs" | coderAI run
coderAI run -

# Structured result (response, success, session_id, model, cost_usd, blocked_tools)
coderAI run --json "what is 2+2"
coderAI run --output json "what is 2+2"   # equivalent output; --json remains supported

# Ordered event stream for CI and editor integrations
coderAI run --output ndjson "inspect the failing tests"

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

`--output` accepts `text`, `json`, or `ndjson`. NDJSON writes one JSON object per stdout line with `schema_version: 1`, a monotonic `sequence`, `timestamp`, `type`, `terminal`, and `data`. It forwards actual lifecycle, tool, warning/error, progress, `capability.routing`, and `capability.first_useful_action` events from the core. The routing payload contains `schema_token_cost`, `selected_capabilities`, `routing_reason`, `selection_success`, and `plan_mode`. The first-useful-action payload contains only `tool_name` and `elapsed_ms`; it never includes the objective, tool arguments, secrets, or result content. Assistant delta events are present only when the configured provider path streams; nonstreaming providers are represented by the terminal result without synthetic deltas. Every run ends with exactly one terminal `result` or `error` envelope.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success — completed without a blocked mutation |
| `1` | A mutating tool was blocked (non-yolo), missing API key, interrupted, or an agent/runtime error |
| `2` | Usage error — no prompt provided, or both `--resume` and `--continue` given |

---

### Offline capability-routing evaluation

Contributors can run the deterministic routing corpus without a provider or
network connection:

```bash
python -m coderAI.evals.capability_routing
python -m coderAI.evals.capability_routing --json
```

The checked command exits non-zero unless all cases pass, conservative fallback
is perfect, false-positive and false-negative counts are zero, and aggregate
schema-token savings against the full eligible-registry baselines remain at
least 50%. `--no-check` prints the same report without failing the process.

---

### `coderAI plan`

Run the complete Plan Mode lifecycle without Textual. State is project-scoped
under `.coderAI/plans/`; every command supports deterministic process exit and
the state-changing/review commands support `--json` where shown.

```bash
# Enforced read-only exploration; returns the plan ID, revision, questions,
# editable artifact path, and durable histories.
coderAI plan create --json "add parser validation"

# Review current state or print the mutable validated artifact path.
coderAI plan show --json
coderAI plan edit

# Edit draft.json in any editor, then validate it into a new immutable revision.
coderAI plan apply --json
coderAI plan apply --file .coderAI/review/parser-plan.json --json

# Set or replace one stable structured answer without another LLM call.
coderAI plan answer storage SQLite --json

# Approval and execution are separate for CI review gates.
coderAI plan approve --json
coderAI plan execute --auto-approve --json

# Continue a paused or executing record after denial/cancellation/interruption.
coderAI plan execute --resume <session-id> --auto-approve --json
```

`create` exposes only read tools, read-only delegation, and `submit_plan`; the
executor independently rejects invented mutations. `approve` refuses plans
with unanswered questions, stale/invalid artifacts, or unapplied draft edits.
`execute` accepts only `approved`, `paused`, or interrupted `executing` records, verifies
the approved snapshot hash, and defaults to deny-on-mutate. A denied or
cancelled attempt becomes `paused` without losing approval and requires an
explicit execute/resume command before mutation can continue. If the agent calls
`request_plan_amendment`, execution returns non-zero with a new draft revision
that must be reviewed and approved.

---

### `coderAI mcp`
Manage MCP (Model Context Protocol) servers. Config scopes:

| Scope | Path |
|-------|------|
| **user** (default) | `~/.coderAI/mcp_servers.json` |
| **project** | `.mcp.json` at the project root (Claude/Cursor-compatible) |
| **local** | `~/.coderAI/mcp_local.json` keyed by project root |

Merge precedence (same name): local → project → user → bundled (`git_extended`).
Project servers require a trusted workspace and `coderAI mcp approve <name>` (or `/mcp <name>` in chat) before autoconnect.

Per-server fields: `transport` (`stdio`/`sse`/`http`), `command`/`args`, `url`/`headers`, `env`, `cwd`, `timeout` (ms), `disabled`. String fields support `${VAR}` / `${VAR:-default}` expansion at connect time. Stdio `env` is applied **after** secret scrubbing (only listed keys).

```bash
coderAI mcp list                                   # Scope, status, auth
coderAI mcp catalog                                # Curated presets
coderAI mcp add                                    # Interactive wizard
coderAI mcp add --preset github                    # Preset → user scope
coderAI mcp add fetch --scope project -- npx -y @modelcontextprotocol/server-fetch
coderAI mcp add gh -e GITHUB_PERSONAL_ACCESS_TOKEN='${GITHUB_TOKEN}' -- npx -y @modelcontextprotocol/server-github
coderAI mcp add api --http https://host/mcp -H "Authorization: Bearer TOKEN"
coderAI mcp get <name>                             # Resolved entry + meta
coderAI mcp enable|disable <name>
coderAI mcp approve|reject <name>                  # Project .mcp.json trust
coderAI mcp remove <name> [--scope user|project|local]
coderAI mcp import --from cursor|claude-desktop|claude-code|file --path …
coderAI mcp debug <name>                           # One-shot connect diagnostics
coderAI mcp login <name>                           # OAuth for HTTP servers
coderAI mcp logout <name>
coderAI mcp resources <name>
coderAI mcp prompts <name>
```

In chat: `/mcp` lists/toggles servers; `/mcp__<server>__<prompt>` loads an MCP prompt template.

---

### `coderAI skills`
Install and manage skill workflows (Claude Code–style). Scopes:

| Scope | Path |
|-------|------|
| **project** (default) | `.coderAI/skills/<name>/SKILLS.md` |
| **user** | `~/.coderAI/skills/<name>/SKILLS.md` |
| **builtin** (read-only) | Installed `coderAI/assets/skills/<name>/SKILLS.md` |

Sources: local path, `owner/repo`, `owner/repo/path`, or a GitHub URL. Ecosystem
`SKILL.md` files are accepted and normalized to `SKILLS.md` on install. Runtime
resolution is project → user → builtin. Project skills require workspace trust;
user and packaged built-in skills are always available. Built-ins cannot be
removed with `coderAI skills remove`.

```bash
coderAI skills install ./my-skill
coderAI skills install owner/repo
coderAI skills install owner/repo/skills/foo --scope user
coderAI skills install https://github.com/owner/repo --path skills/bar
coderAI skills install owner/repo --list              # Discover without installing
coderAI skills install owner/repo --only foo --force
coderAI skills list [--scope all|project|user]
coderAI skills remove <name> [--scope project|user] [-y]
```

In chat: `/skills` lists each workflow with its source scope; the agent loads it
via `use_skill`.

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
coderAI history list --tag audit # Filter by tag
coderAI history list --filter ci # Filter by ID, name, or tag text
coderAI history rename <id> "Release audit"
coderAI history rename <id> --clear
coderAI history tag <id> audit ci
coderAI history tag --remove <id> audit
coderAI history tag --clear <id>
coderAI history export <id> --format markdown
coderAI history export <id> --format json
coderAI history delete <id>      # Delete a specific session
coderAI history clear            # Delete all sessions (asks for confirmation)
```

Names and tags are optional session metadata stored with the transcript and cached in the history index. Export reads the complete persisted message list directly, including system, assistant reasoning, tool-call, and tool-result fields; it is not limited by the TUI timeline.

---

### `coderAI status`
Print system diagnostics — API key status, default model, config directory, session count.

```bash
coderAI status
```

---

### `coderAI doctor` (deep install check)
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

Requires the index to be built first (`coderAI index`). The index stores its
embedding backend, model, and vector dimension. Changing any of them requires a
complete rebuild; `coderAI index` does this automatically, while scoped indexing
asks you to run `coderAI index --force` without `--paths`.

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
| `/skills` | List built-in, user, and trusted-project skill workflows |
| `/reasoning <high\|medium\|low\|none>` | Set thinking budget for reasoning models |
| `/yolo` | Toggle unsafe auto-approve mode for the session |
| `/verbose` | Toggle reasoning display, longer diff previews, and success notices |
| `/show <topic>` | Show reference info (e.g. `/show tasks`, `/show config`, `/show cost`) |
| `/copy` | Copy the last assistant response to clipboard (native tools, then OSC-52, then temp file) |
| `/code-search <query>` | Search the codebase semantically and view results inline |
| `/think` | Toggle thinking/reasoning display |
| `/clear` | Clear the conversation history and start fresh |
| `/allow-tool <tool-name> [scope]` | Always allow a tool this session (high-risk tools need a scope) |
| `/disallow-tool <tool-name>` | Remove a per-session tool allowlist entry |
| `/allowed-tools` | List tools already allowlisted this session |
| `/undo` | Undo last tool action |
| `/rewind <n> [--files]` | Rewind conversation to a past turn |
| `/mcp <name>` | List MCP servers or toggle/approve one · `/mcp__server__prompt` loads an MCP prompt |

| `/plan <request>` | Explore read-only and create a versioned implementation plan |
| `/plan` | Show the active plan |
| `/plan approve` | Approve and execute the exact active revision |
| `/plan answer <question-id> <answer>` | Set or replace one structured answer directly |
| `/plan edit [reset]` | Show the editable `draft.json`; `reset` discards unapplied edits |
| `/plan apply [path]` | Validate the editable artifact and create an immutable revision |
| `/plan amend <instruction>` | Ask the planner to revise the whole plan in read-only mode |
| `/plan resume` | Resume an interrupted executing revision with its exact approval linkage |
| `/plan cancel` | Cancel the active plan |
| `/export` | Export session to markdown |
| `/search <query>` | Show matching snippets from the conversation transcript |
| `/retry` | Restart the agent after a crash |
| `/resume [id]` | Resume a saved session |
| `/kill <id-or-name>` | Cancel a sub-agent |
| `/init` | Scaffold `.coderAI/` directory in the current project root |
| `/exit` | End the session |

Approval prompts offer a one-time action and, when the backend can derive a
safe reviewed command-prefix or path scope, an option to remember that scope
for the session. Remembering a scope does not enable `/yolo`.

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
| `session_retention_days` | `30` | Delete session files older than this many days (`0` disables cleanup) |
| `tui_notifications` | `true` | Ring terminal bell + emit OSC 9 notification when terminal is unfocused and needs attention |
| `budget_limit` | `0` | Max USD per session (`0` = unlimited) |
| `max_file_size` | `1048576` | Max file size readable by `read_file` (bytes) |
| `max_glob_results` | `200` | Max results returned by `glob_search` |
| `max_command_output` | `10000` | Max characters captured from `run_command` output |
| `max_tool_output` | `8000` | Max characters of any tool result kept in context |
| `sandbox_mode` | `off` | OS subprocess confinement: `off`, `best_effort`, or `required` |
| `sandbox_allow_network` | `false` | Permit network when OS confinement is active |
| `web_tools_in_main` | `true` | Allow web tools in the main agent (`web_search`, `read_url`, `http_request`, `download_file`) |
| `embedding_backend` | `auto` | Embeddings provider: `auto`, `openai`, or `local` |
| `embedding_model` | backend default | Optional embedding model override |
| `embedding_device` | backend default | Optional local device, such as `cpu`, `cuda`, or `mps` |
| `gemini_api_key` | — | Google Gemini API key (or set `GEMINI_API_KEY`) |
| `meta_api_key` | — | Meta Model API key (or set `MODEL_API_KEY` / `META_API_KEY`) |
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

**Project-level overrides** (`.coderAI/config.json` in the project root) accept
a limited subset. API keys, sandbox policy, and host resource caps remain global
so repository-authored configuration cannot weaken them.

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
| `MODEL_API_KEY` / `META_API_KEY` | `meta_api_key` |
| `CODERAI_DEFAULT_MODEL` | `default_model` |
| `CODERAI_TEMPERATURE` | `temperature` |
| `CODERAI_MAX_TOKENS` | `max_tokens` |
| `CODERAI_EMBEDDING_BACKEND` | `embedding_backend` |
| `CODERAI_EMBEDDING_MODEL` | `embedding_model` |
| `CODERAI_EMBEDDING_DEVICE` | `embedding_device` |
| `CODERAI_REASONING_EFFORT` | `reasoning_effort` |
| `CODERAI_MAX_ITERATIONS` | `max_iterations` |
| `CODERAI_MAX_TOOL_OUTPUT` | `max_tool_output` |
| `CODERAI_BUDGET_LIMIT` | `budget_limit` |
| `CODERAI_SESSION_RETENTION_DAYS` | `session_retention_days` |
| `CODERAI_LOG_LEVEL` | `log_level` |
| `CODERAI_PROJECT_INSTRUCTION_FILE` | `project_instruction_file` |
| `CODERAI_TOOL_TIMEOUT_SECONDS` | `tool_timeout_seconds` |
| `CODERAI_SUBPROCESS_TIMEOUT_SECONDS` | `subprocess_timeout_seconds` |
| `CODERAI_SANDBOX_MODE` | `sandbox_mode`: `off` (default), `best_effort`, or `required` |
| `CODERAI_SANDBOX_ALLOW_NETWORK` | `sandbox_allow_network`; network remains denied by default when sandboxed |
| `CODERAI_TOOL_RETRY_MAX_ATTEMPTS` | `tool_retry_max_attempts` |
| `CODERAI_TOOL_RETRY_BASE_DELAY` | `tool_retry_base_delay` |
| `CODERAI_MAX_BACKGROUND_PROCESSES` | `max_background_processes` |
| `LMSTUDIO_ENDPOINT` | `lmstudio_endpoint` |
| `OLLAMA_ENDPOINT` | `ollama_endpoint` |
| `CODERAI_TUI_NOTIFICATIONS` | `tui_notifications` |
| `CODERAI_ALLOW_LOCAL_URLS` | `"1"` to allow SSRF-protected web tools to reach localhost |
| `CODERAI_ALLOW_OUTSIDE_PROJECT` | `"1"` to allow file/terminal/refactor tools outside the project root |

### Execution Sandbox

`CODERAI_SANDBOX_MODE=required` confines model-authored commands, REPL code,
tests/lint/format/package/git subprocesses, background commands, trusted project
hooks, and MCP stdio servers with Bubblewrap on Linux or `sandbox-exec` on macOS.
The project and temporary directories are writable; other host paths are
read-only and network is denied. Set `CODERAI_SANDBOX_ALLOW_NETWORK=1` only when
sandboxed commands must download packages, use remote git, or run a networked
MCP server.

The compatibility default is `off`. `best_effort` uses a backend when its
feature probe succeeds, otherwise logs an explicit `running unconfined` warning.
`required` refuses to launch if no backend is usable. `off` and best-effort
fallbacks must not be treated as confinement. Host files remain readable even
in an active sandbox.

---

## Tool Quick Reference

Native tools are discovered at runtime, with `manage_context` registered manually and rare git ops supplied by the bundled `git_extended` MCP server. Browser tools require `pip install 'coderai-agent[browser]'`; PDF extraction in `read_url` requires `pip install 'coderai-agent[web]'`; desktop tools are macOS-only. Batch edits use `search_replace` with an `edits` list. Confirmation required (`✓`) means the agent asks before running.

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

Rare git ops auto-connect via the bundled `git_extended` MCP server as
`mcp__git_extended__git_checkout`, `…__git_push`, `…__git_pull`, `…__git_fetch`,
`…__git_merge`, `…__git_rebase`, `…__git_revert`, `…__git_reset`, `…__git_show`,
`…__git_remote`, `…__git_blame`, `…__git_cherry_pick`, `…__git_stash`, `…__git_tag`.

### Search

| Tool | Confirm | Description |
|---|---|---|
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
| `manage_context` | — | Pin/unpin files from the context window |
| `manage_tasks` | — | Persistent TODO list management |

### Multi-Agent & Collaboration

| Tool | Confirm | Description |
|---|---|---|
| `delegate_task` | ✓ | Spawn an isolated sub-agent |

### Execution & Planning

| Tool | Confirm | Description |
|---|---|---|
| `python_repl` | ✓ | Run Python code in an isolated subprocess |
| `use_skill` | — | Load a skill workflow |
| `submit_plan` | — | Submit the complete structured proposal (Plan Mode only) |
| `request_plan_amendment` | — | Stop divergent execution and propose a replacement revision for reapproval |

### Vision

| Tool | Confirm | Description |
|---|---|---|
| `read_image` | — | Read and encode an image for analysis |

### MCP

| Tool | Confirm | Description |
|---|---|---|
| `mcp_connect` | ✓ | Connect to an external MCP server |
| `mcp_disconnect` | ✓ | Disconnect from an MCP server |
| `mcp_list` | — | List connected servers, tools, resources, and prompts |
| `mcp_list_resources` | — | List resources exposed by a connected server |
| `mcp_read_resource` | — | Read a resource (by URI) from a connected server |
| `mcp_list_prompts` | — | List prompt templates exposed by a connected server |
| `mcp_get_prompt` | — | Fetch a prompt template (with arguments) from a server |

### Browser Automation

*Requires `pip install 'coderai-agent[browser]'` and `playwright install chromium`.*

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
| `undo` | ✓ | Roll back the latest durable workspace transaction; `transaction_id` selects an exact transaction, with legacy file-index fallback for older sessions |
| `undo_history` | — | View session-owned transaction history and legacy file-backup history |

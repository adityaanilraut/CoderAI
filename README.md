<p align="center">
  <h1 align="center">🤖 CoderAI</h1>
  <p align="center"><strong>An autonomous, multi-agent coding assistant that lives in your terminal.</strong></p>
  <p align="center">
    <a href="https://github.com/adityaanilraut/CoderAI/actions/workflows/ci.yml"><img src="https://github.com/adityaanilraut/CoderAI/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  </p>
  <p align="center">
    <a href="#-getting-started">Getting Started</a> · <a href="#architecture">Architecture</a> · <a href="#tools-reference">Tools</a> · <a href="#agent-system">Agents</a> · <a href="#workflows--skills">Workflows</a>
  </p>
</p>

---

CoderAI is a Python CLI tool that pairs an LLM with a focused set of coding tools to read, write, search, debug, test, and ship code from your terminal. It supports **8 LLM providers**, **6 specialist agent personas**, multi-agent delegation, optional semantic search and browser automation, and a persistent task checklist for multi-step work.

## ✨ Key Features

| Feature | Description |
|---|---|
| **Multi-Provider LLM** | OpenAI, Anthropic Claude, Groq, DeepSeek, Gemini, Meta, LM Studio, Ollama |
| **Progressive Tools** | A compact universal schema set plus deterministic objective-routed file, Git, terminal, web, browser, MCP, memory, and code-intelligence capabilities |
| **Browser Automation** | Cross-platform browser control via Playwright — form filling, shopping, data entry, web scraping |
| **Multi-Agent System** | Spawn isolated sub-agents for code review, security audit, research, etc. |
| **Planning & Tasks** | Enforced read-only `/plan` workflow plus persistent execution checklists via `manage_tasks` / `/tasks` |
| **Textual interactive UI** | `coderAI chat` uses a pure-Python [Textual](https://textual.textualize.io/) TUI ([`docs/CHAT_EVENTS.md`](docs/CHAT_EVENTS.md)) |
| **Rich CLI output** | Non-interactive commands (`status`, `config`, `history`, …) use [Rich](https://github.com/Textualize/rich) for tables and formatting |
| **Semantic Search** | Natural-language code search via OpenAI or fully local embeddings + ChromaDB |
| **Context Management** | Pin files, auto-detect project type, smart context compaction |
| **Persistent Memory** | Key-value store that survives across sessions |
| **Undo / Rollback** | Session-owned, conflict-safe workspace transactions |
| **MCP Integration** | Connect to external Model Context Protocol servers |
| **Skills & Rules** | Reusable skill workflows and per-project coding rules |
| **Cost Tracking** | Real-time token and cost accounting with budget limits |
| **Hooks** | Pre/post tool execution hooks via `.coderAI/hooks.json` |

---

## 🚀 Getting Started

**Requirements:** Python 3.10+

```bash
# 1. Clone
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI

# 2a. Install (core)
pip3 install -e .

# 2b. Optional extras (combine as needed, e.g. ".[semantic,local-embeddings]"):
#   semantic  → ChromaDB-backed `coderAI index` / `search` + semantic_search tool
#   local-embeddings → private, on-device embeddings via sentence-transformers
#   web       → PDF extraction in read_url (pypdf)
#   browser   → Playwright browser automation
pip3 install -e ".[semantic]"

# Browser automation also needs a Chromium download:
pip3 install -e ".[browser]"
playwright install chromium

# 3. Configure at least one provider (interactive wizard)
coderAI setup

# 4. Verify your install (config, keys, binary, cache)
coderAI doctor

# 5. Start chatting
coderAI                    # default: opens Textual chat UI
coderAI chat -m opus       # pick a model/alias
coderAI chat --resume ID   # resume a saved session
```

Don't want to run the wizard? Set a provider key as an environment variable
instead — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`,
`DEEPSEEK_API_KEY`, or `GEMINI_API_KEY`. Copy [`.env.example`](.env.example)
for the full flag list. For local inference, run
`coderAI config set default_model lmstudio` (or `ollama`).

**Platforms:** Linux and macOS are fully supported; Windows is best-effort.
See [INSTALL.md](docs/INSTALL.md) and [SECURITY.md](SECURITY.md#supported-platforms).

### Interactive chat commands

Type a slash inside `coderAI chat`:

| Command | Description |
|---|---|
| `/help` | Open the command menu |
| `/model [name]` | Switch session model · `/model default <name>` to persist |
| `/tokens` · `/status` · `/context` | Session bar refresh |
| `/compact` | Force-compress conversation history |
| `/agents` | Note about the live agents table |
| `/persona [name\|default\|list]` | List, apply, or clear an agent persona |
| `/skills` | List available built-in, user, and trusted-project skill workflows |
| `/clear` | Wipe conversation & context |
| `/reasoning <high\|medium\|low\|none>` | Thinking budget for reasoning models |
| `/yolo` | Toggle auto-approve for high-risk tools |
| `/show <topic>` | Reference info (`models`, `cost`, `config`, `tasks`, …) |
| `/code-search <query>` | Semantic codebase search inline |
| `/export` | Save the session timeline as markdown |
| `/verbose` | Toggle reasoning, longer diff previews, and success notices |
| `/exit` | Shut down the agent |

See [COMMANDS.md](docs/COMMANDS.md) for the full CLI reference.

---

## 🏗️ Architecture

### High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                          CLI Layer                                │
│     coderAI/cli/  —  Click commands & entry (coderAI.cli:main)    │
│                                                                   │
│   one-shot subcommands ──► coderAI/cli/utils (Rich helpers)       │
│   `coderAI run`         ──► headless one-shot (no TUI)            │
│   `coderAI chat`        ──► coderAI/tui (Textual TUI)             │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────┴───────────────────────────────────────┐
│                         Agent Layer                               │
│                    coderAI/core/agent.py                          │
│  • Agentic loop (process_message → LLM → tools → LLM → ...)      │
│  • Context window management with auto-summarization              │
│  • Retry logic with exponential backoff                           │
│  • Pre/Post tool hooks                                            │
│  • Cooperative cancellation via AgentTracker                      │
└───────┬──────────────┬──────────────────┬────────────────────────┘
        │              │                  │
   ┌────┴────┐   ┌─────┴──────┐   ┌──────┴──────┐
   │   LLM   │   │   Tools    │   │  Sub-Agent  │
   │Providers│   │  Registry  │   │  Delegation │
   │   (8)   │   │ (Runtime)  │   │  (Isolated) │
   └─────────┘   └────────────┘   └─────────────┘
```

`UIBridge` (`coderAI/tui/controller.py`) is an in-process controller used
by the Textual TUI: it subscribes to `event_emitter`, forwards events to
the UI via an `on_event` callback, and dispatches slash commands back into
the agent. See [`docs/CHAT_EVENTS.md`](docs/CHAT_EVENTS.md) for the event
catalog.

---

## 📁 Project Structure Tree

```
CoderAI/
├── pyproject.toml              # Package metadata, dependencies, entry point
├── requirements.lock           # Pinned, hashed deps (make lock / pip-audit)
├── requirements.txt            # Compat shim → `pip install -e .`
├── .env.example                # Provider keys + CODERAI_* flags
├── CHANGELOG.md                # Release notes
├── Makefile                    # Dev shortcuts (test, lint, install)
├── LICENSE                     # MIT License
├── README.md                   # ← You are here
├── SECURITY.md                 # Threat model, controls, residual risks
│
├── coderAI/                    # ─── Main Python Package (import name) ───
│   ├── __init__.py             # Version via importlib.metadata
│   ├── types/                  # Shared leaf types (Provenance, ToolErrorCode, …)
│   ├── cli/                    # Click CLI (entry: coderAI.cli:main)
│   │   ├── main.py             #   Root group; chat, info, doctor, status, …
│   │   ├── run_cmd.py          #   `coderAI run` (headless one-shot)
│   │   ├── setup_cmd.py        #   Interactive setup wizard
│   │   ├── mcp_cmd.py          #   `coderAI mcp` server management
│   │   └── utils.py            #   Rich helpers for one-shot CLI output
│   ├── system_prompt.py        # Compat shim → prompts.compose
│   ├── prompts/                # MDX templates + compose.py (system prompt)
│   ├── assets/                 # Packaged personas, skills, rules, and /init starters
│   ├── skills/                 # Skill discovery framework (not the use_skill tool)
│   ├── mcp_servers/            # Bundled stdio MCP servers (e.g. git_extended)
│   ├── py.typed                # Mypy marker file
│   │
│   ├── core/                   # ─── Core Orchestration Layer ───
│   │   ├── agent.py            #   Main agent orchestrator
│   │   ├── agent_loop.py       #   ExecutionLoop: LLM-tool iteration loop
│   │   ├── agent_capabilities.py # Tool registry, personas, approvals, hooks
│   │   ├── capability_routing.py # Objective-scoped progressive schemas
│   │   ├── agent_session.py    #   Session lifecycle, checkpoints, rewind
│   │   ├── execution_context.py # Immutable run/session/workspace identity
│   │   ├── agent_tracker.py    #   Real-time agent registry & cooperative cancellation
│   │   ├── personas.py         #   Scoped persona loader (project → user → builtin)
│   │   ├── session_bootstrap.py # Shared session bootstrap (TUI + headless)
│   │   ├── permissions.py      #   Approval / high-risk policy
│   │   ├── services.py         #   ContextVar service container
│   │   ├── tool_executor.py    #   Tool execution runner & confirmation gates
│   │   └── tool_routing.py     #   ToolRegistry + MCP wire-format dispatch
│   │
│   ├── system/                 # ─── System & Persistence ───
│   │   ├── config.py / display.py / command_safety.py / cost.py / …
│   │   ├── hooks_manager.py / history.py / trust.py / sandbox.py / …
│   │   └── safeguards.py / proc.py / events.py / retry.py / …
│   │
│   ├── context/                # Context window + semantic index
│   ├── embeddings/             # OpenAI + optional local embeddings
│   ├── tui/                    # Textual interactive chat UI
│   ├── llm/                    # Provider backends + factory
│   └── tools/                  # Native tools (+ filesystem/, web/)
│       ├── use_skill.py        # use_skill tool (project/user/builtin content)
│       ├── git.py              # Native core git
│       └── git_extended.py     # Rare git → bundled MCP (not auto-registered)
│
├── .coderAI/                   # This repository's project-scoped overlays
├── tests/                      # Mirrors package layout (core/, tools/, tui/, …)
│   └── security/               # Security regression suite
└── docs/                       # ARCHITECTURE, COMMANDS, INSTALL, …
```

### Naming conventions

| Surface | Form | Example |
|---|---|---|
| Product / repo | `CoderAI` | GitHub repo title |
| PyPI distribution | `coderai-agent` | `pip install coderai-agent` |
| Python package / CLI | `coderAI` | `import coderAI`, `coderAI chat` |
| Config / project dir | `.coderAI` | `~/.coderAI/`, project overlays |
| Env vars / root doc | `CODERAI_*` / `CODERAI.md` | `CODERAI_DEFAULT_MODEL` |

The PyPI project is `coderai-agent` because `coderai` / `coder-ai` are already taken. Do not use `CoderAI` as an import path.

**Skills triad:** `coderAI/skills/` (framework) · `coderAI/tools/use_skill.py` (tool) · `coderAI/assets/skills/`, `~/.coderAI/skills/`, and `.coderAI/skills/` (content scopes).

**Agents triad:** `coderAI/core/agent.py` (orchestrator) · `coderAI/core/personas.py` (persona loader) · `coderAI/assets/agents/`, `~/.coderAI/agents/`, and `.coderAI/agents/` (persona scopes).

---

## 🔁 The Agentic Loop

The heart of CoderAI is the **agentic loop** in `coderAI/core/agent.py → process_message()`. Here is how every user message flows through the system:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. User sends message                                          │
│  2. Inject pinned context + project instructions                │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Agent Core                                 │
│                   coderAI/core/agent.py                           │
│                                                                   │
│   • Lifecycle & Session state (`history.py`)                      │
│   • Capability & Tool Routing (`core/capability_routing.py`)     │
│   • System Prompt Composer (`prompts/compose.py`)                 │
│   • Personas (`core/personas.py`) & Skills (`skills/`)            │
│   • Context Management (`context/context_controller.py`)          │
│   • Sub-Agent Spawning & Worktree Isolation (`tools/subagent.py`) │
│   • Workspace Transactions & Undo (`core/workspace_transactions`) │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Execution Loop                               │
│                 coderAI/core/agent_loop.py                        │
│                                                                   │
│   • Provider Chat & Streaming (`coderAI/llm/`)                   │
│   • Tool Execution (`core/tool_executor.py`)                      │
│   • Loop Safeguards & Error Policies (`system/loop_guard.py`)     │
│   • Cost & Budget Accounting (`system/cost.py`)                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Tools Reference

CoderAI discovers 67 native tools automatically at runtime and registers `manage_context` and `request_plan_amendment` with the agent (69 native tools), plus rare git operations on the bundled `git_extended` MCP server. Each tool adheres to the `Tool` abstract base class with strict safety checks, permission gates, path sanitization, and transaction rollback.

### Filesystem

| Tool | Description |
|---|---|
| `read_file` | Read file contents with optional line range |
| `write_file` | Create or overwrite files (protected paths blocked) |
| `search_replace` | Find and replace text in a file with verification (batch via `edits`) |
| `apply_diff` | Apply a unified diff patch for multi-line edits |
| `multi_edit` | Apply multiple text replacements across a file in a single step |
| `list_directory` | List files and subdirectories with sizes and metadata |
| `glob_search` | Find files matching glob patterns (`**/*.py`) |
| `move_file` | Move or rename a file or directory |
| `copy_file` | Copy a file or directory tree |
| `delete_file` | Delete a file or directory (recursive opt-in) |
| `create_directory` | Create directories including parents (`mkdir -p`) |
| `file_stat` | Get detailed file metadata (size, permissions, timestamps) |
| `file_chmod` | Modify POSIX file permissions |
| `file_readlink` | Read symlink targets safely |
| `directory_tree` | Visual directory tree up to a max depth |
| `read_file_slice` | Read a line range from a file |
| `workspace_status` | Git changes plus recently modified files |

### Terminal & Process Management

| Tool | Description |
|---|---|
| `run_command` | Execute shell commands in a scrubbed environment |
| `run_background` | Start long-running background processes (servers, test watchers) |
| `list_processes` | List active background processes started by the agent |
| `kill_process` | Terminate a background process by PID |
| `read_bg_output` | Read buffered standard output/error from background processes |
| `write_bg_input` | Write to a tracked background process stdin |

### Git (Native)

Everyday Git operations remain native and sandboxed.

| Tool | Description |
|---|---|
| `git_add` | Stage specific files or patterns for commit |
| `git_status` | Show working tree status and untracked files |
| `git_diff` | View diffs (staged, unstaged, between commits/refs) |
| `git_commit` | Create commits with structured messages |
| `git_log` | View commit history and graph |
| `git_branch` | List, create, or delete branches |

**Extended Git via MCP (`mcp__git_extended__…`):** `git_checkout`, `git_stash`, `git_push`, `git_pull`, `git_fetch`, `git_merge`, `git_rebase`, `git_revert`, `git_reset`, `git_show`, `git_remote`, `git_blame`, `git_cherry_pick`, `git_tag`.

### Search & Intelligence

*`semantic_search` requires `coderai-agent[semantic]`. It uses OpenAI embeddings when configured, or `coderai-agent[local-embeddings]` for local sentence-transformers.*

| Tool | Description |
|---|---|
| `grep` | Fast regex pattern matching across project files with context |
| `symbol_search` | Search AST symbol definitions (classes, functions, methods) |
| `semantic_search` | Natural-language code search via vector embeddings |

### Web & Network

*PDF extraction in `read_url` requires `coderai-agent[web]` (`pypdf`).*

| Tool | Description |
|---|---|
| `web_search` | Web search (DuckDuckGo, Tavily, Exa) with snippet retrieval |
| `read_url` | Fetch, sanitize, and extract text/markdown from URLs (SSRF protected) |
| `download_file` | Download files to project workspace with size limits |
| `http_request` | Generic HTTP client with proxy and SSRF guards |

### Project, Context & Planning

| Tool | Description |
|---|---|
| `manage_context` | Pin/unpin files to keep critical code inside prompt context |
| `manage_tasks` | Manage persistent task list (`tasks.json`) with status and priorities |
| `submit_plan` | Submit structured execution plan in Plan Mode |
| `use_skill` | Activate a reusable skill workflow |
| `context_stats` | Token-budget breakdown of the live context window |
| `export_session` | Write the session transcript as HTML or Markdown |

### Multi-Agent Delegation

| Tool | Description |
|---|---|
| `delegate_task` | Spawn isolated child agents with domain-scoped worktrees |

### Code Quality & Refactoring

| Tool | Description |
|---|---|
| `lint` | Auto-detect and run project linter (ruff, eslint, clippy, flake8) |
| `format` | Auto-detect and run formatter (ruff, prettier, black, gofmt) |
| `run_tests` | Auto-detect and execute test runners (pytest, jest, cargo test, go test) |
| `refactor` | AST-aware multi-file symbol renaming and reference searching |
| `package_manager` | Install, remove, and audit dependencies (pip, npm, cargo, etc.) |

### Vision & Multimodal

| Tool | Description |
|---|---|
| `read_image` | Load, inspect, and encode local images for vision-capable models |

### Browser Automation

*Requires `pip install 'coderai-agent[browser]'` and `playwright install chromium`.*

| Tool | Description |
|---|---|
| `browser_navigate` | Navigate to a URL and wait for load completion |
| `browser_snapshot` | Capture accessibility tree with stable element references (`[e1]`, `[e2]`) |
| `browser_click` | Click elements using accessibility snapshot references |
| `browser_type` | Type text into inputs and forms with optional clearing |
| `browser_select_option` | Select items from dropdowns and select elements |
| `browser_get_content` | Extract clean markdown or text content from web pages |
| `browser_screenshot` | Capture viewport or full-page PNG screenshots |
| `browser_evaluate` | Run sandboxed JavaScript expressions within the page |
| `browser_wait` | Wait for selector, text match, or timeout |
| `browser_close` | Close active browser context and release resources |

### MCP Integration

| Tool | Description |
|---|---|
| `mcp_connect` | Connect to an external MCP server via stdio, SSE, or HTTP |
| `mcp_disconnect` | Terminate connection to an MCP server |
| `mcp_list` | List active MCP servers, tools, resources, and prompts |
| `mcp_list_resources` | Discover resources exposed by connected MCP servers |
| `mcp_read_resource` | Read resource contents from an MCP server URI |
| `mcp_list_prompts` | Discover prompt templates exposed by MCP servers |
| `mcp_get_prompt` | Retrieve and format prompt templates from an MCP server |

### Undo & Transaction Rollback

| Tool | Description |
|---|---|
| `undo` | Roll back the latest workspace transaction or a specific transaction ID |
| `undo_history` | Inspect durable session transaction logs and snapshots |

### Desktop Automation (macOS only)

| Tool | Description |
|---|---|
| `run_applescript` | Execute AppleScript or JXA on the macOS host |
| `get_accessibility_tree` | Retrieve the macOS accessibility UI tree as JSON |
| `click_ui_element` | Click a UI element via AppleScript System Events |
| `type_keystrokes` | Simulate typing or key presses on macOS |

### MCP Integration

Connect external Model Context Protocol servers (stdio / SSE / Streamable HTTP)
via `coderAI mcp add`, presets (`coderAI mcp catalog`), or project `.mcp.json`.
Scopes: user (`~/.coderAI/mcp_servers.json`), project (`.mcp.json`), local.
Tools appear as `mcp__<server>__<tool>`; prompts as `/mcp__<server>__<prompt>`.

| Tool | Description |
|---|---|
| `mcp_connect` | Connect to an external MCP server (`env`/`cwd`/`timeout` supported) |
| `mcp_disconnect` | Disconnect from an MCP server |
| `mcp_list` | List connected servers and their tools, resources, and prompts |
| `mcp_list_resources` | List resources exposed by a connected MCP server |
| `mcp_read_resource` | Read a resource (by URI) from a connected MCP server |
| `mcp_list_prompts` | List prompt templates exposed by a connected MCP server |
| `mcp_get_prompt` | Fetch a prompt template (with arguments) from a server |

### Undo / Rollback

Undo and rewind history is bound to the executing agent's persisted session.
Concurrent delegated agents use distinct recovery ledgers, and resuming a
session reopens that same ledger. Approved synchronous mutations now receive a
durable before/after transaction record, including changes observed after
foreground shell commands and tool hooks. Rollback refuses to overwrite later
user changes and keeps partial failures retryable. Long-lived background
processes, Git metadata-only changes, and isolated mutating worktrees remain
Milestone 3 work.

| Tool | Description |
|---|---|
| `undo` | Roll back the latest workspace transaction, or a named transaction ID |
| `undo_history` | View durable transaction and legacy file-backup history |

---

## 🤖 Agent System

### Agent Personas

CoderAI provides specialized agent personas with deterministic precedence: trusted project `.coderAI/agents/`, user `~/.coderAI/agents/`, then packaged `coderAI/assets/agents/`. Built-in personas (`planner`, `code-reviewer`) are packaged with the distribution, while custom personas (like `architect`, `security-reviewer`, `tdd-guide`, and `build-error-resolver`) can be placed in `.coderAI/agents/`. Project personas are withheld until workspace-trust is established.

Each persona defines:
- **`name`** — Identifier used for `/persona` or delegated persona selection
- **`description`** — What the agent specializes in
- **`tools`** — High-level tool labels (for example `Read`, `Edit`, `Bash`) that expand into concrete runtime tools; read-only tools remain available for codebase inspection
- **`model`** — Preferred LLM model
- **Instructions** — Full system prompt in markdown body

| Persona | Source | Specialty |
|---|---|---|
| `planner` | Built-in | Implementation planning for complex features |
| `code-reviewer` | Built-in | Code quality, correctness, and conventions |
| `architect` | Project / User | Architecture analysis and structural design |
| `security-reviewer` | Project / User | Security vulnerability analysis and auditing |
| `tdd-guide` | Project / User | Test-driven development workflows and test design |
| `build-error-resolver` | Project / User | Build error diagnosis and fixing |

### Sub-Agent Delegation

The `delegate_task` tool spawns **isolated sub-agents** in their own sessions. The `agent_role` can be an exact persona file name such as `security-reviewer` or a natural alias such as `Code Reviewer`; when it resolves to a persona, the sub-agent inherits that persona's prompt and mutating-tool policy.

```
Parent Agent
│
├── delegate_task("Review auth module", role="security-reviewer")
│   └── Sub-Agent (security-reviewer persona; isolated Git worktree for mutations)
│       ├── read_file("src/auth.py")
│       ├── grep("password|token|secret")
│       ├── ... (autonomous tool calls)
│       └── Returns comprehensive report
│
├── delegate_task("Research React 19 features", role=None)
│   └── Sub-Agent (general)
│       ├── web_search("React 19 new features")
│       ├── read_url(...)
│       └── Returns research summary
│
└── Continues with parent session (context preserved)
```

**Key Properties:**
- Max delegation depth: **3** (prevents infinite recursion)
- Sub-agents inherit the parent's pinned context and project instructions
- Failed sub-agents are **retried up to 2 times** with exponential backoff
- Each sub-agent has its own isolated session and token tracking
- Workspace-mutating sub-agents use detached Git worktrees; their exact patch is
  conflict-checked and integrated only after parent review/approval
- Sub-agents are tracked in the global `AgentTracker` with parent-child links

### Agent Tracker

The `AgentTracker` (`agent_tracker.py`) provides **real-time observability**:

- Status tracking: `IDLE → THINKING → TOOL_CALL → DONE/ERROR/CANCELLED`
- Token and cost accounting per agent
- Context window usage percentage
- Cooperative cancellation (with recursive child cancellation)
- `/agents` command in chat shows all active agents

### Resource Locking

The `ResourceManager` (`locks.py`) prevents race conditions during parallel execution:

- **Per-file locks** — Normalized path-based asyncio locks
- **Git lock** — Prevents concurrent git operations (index.lock conflicts)
- **Workspace lock** — For broad operations like test runs

---

## 📋 Workflows & Skills

### Skills

Skills are predefined step-by-step workflows. CoderAI resolves them in the same
explicit order: trusted project `.coderAI/skills/<name>/SKILLS.md`, user-wide
`~/.coderAI/skills/`, then packaged built-ins in `coderAI/assets/skills/`.
Ecosystem `SKILL.md` files are accepted and normalized on install. `/init`
copies its starter persona, rule, and `CODERAI.md` from those installed package
resources without overwriting existing files.

**Install from GitHub or a local path** (Claude Code–style):

```bash
coderAI skills install owner/repo
coderAI skills install owner/repo/skills/foo --scope user
coderAI skills install https://github.com/owner/repo --path skills/bar
coderAI skills install ./my-skill
coderAI skills list
coderAI skills remove foo
```

Shipped examples:

| Skill | Description |
|---|---|
| `security-audit` | 5-step security review (credentials, injection, auth, deps, logging) |
| `tdd-workflow` | Test-driven development workflow guide |

Use them via the `use_skill` tool:
```
> Use the security-audit skill to review the auth module
```

### Task Tracking

Use `manage_tasks` for a persistent execution checklist during multi-step work.
In chat, `/tasks` refreshes that checklist. `/plan <request>` is a separate,
enforced read-only workflow that creates a versioned plan for review. Plan
cards show stable decisions and the mutable `.coderAI/plans/<id>/draft.json`
artifact. Use `/plan answer <id> <answer>`, `/plan edit`, and `/plan apply` to
review or change it without another model rewrite; `/plan approve` executes the
exact hashed revision, and `/plan resume` continues an interrupted attempt.

The same lifecycle is available without the TUI for scripts and CI:

```bash
coderAI plan create --json "add parser validation"
coderAI plan show --json
coderAI plan edit                         # prints the draft.json path
coderAI plan apply --json                 # validates edits and creates a revision
coderAI plan answer storage SQLite --json
coderAI plan approve --json               # approval only
coderAI plan execute --auto-approve --json
```

Headless execution denies mutations unless `--auto-approve`/`--yolo` is
explicit. If implementation discovers that the approved plan must change, the
agent records an execution amendment, stops mutation, and requires approval of
the new revision.

### Project Rules

Rules in `.coderAI/rules/*.md` are **automatically injected** into every agent's system prompt:

- `001-common-principles.md` — TDD, security-first, tool usage, communication
- `101-python-standards.md` — Python-specific coding conventions

### Hooks

Define pre/post tool execution hooks in `.coderAI/hooks.json`:

```json
{
  "hooks": [
    {
      "type": "PostToolUse",
      "tool": "write_file",
      "command": "ruff check --fix ."
    }
  ]
}
```

---

## 🔌 LLM Providers

| Provider | Models | Requirements |
|---|---|---|
| **OpenAI** | `gpt-5.6` (`gpt-5.6-sol`), `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `o1`, `o1-mini`, `o3-mini` | `OPENAI_API_KEY` |
| **Anthropic** | `claude-fable-5`, `claude-sonnet-5`, `claude-opus-4-8`, `fable`, `sonnet`, `opus`, `haiku` | `ANTHROPIC_API_KEY` |
| **Groq** | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `llama3-70b-8192`, `llama3-8b-8192` | `GROQ_API_KEY` |
| **DeepSeek** | `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v3.2`, `deepseek-r1` | `DEEPSEEK_API_KEY` |
| **Gemini** | `gemini-3.5-flash`, `gemini-3.1-pro`, `gemini-2.5-flash`, `gemini-2.5-pro`, … | `GEMINI_API_KEY` |
| **Meta** | `muse-spark-1.1`, `muse-spark`, `muse` | `MODEL_API_KEY` or `META_API_KEY` |
| **LM Studio** | Any local model | LM Studio running locally |
| **Ollama** | Any local model | Ollama running locally |

All providers implement the `LLMProvider` interface: `chat()`, `stream()`, `count_tokens()`, `supports_tools()`.

---

## ⚙️ Configuration

Configuration is stored in `~/.coderAI/config.json` and managed via `coderAI config` or `coderAI setup`.

| Key | Default | Description |
|---|---|---|
| `default_model` | `claude-4-sonnet` | Default LLM model |
| `temperature` | `0.7` | Sampling temperature |
| `max_tokens` | `8192` | Max output tokens |
| `context_window` | `128000` | Context window size |
| `max_iterations` | `50` | Max agentic loop iterations |
| `reasoning_effort` | `medium` | Reasoning depth (`high`/`medium`/`low`/`none`) |
| `streaming` | `true` | Enable streaming responses |
| `save_history` | `true` | Persist conversation sessions |
| `budget_limit` | `0` | Max cost in USD (0 = unlimited) |
| `web_tools_in_main` | `true` | Allow web tools in the main agent |
| `browser_headless` | `true` | Run browser in headless mode |
| `browser_timeout` | `30.0` | Browser operation timeout in seconds |
| `browser_allowed_domains` | — | Comma-separated domain allowlist (blank = all allowed) |
| `approval_timeout_seconds` | `300` | Seconds before approval prompts auto-deny (0 = wait forever) |
| `tool_timeout_seconds` | `120.0` | Outer wall-clock cap per tool call (tools with their own `timeout` argument derive a larger cap automatically) |
| `tool_timeout_overrides` | `{}` | Per-tool-name overrides of the outer cap, e.g. `{"run_tests": 900}` |
| `subprocess_timeout_seconds` | `60.0` | Default timeout for one-shot tool subprocesses (format/lint/grep/git) |
| `tool_retry_max_attempts` | `2` | Transient-failure retries for opt-in tools (web fetches); `0` disables |
| `tool_retry_base_delay` | `1.0` | Base delay (seconds) for tool-retry exponential backoff |
| `max_background_processes` | `10` | Tracked `run_background` processes (global only — not project-overridable) |

---

## 🔒 Security

CoderAI treats *untrusted input* — a cloned repo's `.coderAI/*` overlay, fetched
web pages, MCP server output — as data that must never act with your authority.
Two boundaries you'll see day to day:

- **Workspace trust.** A newly opened project is untrusted until you run `/trust`
  (or start with `--trust-workspace`). Until then, repo-supplied hooks, config
  overlays, and `ask` permission rules are ignored, so a malicious repo can't run
  a hook on your first message.
- **Injection-aware egress gating.** Once a turn ingests untrusted content, any
  network tool needs confirmation for the rest of that turn (so an injected page
  can't trigger a follow-up exfiltration fetch). MCP output goes further: a local
  *mutating* tool then needs an explicit OK **even under `--yolo`**.

Mutating tools confirm by default; high-risk tools can't be blanket-allowed;
credentials and history are stored owner-only; remote MCP/OAuth endpoints must be
HTTPS. The red-team regression corpus runs as a **blocking** CI job
(`make test-security`).

See **[SECURITY.md](SECURITY.md)** for the full threat model, the complete list
of controls, how to report a vulnerability, and the known residual risks.

---

## 🧪 Testing & CI

Pull requests run **Ruff** and **pytest** on GitHub Actions (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

```bash
# Install dev dependencies (pytest, ruff, mypy, …)
pip install -e ".[dev]"

# Lint (same as CI)
python -m ruff check coderAI/ tests/ scripts/

# Run the full test suite
pytest

# Deterministic progressive-routing accuracy and schema-token budget
python -m coderAI.evals.capability_routing

# Or use the Makefile (also runs install + CLI smoke checks)
make test

# Run only the red-team security suite (also a blocking CI job)
make test-security          # == pytest -m security

# Regenerate / audit the pinned, hashed lockfile
make lock                   # uv pip compile → requirements.lock
make audit                  # pip-audit -r requirements.lock

# Run specific test categories
pytest tests/test_agent.py
pytest tests/test_web.py

# Validate installation (config, keys, dependencies)
coderAI doctor

# Build and probe the wheel + sdist from outside the checkout
python -m build --outdir /tmp/coderai-dist
python scripts/verify_wheel.py /tmp/coderai-dist

# Optional installed-wheel probes (repeat --extra to combine)
python scripts/verify_wheel.py /tmp/coderai-dist --extra semantic --extra web
python scripts/verify_wheel.py /tmp/coderai-dist --extra browser --install-browser

# Static typing (CI gate; strict modules listed in pyproject.toml)
make typecheck
```

---

## 📄 CLI Commands

| Command | Description |
|---|---|
| `coderAI` / `coderAI chat` | Start interactive chat |
| `coderAI chat -m <model>` | Chat with specific model |
| `coderAI chat --resume <id>` | Resume a previous session |
| `coderAI chat --continue` | Resume the most recently updated session |
| `coderAI chat -p <persona>` | Start chat with a persona (e.g. `code-reviewer`) |
| `coderAI run "<prompt>"` | Headless one-shot: run a prompt and exit, no TUI (deny-on-mutate; `--yolo` to allow) |
| `coderAI run --json "<prompt>"` | Headless run emitting a structured JSON result to stdout |
| `coderAI run --output ndjson "<prompt>"` | Headless run emitting schema-versioned lifecycle/tool/assistant events and one terminal envelope |
| `coderAI mcp list` / `add` / `remove` | Manage MCP servers (also `login` / `logout` / `resources` / `prompts`) |
| `coderAI setup` | Interactive setup wizard |
| `coderAI doctor` | Diagnose install (config, keys, dependencies) |
| `coderAI models` | List available models and providers |
| `coderAI set-model <name>` | Set default model |
| `coderAI config show` | Show configuration |
| `coderAI config set <k> <v>` | Set a configuration value |
| `coderAI config reset` | Reset to defaults |
| `coderAI history list` | List all sessions |
| `coderAI history rename <id> <name>` | Name a saved session |
| `coderAI history tag <id> <tag>...` | Add tags to a saved session |
| `coderAI history export <id> --format markdown\|json` | Export the complete persisted transcript |
| `coderAI history clear` | Clear all history |
| `coderAI history delete <id>` | Delete a session |
| `coderAI info` | Show agent and model info |
| `coderAI status` | System diagnostics |
| `coderAI cost` | API cost and pricing info |
| `coderAI tasks list` | Show project tasks |
| `coderAI index` | Build/update the semantic code search index |
| `coderAI search <query>` | Search the codebase with natural language |

---

## 🧩 Extending CoderAI

### Adding a New Tool

```python
from pydantic import BaseModel, Field
from coderAI.tools.base import Tool

class MyParams(BaseModel):
    input: str = Field(..., description="Input value")

class MyCustomTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    parameters_model = MyParams
    is_read_only = True  # Set False if the tool mutates state

    async def execute(self, input: str, **kwargs):
        return {"success": True, "result": f"Processed: {input}"}

# Auto-discovered by tools/discovery.py if __init__ takes no required args.
# For tools that need the Agent (e.g. ManageContextTool), register manually
# in AgentCapabilitiesMixin._create_tool_registry() and update
# tests/test_tool_registry_snapshot.py.
```

### Adding a New Agent Persona

Create `.coderAI/agents/my-specialist.md`:

```markdown
---
name: my-specialist
description: Expert in my domain
tools: ["Read", "Grep", "Bash", "Glob"]
model: sonnet
---

You are an expert in [domain]. Your role is to...
```

### Adding a New LLM Provider

Implement the `LLMProvider` interface in `coderAI/llm/`:

```python
from coderAI.llm.base import LLMProvider

class MyProvider(LLMProvider):
    async def chat(self, messages, tools, **kwargs): ...
    async def stream(self, messages, tools, **kwargs): ...
    def count_tokens(self, text) -> int: ...
    def supports_tools(self) -> bool: ...
```

---

## 📜 License

MIT License — see [LICENSE](LICENSE).

## 👤 Author

**Aditya Raut** — [GitHub](https://github.com/adityaanilraut)

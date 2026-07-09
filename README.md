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

CoderAI is a Python CLI tool that pairs an LLM with **96 built-in tools** to read, write, search, debug, test, automate browsers, and ship code — all from a single terminal session. It supports **7 LLM providers**, **17 specialist agent personas**, a **multi-agent delegation system** with retry logic, a **semantic code search engine**, a **cross-platform browser automation engine**, and a **plan-and-execute workflow** to tackle complex tasks autonomously.

## ✨ Key Features

| Feature | Description |
|---|---|
| **Multi-Provider LLM** | OpenAI, Anthropic Claude, Groq, DeepSeek, Gemini, LM Studio, Ollama |
| **96 Tools** | File I/O, Git, terminal, web, browser automation, HTTP, memory, process management, background jobs, semantic search, and more |
| **Browser Automation** | Cross-platform browser control via Playwright — form filling, shopping, data entry, web scraping |
| **Multi-Agent System** | Spawn isolated sub-agents for code review, security audit, research, etc. |
| **Planning & Tasks** | Structured plan-and-execute workflows with persistent task tracking |
| **Textual interactive UI** | `coderAI chat` uses a pure-Python [Textual](https://textual.textualize.io/) TUI ([`docs/CHAT_EVENTS.md`](docs/CHAT_EVENTS.md)) |
| **Rich CLI output** | Non-interactive commands (`status`, `config`, `history`, …) use [Rich](https://github.com/Textualize/rich) for tables and formatting |
| **Semantic Search** | Natural-language code search via embeddings (OpenAI + ChromaDB) |
| **Context Management** | Pin files, auto-detect project type, smart context compaction |
| **Persistent Memory** | Key-value store that survives across sessions |
| **Undo / Rollback** | Revert any file modification instantly |
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

# 2b. Optional extras (combine as needed, e.g. ".[semantic,browser]"):
#   semantic  → ChromaDB-backed `coderAI index` / `search` + semantic_search tool
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
`DEEPSEEK_API_KEY`, or `GEMINI_API_KEY`. For local inference, run
`coderAI config set default_model lmstudio` (or `ollama`).

See [INSTALL.md](docs/INSTALL.md) for platform-specific notes and offline builds.

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
| `/skills` | List available project skill workflows |
| `/clear` | Wipe conversation & context |
| `/reasoning <high\|medium\|low\|none>` | Thinking budget for reasoning models |
| `/yolo` | Toggle auto-approve for high-risk tools |
| `/show <topic>` | Reference info (`models`, `cost`, `config`, `tasks`, `plan`, …) |
| `/code-search <query>` | Semantic codebase search inline |
| `/export` | Save the session timeline as markdown |
| `/verbose` | Toggle verbose tool output |
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
   │ (7)     │   │  (96)      │   │  (Isolated) │
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
CoderAI-main/
├── pyproject.toml              # Package metadata, dependencies, entry point
├── requirements.txt            # Pinned dependencies
├── Makefile                    # Dev shortcuts (test, lint, install)
├── LICENSE                     # MIT License
├── README.md                   # ← You are here
│
├── coderAI/                    # ─── Main Python Package ───
│   ├── __init__.py             # Package version
│   ├── cli/                    # Click CLI (entry: coderAI.cli:main)
│   │   ├── main.py             #   Root group; chat, info, doctor, status, …
│   │   ├── run_cmd.py          #   `coderAI run` (headless one-shot)
│   │   ├── mcp_cmd.py          #   `coderAI mcp` server management
│   │   ├── bootstrap.py        #   Shared session bootstrap (TUI + headless)
│   │   └── utils.py            #   Rich helpers for one-shot CLI output
│   ├── system_prompt.py        # Default system prompt with tool docs & strategies
│   ├── prompts/                # MDX system-prompt templates
│   ├── skills/                 # Skill discovery and hosted-skill sources
│   ├── py.typed                # Mypy marker file
│   │
│   ├── core/                   # ─── Core Orchestration Layer ───
│   │   ├── agent.py            #   Main agent orchestrator
│   │   ├── agent_loop.py       #   ExecutionLoop: LLM-tool iteration loop
│   │   ├── agent_capabilities.py # Tool registry, personas, approvals, hooks
│   │   ├── agent_session.py    #   Session lifecycle, checkpoints, rewind
│   │   ├── agent_tracker.py    #   Real-time agent registry & cooperative cancellation
│   │   ├── agents.py           #   AgentPersona loader from .coderAI/agents/*.md
│   │   ├── jobs.py             #   JobManager: background tool jobs (start_job & co.)
│   │   ├── permissions.py      #   Approval / high-risk policy
│   │   ├── provenance.py       #   Untrusted-ingest tainting
│   │   ├── tool_executor.py    #   Tool execution runner & confirmation gates
│   │   └── tool_routing.py     #   ToolRegistry + MCP wire-format dispatch
│   │
│   ├── system/                 # ─── System & Persistence ───
│   │   ├── config.py           #   Pydantic config with JSON persistence (~/.coderAI/config.json)
│   │   ├── cost.py             #   Token cost tracking with per-model pricing
│   │   ├── error_policy.py     #   Budget limits & retry delay policy
│   │   ├── events.py           #   Event emitter for UI notifications
│   │   ├── history.py          #   Session persistence (JSON files in ~/.coderAI/history/)
│   │   ├── hooks_manager.py    #   Execution hooks manager
│   │   ├── locks.py            #   Async resource locks for parallel agent safety
│   │   ├── proc.py             #   Scrubbed subprocess / exec sandbox
│   │   ├── retry.py            #   Canonical backoff+jitter / async retry helpers
│   │   ├── trust.py            #   Workspace trust gate
│   │   ├── project_layout.py   #   Project folder detection helpers
│   │   ├── read_cache.py       #   Caching layer for repeated file reads
│   │   └── safeguards.py       #   Safety guards for commands & staging files
│   │
│   ├── context/                # ─── Context Window Management ───
│   │   ├── code_chunker.py     #   AST/regex/sliding-window code chunker for embedding
│   │   ├── code_indexer.py     #   ChromaDB-backed semantic code index
│   │   ├── context.py          #   Compatibility shim (pinned state lives in controller)
│   │   ├── context_controller.py # Token estimation, truncation, summarization, pins
│   │   └── context_selector.py #   Relevance-based snippet selection
│   │
│   ├── embeddings/             # ─── Embedding providers for semantic search ───
│   │   └── openai.py           #   OpenAI embeddings (text-embedding-3-small)
│   │
│   ├── tui/                    # ─── Textual interactive chat UI ───
│   │   ├── app.py              #   CoderAIApp (Textual screens, key bindings)
│   │   ├── controller.py       #   UIBridge: event_emitter ↔ UI on_event ↔ slash commands
│   │   ├── commands.py         #   UIBridge command handlers + /show reference text
│   │   ├── streaming.py        #   BridgeStreamingHandler → phased turn events
│   │   ├── tool_metadata.py    #   Tool category/risk/preview helpers
│   │   ├── listeners.py        #   EventReducer (agent events → timeline state)
│   │   ├── slash.py            #   Slash-command routing
│   │   ├── state.py            #   SessionState + AgentInfo dataclasses
│   │   ├── session_setup.py    #   Agent + UIBridge bootstrap
│   │   ├── help_menu.py        #   /help command catalog
│   │   └── diff_render.py      #   Compact diff rendering
│   │
│   ├── llm/                    # ─── LLM Provider Backends ───
│   │   ├── base.py             #   Abstract LLMProvider interface
│   │   ├── factory.py          #   create_provider(model, config)
│   │   ├── openai.py           #   OpenAI (gpt-5.4, o1, o3-mini)
│   │   ├── anthropic.py        #   Anthropic (Claude 4 Sonnet, 3.5 Sonnet, etc.)
│   │   ├── groq.py             #   Groq (Llama 3, GPT-OSS models)
│   │   ├── deepseek.py         #   DeepSeek (V3.2, R1)
│   │   ├── gemini.py           #   Google Gemini (OpenAI-compatible API)
│   │   ├── lmstudio.py         #   LM Studio (local OpenAI-compatible)
│   │   └── ollama.py           #   Ollama (local models)
│   │
│   └── tools/                  # ─── Agent Tool Implementations (96 total) ───
│       ├── base.py             #   Tool ABC + ToolRegistry
│       ├── discovery.py        #   Auto-discovery of no-arg Tool subclasses
│       ├── _detect.py          #   Shared walk-up project-tool detection
│       ├── filesystem/         #   read/write/edit/manage/metadata (+ _guards)
│       ├── terminal.py         #   run_command, run_background, list/kill_processes, read_bg_output
│       ├── git.py              #   git_add … git_tag, git_fetch (20 git tools)
│       ├── search.py           #   text_search, grep, symbol_search
│       ├── semantic_search.py  #   semantic_search (natural-language code search)
│       ├── web/                #   web_search, read_url, download_file, http_request, …
│       ├── browser.py          #   browser_navigate … browser_close (Playwright; optional)
│       ├── desktop.py          #   run_applescript, get_accessibility_tree, click/type (macOS only)
│       ├── memory.py           #   save_memory, recall_memory, delete_memory
│       ├── mcp.py              #   mcp_connect/disconnect/call_tool/list (+resources, prompts)
│       ├── undo.py             #   undo, undo_history
│       ├── project.py          #   project_context
│       ├── context_manage.py   #   manage_context (pin/unpin; manual registration)
│       ├── tasks.py            #   manage_tasks
│       ├── subagent.py         #   delegate_task
│       ├── jobs.py             #   start_job, job_status, job_result, wait_job, cancel_job
│       ├── lint.py / format.py #   lint, format (scrubbed subprocess from project root)
│       ├── testing.py          #   run_tests
│       ├── package_manager.py  #   package_manager (pip, npm, …)
│       ├── refactor.py         #   refactor (rename_symbol, find_references)
│       ├── vision.py           #   read_image
│       ├── skills.py           #   use_skill
│       ├── repl.py             #   python_repl
│       ├── planning.py         #   plan
│       └── notepad.py          #   notepad
│
├── docs/
│   ├── ARCHITECTURE.md         # Detailed architecture documentation
│   ├── CHAT_EVENTS.md          # Textual UI event catalog (UIBridge ↔ TUI)
│   ├── CLAUDE.md               # Contributor / LLM-oriented guide
│   ├── COMMANDS.md             # CLI command reference
│   ├── EXAMPLES.md             # Usage examples
│   ├── INSTALL.md              # Installation guide
│   └── REFACTOR_PLAN.md        # Historical refactoring roadmap
│
├── .coderAI/                   # ─── Project Configuration ───
│   ├── agents/                 #   17 agent personas (YAML frontmatter + markdown)
│   │   ├── planner.md          #     Planning specialist
│   │   ├── code-reviewer.md    #     Code review expert
│   │   ├── architect.md        #     Architecture analyst
│   │   ├── security-reviewer.md#     Security auditor
│   │   ├── chief-of-staff.md   #     Coordination / orchestration
│   │   ├── tdd-guide.md        #     Test-driven development guide
│   │   ├── python-reviewer.md  #     Python code reviewer
│   │   ├── go-reviewer.md      #     Go code reviewer
│   │   ├── database-reviewer.md#     Database/SQL reviewer
│   │   ├── doc-updater.md      #     Documentation specialist
│   │   ├── e2e-runner.md       #     End-to-end test runner
│   │   ├── build-error-resolver.md  # Build error debugger
│   │   ├── go-build-resolver.md#     Go build error specialist
│   │   ├── refactor-cleaner.md #     Refactoring specialist
│   │   ├── harness-optimizer.md#     Test harness optimizer
│   │   ├── loop-operator.md    #     Loop/iteration operator
│   │   └── test-planner.md     #     Test planning specialist
│   │
│   ├── skills/                 #   Reusable skill workflows (each dir has SKILLS.md)
│   │   ├── security-audit/     #     Step-by-step security audit
│   │   ├── tdd-workflow/       #     TDD workflow guide
│   │   ├── spotify-control/    #     Spotify control skill
│   │   └── test-skill/         #     Test skill template
│   │
│   ├── rules/                  #   Per-project coding rules (auto-injected into prompts)
│   │   ├── 001-common-principles.md  # TDD, security-first, tool usage
│   │   └── 101-python-standards.md   # Python-specific conventions
│   │
│   └── current_plan.json       #   Active execution plan (managed by plan tool)
│
└── tests/                      # ─── Test Suite (100+ modules) ───
    ├── test_coderAI.py         #   Comprehensive tool tests
    ├── test_agent.py           #   Agent orchestration tests
    ├── test_tool_registry_snapshot.py  # Pins the 94 auto-discovered tools
    ├── test_refactor.py        #   Refactor tool tests
    ├── security/               #   Red-team / security regression suite
    └── …
```

---

## 🔁 The Agentic Loop

The heart of CoderAI is the **agentic loop** in `coderAI/core/agent.py → process_message()`. Here is how every user message flows through the system:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. User sends message                                          │
│  2. Inject pinned context + project instructions                │
│  3. Context compaction when the usable context budget is full    │
│  4. ┌──────────────────── LOOP (max_iterations) ──────────────┐ │
│     │  a. Check cancellation flag                              │ │
│     │  b. Call LLM with messages + tool schemas                │ │
│     │     (with retry: up to 3 attempts, exponential backoff)  │ │
│     │  c. If NO tool calls → return final response → DONE      │ │
│     │  d. If tool calls:                                       │ │
│     │     • Parse all tool call arguments                      │ │
│     │     • Run pre-tool hooks (from hooks.json)               │ │
│     │     • Execute read-only tools in PARALLEL (asyncio)      │ │
│     │     • Execute mutating tools SEQUENTIALLY                │ │
│     │     • Run post-tool hooks                                │ │
│     │     • Summarize/truncate large results                   │ │
│     │     • Add tool results to session                        │ │
│     │     • Re-inject context, re-manage context window        │ │
│     │     • CONTINUE LOOP → back to (a)                        │ │
│     └──────────────────────────────────────────────────────────┘ │
│  5. Save session to disk                                        │
└─────────────────────────────────────────────────────────────────┘
```

### Key Loop Features

- **Retry with backoff** — Transient errors (429, 5xx, timeouts) are retried up to 3 times with exponential delay.
- **Consecutive error guard** — After 5 consecutive errors the loop halts gracefully.
- **Parallel tool execution** — Read-only tools run concurrently via `asyncio.gather()`; mutating tools run sequentially to prevent race conditions.
- **Context auto-compaction** — When estimated tokens exceed the usable context budget (`context_window` minus response and tool overhead), older messages are summarized by the LLM and replaced with a condensed summary.
- **Cooperative cancellation** — `AgentTracker` provides a cancel event; the loop checks it on every iteration.

---

## 🛠️ Tools Reference

CoderAI registers **96 tools** that the LLM can call (94 auto-discovered plus `manage_context` and `start_job`, which are registered manually). Each tool follows the `Tool` abstract base class. Browser, desktop, and some web tools are removed at runtime when optional dependencies or the host OS are unavailable — see notes below. Batch edits use `search_replace` with an `edits` list (there is no separate `multi_edit` tool).

### Filesystem (14 tools)

| Tool | Description |
|---|---|
| `read_file` | Read file contents with optional line range |
| `write_file` | Create or overwrite files (protected paths blocked) |
| `search_replace` | Find and replace text in a file with verification (batch via `edits`) |
| `apply_diff` | Apply a unified diff patch for multi-line edits |
| `list_directory` | List files and subdirectories |
| `glob_search` | Find files by glob pattern (`**/*.py`) |
| `move_file` | Move or rename a file or directory |
| `copy_file` | Copy a file or directory tree |
| `delete_file` | Delete a file or directory (recursive opt-in) |
| `create_directory` | Create directories including parents (`mkdir -p`) |
| `file_stat` | Get file metadata (size, permissions, timestamps) |
| `file_chmod` | Change file permissions |
| `file_chown` | Change file ownership |
| `file_readlink` | Read symlink targets | |

### Terminal (5 tools)

| Tool | Description |
|---|---|
| `run_command` | Execute shell commands (dangerous commands require confirmation) |
| `run_background` | Start long-running processes (servers, watchers) |
| `list_processes` | List background processes started by the agent |
| `kill_process` | Terminate a background process by PID |
| `read_bg_output` | Read buffered output from a `run_background` process |

### Git (20 tools)

| Tool | Description |
|---|---|
| `git_add` | Stage specific files for commit |
| `git_status` | Show working tree status |
| `git_diff` | View diffs (staged, unstaged, between refs) |
| `git_commit` | Create commits |
| `git_log` | View commit history |
| `git_branch` | List, create, or delete branches |
| `git_checkout` | Switch or create branches |
| `git_stash` | Stash/restore uncommitted changes |
| `git_push` | Push commits to remote (uses `--force-with-lease` for safety) |
| `git_pull` | Fetch and merge/rebase from remote |
| `git_merge` | Merge a branch into the current branch |
| `git_rebase` | Rebase onto another branch; supports `--abort`/`--continue` |
| `git_revert` | Create a revert commit (safe, doesn't rewrite history) |
| `git_reset` | Reset HEAD — soft / mixed / hard |
| `git_show` | Inspect a commit's message and diff |
| `git_remote` | List, add, remove, or update remotes |
| `git_blame` | Annotate file lines with commit and author |
| `git_cherry_pick` | Apply specific commits onto the current branch |
| `git_tag` | List, create, or delete tags |
| `git_fetch` | Fetch objects and refs from a remote |

### Search & Analysis (4 tools)

*`semantic_search` requires optional `chromadb` — install with `pip install coderAI[semantic]` (plus an OpenAI key for embeddings).*

| Tool | Description |
|---|---|
| `text_search` | Fast recursive text search across files |
| `grep` | Regex pattern matching with context lines |
| `symbol_search` | Find function/class/variable definitions by name |
| `semantic_search` | Natural-language code search via embeddings (requires OpenAI key + `coderAI[semantic]`) |

### Web & HTTP (7 tools)

*PDF extraction in `read_url` requires optional `pypdf` — install with `pip install coderAI[web]`.*

| Tool | Description |
|---|---|
| `web_search` | Web search (DuckDuckGo and other backends) with optional content fetching |
| `read_url` | Fetch and extract text from any URL (HTML or PDF with `pypdf`) |
| `download_file` | Download files (ZIP, images, etc.) from URLs |
| `http_request` | Generic HTTP client — any method, headers, JSON body (SSRF-protected) |
| `wikipedia_search` | Search Wikipedia and return article summaries |
| `read_feed` | Parse RSS/Atom feeds from a URL |
| `sitemap_discover` | Discover pages via `sitemap.xml` / `robots.txt` |

### Memory (3 tools)

| Tool | Description |
|---|---|
| `save_memory` | Store key-value data persistently across sessions |
| `recall_memory` | Retrieve or search saved memories |
| `delete_memory` | Remove a memory entry by key |

### Project & Context (2 tools)

| Tool | Description |
|---|---|
| `project_context` | Auto-detect project type, deps, and structure |
| `manage_context` | Pin/unpin files to the LLM context window |

### Planning & Tasks (2 tools)

| Tool | Description |
|---|---|
| `plan` | Create/show/advance/update/clear structured execution plans |
| `manage_tasks` | Persistent TODO list with priorities |

### Multi-Agent (2 tools)

| Tool | Description |
|---|---|
| `delegate_task` | Spawn an isolated sub-agent for complex tasks |
| `notepad` | Shared notepad for inter-agent communication |

### Background Jobs (5 tools)

Run long tools detached from the turn so the agent keeps working while they run. Only tools marked *backgroundable* (`run_tests`, `package_manager`, `lint`, `download_file`) may be targets; `start_job` always confirms (one approval covers the whole unattended job) and jobs are capped by `max_background_jobs`.

| Tool | Description |
|---|---|
| `start_job` | Submit a backgroundable tool as a detached job; returns a `job_id` immediately |
| `job_status` | Check one job (or list all): queued/running/done/failed/timeout/cancelled |
| `job_result` | Collect a finished job's full result payload |
| `wait_job` | Block up to a bounded timeout until a job finishes |
| `cancel_job` | Cancel a queued or running job |

### Code Quality (3 tools)

| Tool | Description |
|---|---|
| `lint` | Auto-detect and run project linter (ruff, eslint, clippy, etc.) |
| `format` | Auto-detect and run code formatter (ruff format, black, prettier, gofmt) |
| `run_tests` | Auto-detect and run the project test runner (pytest, jest, cargo test, etc.) |

### Refactoring (1 tool)

| Tool | Description |
|---|---|
| `refactor` | Cross-file `rename_symbol` and `find_references` (Python AST-aware; JS/TS regex-based). Writes go through the full `write_file` pipeline (locks, guards, backup, atomic write); partial failures report `files_skipped`. Use `dry_run=true` first. |

### Package Management (1 tool)

| Tool | Description |
|---|---|
| `package_manager` | Install, remove, or list packages (pip, npm, cargo, etc.) |

### Code Execution (1 tool)

| Tool | Description |
|---|---|
| `python_repl` | Execute Python code in an isolated subprocess |

### Vision (1 tool)

| Tool | Description |
|---|---|
| `read_image` | Read and base64-encode images for multimodal analysis |

### Skills (1 tool)

| Tool | Description |
|---|---|
| `use_skill` | Load predefined skill workflows from `.coderAI/skills/` |

### Browser Automation (10 tools)

*Requires `playwright` — install with `pip install coderAI[browser] && playwright install chromium`.*

Browser tools provide full control over a headless Chromium browser for form filling, shopping, data entry, and web scraping. They use an **accessibility snapshot** pattern: navigate → snapshot (get element refs like `[e12]`) → click/type by ref → repeat.

| Tool | Description |
|---|---|
| `browser_navigate` | Navigate to a URL — returns page title and final URL |
| `browser_snapshot` | Capture the accessibility tree with element refs (`[e0]`, `[e1]`, ...) |
| `browser_click` | Click an element by its snapshot ref |
| `browser_type` | Type text into an input field by ref (set `clear=true` to replace) |
| `browser_select_option` | Select an option from a dropdown/combobox by ref |
| `browser_get_content` | Extract page content as markdown, plain text, or raw HTML |
| `browser_screenshot` | Take a PNG screenshot of the current page viewport |
| `browser_evaluate` | Execute JavaScript in the page context and return the result |
| `browser_wait` | Wait for text to appear or a timeout duration |
| `browser_close` | Close the browser and free resources |

**Workflow example:**
```
1. browser_navigate("https://example.com/form")
2. browser_snapshot()              → "textbox 'Email' [e5], button 'Submit' [e9]"
3. browser_type(ref="e5", text="user@example.com")
4. browser_click(ref="e9")
5. browser_snapshot()              → "heading 'Thank you!' [e1]"
6. browser_get_content()           → confirmation page text
7. browser_close()
```

### Desktop Automation (macOS only, 4 tools)

| Tool | Description |
|---|---|
| `run_applescript` | Execute AppleScript or JXA on the macOS host |
| `get_accessibility_tree` | Retrieve the macOS accessibility UI tree as JSON |
| `click_ui_element` | Click a UI element via AppleScript System Events |
| `type_keystrokes` | Simulate typing or key presses on macOS |

### MCP Integration (8 tools)

| Tool | Description |
|---|---|
| `mcp_connect` | Connect to an external MCP server |
| `mcp_disconnect` | Disconnect from an MCP server |
| `mcp_call_tool` | Call a tool on a connected MCP server |
| `mcp_list` | List connected servers and their tools, resources, and prompts |
| `mcp_list_resources` | List resources exposed by a connected MCP server |
| `mcp_read_resource` | Read a resource (by URI) from a connected MCP server |
| `mcp_list_prompts` | List prompt templates exposed by a connected MCP server |
| `mcp_get_prompt` | Fetch a prompt template (with arguments) from a server |

### Undo / Rollback (2 tools)

| Tool | Description |
|---|---|
| `undo` | Revert the last file modification |
| `undo_history` | View recent file change history |

---

## 🤖 Agent System

### Agent Personas

CoderAI supports **17 specialist agent personas** defined as Markdown files with YAML frontmatter in `.coderAI/agents/`. Each persona has:

- **`name`** — Identifier used for `/agent` or delegated persona selection
- **`description`** — What the agent specializes in
- **`tools`** — High-level tool labels (for example `Read`, `Edit`, `Bash`) that expand into concrete runtime tools; read-only tools remain available for codebase inspection
- **`model`** — Preferred LLM model
- **Instructions** — Full system prompt in markdown body

| Persona | Specialty |
|---|---|
| `planner` | Implementation planning for complex features |
| `code-reviewer` | Code quality, correctness, and conventions |
| `architect` | Architecture analysis and design |
| `security-reviewer` | Security vulnerability analysis |
| `chief-of-staff` | Coordination and orchestration |
| `tdd-guide` | Test-driven development guidance |
| `python-reviewer` | Python-specific code review |
| `go-reviewer` | Go-specific code review |
| `database-reviewer` | Database and SQL review |
| `doc-updater` | Documentation maintenance |
| `e2e-runner` | End-to-end test execution |
| `build-error-resolver` | Build error diagnosis and fixing |
| `go-build-resolver` | Go build error specialist |
| `refactor-cleaner` | Refactoring specialist |
| `harness-optimizer` | Test harness optimization |
| `loop-operator` | Iterative loop operations |
| `test-planner` | Test strategy planning |

### Sub-Agent Delegation

The `delegate_task` tool spawns **isolated sub-agents** in their own sessions. The `agent_role` can be an exact persona file name such as `security-reviewer` or a natural alias such as `Code Reviewer`; when it resolves to a persona, the sub-agent inherits that persona's prompt and mutating-tool policy.

```
Parent Agent
│
├── delegate_task("Review auth module", role="security-reviewer")
│   └── Sub-Agent (security-reviewer persona)
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

Skills are predefined step-by-step workflows stored in `.coderAI/skills/<name>/SKILLS.md`:

| Skill | Description |
|---|---|
| `security-audit` | 5-step security review (credentials, injection, auth, deps, logging) |
| `tdd-workflow` | Test-driven development workflow guide |
| `spotify-control` | Control Spotify playback from the agent |
| `test-skill` | Template/test skill |

Use them via the `use_skill` tool:
```
> Use the security-audit skill to review the auth module
```

### Planning Tool

The `plan` tool provides structured multi-step execution:

```
> Create a plan to add user authentication

Plan "Add User Authentication" created with 5 steps:
  [0] ✅ Set up database schema       — done
  [1] 🔄 Create auth middleware       — in progress
  [2] ⬜ Build login/register routes  — pending
  [3] ⬜ Add session management       — pending
  [4] ⬜ Write tests                  — pending

Progress: 1/5 steps completed
```

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
| **OpenAI** | `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `o1`, `o1-mini`, `o3-mini` | `OPENAI_API_KEY` |
| **Anthropic** | `claude-4-sonnet`, `claude-3.5-sonnet`, `claude-3.5-haiku`, `claude-3-opus` | `ANTHROPIC_API_KEY` |
| **Groq** | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `llama3-70b-8192`, `llama3-8b-8192` | `GROQ_API_KEY` |
| **DeepSeek** | `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v3.2`, `deepseek-r1` | `DEEPSEEK_API_KEY` |
| **Gemini** | `gemini-3.5-flash`, `gemini-3.1-pro`, `gemini-2.5-flash`, `gemini-2.5-pro`, … | `GEMINI_API_KEY` |
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
| `max_background_jobs` | `3` | Concurrent `start_job` background jobs (global only — not project-overridable) |
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
python -m ruff check coderAI/

# Run the full test suite
pytest

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
| `coderAI mcp list` / `add` / `remove` | Manage MCP servers (also `login` / `logout` / `resources` / `prompts`) |
| `coderAI setup` | Interactive setup wizard |
| `coderAI doctor` | Diagnose install (config, keys, dependencies) |
| `coderAI models` | List available models and providers |
| `coderAI set-model <name>` | Set default model |
| `coderAI config show` | Show configuration |
| `coderAI config set <k> <v>` | Set a configuration value |
| `coderAI config reset` | Reset to defaults |
| `coderAI history list` | List all sessions |
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

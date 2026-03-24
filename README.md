<p align="center">
  <h1 align="center">🤖 CoderAI</h1>
  <p align="center"><strong>An autonomous, multi-agent coding assistant that lives in your terminal.</strong></p>
  <p align="center">
    <a href="#installation">Install</a> · <a href="#quick-start">Quick Start</a> · <a href="#architecture">Architecture</a> · <a href="#tools-reference">Tools</a> · <a href="#agent-system">Agents</a> · <a href="#workflows--skills">Workflows</a>
  </p>
</p>

---

CoderAI is a Python CLI tool that pairs an LLM with **35+ built-in tools** to read, write, search, debug, test, and ship code — all from a single terminal session. It supports **6 LLM providers**, **17 specialist agent personas**, a **multi-agent delegation system** with retry logic, and a **plan-and-execute workflow** to tackle complex tasks autonomously.

## ✨ Key Features

| Feature | Description |
|---|---|
| **Multi-Provider LLM** | OpenAI, Anthropic Claude, Groq, DeepSeek, LM Studio, Ollama |
| **35+ Tools** | File I/O, Git, terminal, web search, linting, image reading, MCP, and more |
| **Multi-Agent System** | Spawn isolated sub-agents for code review, security audit, research, etc. |
| **Planning & Tasks** | Structured plan-and-execute workflows with persistent task tracking |
| **Rich Terminal UI** | Syntax-highlighted streaming with markdown rendering via [Rich](https://github.com/Textualize/rich) |
| **Context Management** | Pin files, auto-detect project type, smart context compaction |
| **Persistent Memory** | Key-value store that survives across sessions |
| **Undo / Rollback** | Revert any file modification instantly |
| **MCP Integration** | Connect to external Model Context Protocol servers |
| **Skills & Rules** | Reusable skill workflows and per-project coding rules |
| **Cost Tracking** | Real-time token and cost accounting with budget limits |
| **Hooks** | Pre/post tool execution hooks via `.coderAI/hooks.json` |

---

## 📦 Installation

**Requirements:** Python 3.9+

```bash
# Clone the repository
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI

# Install in editable mode
pip install -e .

# Run the setup wizard to configure API keys
coderAI setup
```

See [INSTALL.md](INSTALL.md) for detailed installation instructions and troubleshooting.

---

## 🚀 Quick Start

```bash
# Interactive chat (default)
coderAI

# or explicitly
coderAI chat

# Single-shot mode
coderAI chat -m claude-4-sonnet

# Resume a previous session
coderAI chat --resume <session-id>

# List available models
coderAI models

# Check system status
coderAI status
```

### Interactive Commands (inside chat)

| Command | Description |
|---|---|
| `/help` | Show available commands |
| `/model` | Change the LLM model |
| `/tokens` | Show token usage and cost |
| `/context` | Show pinned context files |
| `/compact` | Force-compress conversation history |
| `/agents` | Show active agents and sub-agents |
| `/auto-approve` | Toggle tool confirmation prompts |
| `/clear` | Clear conversation history |
| `/exit` | End the session |

---

## 🏗️ Architecture

### High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                          CLI Layer                                │
│           coderAI/cli.py  —  Click commands & entry points       │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────┴───────────────────────────────────────┐
│                         Agent Layer                               │
│                       coderAI/agent.py                            │
│  • Agentic loop (process_message → LLM → tools → LLM → ...)     │
│  • Context window management with auto-summarization             │
│  • Retry logic with exponential backoff                          │
│  • Pre/Post tool hooks                                           │
│  • Cooperative cancellation via AgentTracker                     │
└───────┬──────────────┬───────────────┬──────────────┬────────────┘
        │              │               │              │
   ┌────┴────┐   ┌─────┴──────┐  ┌────┴─────┐  ┌────┴────────┐
   │   LLM   │   │   Tools    │  │    UI    │  │  Sub-Agent  │
   │Providers│   │  Registry  │  │Components│  │  Delegation │
   │ (6)     │   │  (35+)     │  │  (Rich)  │  │  (Isolated) │
   └─────────┘   └────────────┘  └──────────┘  └─────────────┘
```

---

## 📁 Project Structure Tree

```
CoderAI-main/
├── pyproject.toml              # Package metadata, dependencies, entry point
├── requirements.txt            # Pinned dependencies
├── Makefile                    # Dev shortcuts (test, lint, install)
├── LICENSE                     # MIT License
├── README.md                   # ← You are here
├── ARCHITECTURE.md             # Detailed architecture documentation
├── COMMANDS.md                 # CLI command reference
├── EXAMPLES.md                 # Usage examples
├── INSTALL.md                  # Installation guide
├── CLAUDE.md                   # LLM-specific instructions
│
├── coderAI/                    # ─── Main Python Package ───
│   ├── __init__.py             # Package version
│   ├── cli.py                  # Click CLI: chat, config, history, models, setup, status, cost, tasks
│   ├── agent.py                # Core orchestrator: agentic loop, context mgmt, retry, hooks
│   ├── agents.py               # AgentPersona loader from .coderAI/agents/*.md
│   ├── agent_tracker.py        # Real-time agent registry, cancellation, observability
│   ├── config.py               # Pydantic config with JSON persistence (~/.coderAI/config.json)
│   ├── context.py              # Pinned-file context manager with relevance filtering
│   ├── context_selector.py     # Keyword extraction & relevance-based snippet selection
│   ├── cost.py                 # Token cost tracking with per-model pricing
│   ├── events.py               # Event emitter for UI notifications
│   ├── history.py              # Session persistence (JSON files in ~/.coderAI/history/)
│   ├── locks.py                # Async resource locks for parallel agent safety
│   ├── notepad.py              # Shared in-memory notepad for inter-agent communication
│   ├── skills.py               # Skill loader from .coderAI/skills/*.md
│   ├── system_prompt.py        # Default system prompt with tool docs & strategies
│   │
│   ├── llm/                    # ─── LLM Provider Backends ───
│   │   ├── base.py             #   Abstract LLMProvider interface
│   │   ├── openai.py           #   OpenAI (GPT-5, o1, o3-mini)
│   │   ├── anthropic.py        #   Anthropic (Claude 4 Sonnet, 3.5 Sonnet, etc.)
│   │   ├── groq.py             #   Groq (Llama 3, GPT-OSS models)
│   │   ├── deepseek.py         #   DeepSeek (V3.2, R1)
│   │   ├── lmstudio.py         #   LM Studio (local OpenAI-compatible)
│   │   └── ollama.py           #   Ollama (local models)
│   │
│   ├── tools/                  # ─── MCP Tool Implementations ───
│   │   ├── base.py             #   Tool ABC + ToolRegistry
│   │   ├── filesystem.py       #   read_file, write_file, search_replace, apply_diff, list_directory, glob_search
│   │   ├── terminal.py         #   run_command, run_background (with safety blocklist)
│   │   ├── git.py              #   git_add, git_status, git_diff, git_commit, git_log, git_branch, git_checkout, git_stash
│   │   ├── search.py           #   text_search, grep (regex-capable)
│   │   ├── web.py              #   web_search (DuckDuckGo), read_url, download_file
│   │   ├── memory.py           #   save_memory, recall_memory (persistent key-value)
│   │   ├── mcp.py              #   mcp_connect, mcp_call_tool, mcp_list
│   │   ├── undo.py             #   undo, undo_history (file backup/rollback)
│   │   ├── project.py          #   project_context (auto-detect project type)
│   │   ├── context_manage.py   #   manage_context (pin/unpin files)
│   │   ├── tasks.py            #   manage_tasks (persistent TODO list)
│   │   ├── subagent.py         #   delegate_task (spawn isolated sub-agents)
│   │   ├── lint.py             #   lint (auto-detect & run linter)
│   │   ├── vision.py           #   read_image (base64 encoding for multimodal)
│   │   ├── skills.py           #   use_skill (load skill workflows)
│   │   ├── repl.py             #   python_repl (isolated subprocess execution)
│   │   ├── planning.py         #   plan (create/show/advance/update/clear)
│   │   └── notepad.py          #   notepad (shared inter-agent notepad)
│   │
│   └── ui/                     # ─── Terminal UI (Rich) ───
│       ├── display.py          #   Markdown, syntax, tables, trees, panels
│       ├── interactive.py      #   Interactive chat loop with prompt-toolkit
│       └── streaming.py        #   Live streaming display handler
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
│   ├── skills/                 #   Reusable skill workflows
│   │   ├── security-audit.md   #     Step-by-step security audit
│   │   ├── tdd-workflow.md     #     TDD workflow guide
│   │   └── test-skill.md       #     Test skill template
│   │
│   ├── rules/                  #   Per-project coding rules (auto-injected into prompts)
│   │   ├── 001-common-principles.md  # TDD, security-first, tool usage
│   │   └── 101-python-standards.md   # Python-specific conventions
│   │
│   └── current_plan.json       #   Active execution plan (managed by plan tool)
│
└── tests/                      # ─── Test Suite ───
    ├── test_coderAI.py         #   Comprehensive tool tests
    ├── test_agent.py           #   Agent orchestration tests
    ├── test_integration.py     #   End-to-end integration tests
    ├── test_web.py             #   Web tool tests
    ├── test_streaming.py       #   Streaming handler tests
    ├── test_context.py         #   Context manager tests
    ├── test_context_manage.py  #   Context management tool tests
    ├── test_git_extended.py    #   Extended Git tool tests
    ├── test_notepad.py         #   Notepad tool tests
    ├── test_planning.py        #   Planning tool tests
    ├── test_repl.py            #   Python REPL tool tests
    └── test_skills.py          #   Skills tool tests
```

---

## 🔁 The Agentic Loop

The heart of CoderAI is the **agentic loop** in `agent.py → process_message()`. Here is how every user message flows through the system:

```
┌─────────────────────────────────────────────────────────────────┐
│  1. User sends message                                          │
│  2. Inject pinned context + project instructions                │
│  3. Proactive context compaction (if >70% of context window)    │
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
- **Consecutive error guard** — After 3 consecutive errors the loop halts gracefully.
- **Parallel tool execution** — Read-only tools run concurrently via `asyncio.gather()`; mutating tools run sequentially to prevent race conditions.
- **Context auto-compaction** — When token usage exceeds 70% of the context window, older messages are summarized by the LLM and replaced with a condensed summary.
- **Cooperative cancellation** — `AgentTracker` provides a cancel event; the loop checks it on every iteration.

---

## 🛠️ Tools Reference

CoderAI registers **35+ tools** that the LLM can call. Each tool follows the `Tool` abstract base class and is auto-registered in the `ToolRegistry`.

### Filesystem (6 tools)

| Tool | Description |
|---|---|
| `read_file` | Read file contents with optional line range |
| `write_file` | Create or overwrite files (protected paths blocked) |
| `search_replace` | Find and replace text in a file with verification |
| `apply_diff` | Apply a unified diff patch for multi-line edits |
| `list_directory` | List files and subdirectories |
| `glob_search` | Find files by glob pattern (`**/*.py`) |

### Terminal (2 tools)

| Tool | Description |
|---|---|
| `run_command` | Execute shell commands (dangerous commands require confirmation) |
| `run_background` | Start long-running processes (servers, watchers) |

### Git (8 tools)

| Tool | Description |
|---|---|
| `git_add` | Stage files |
| `git_status` | Show working tree status |
| `git_diff` | View diffs (staged, unstaged, between refs) |
| `git_commit` | Create commits |
| `git_log` | View commit history |
| `git_branch` | List, create, or delete branches |
| `git_checkout` | Switch or create branches |
| `git_stash` | Stash/restore uncommitted changes |

### Search & Analysis (3 tools)

| Tool | Description |
|---|---|
| `text_search` | Fast recursive text search across files |
| `grep` | Regex pattern matching with context lines |
| `lint` | Auto-detect and run project linter (ruff, eslint, etc.) |

### Web (3 tools)

| Tool | Description |
|---|---|
| `web_search` | DuckDuckGo search with optional content fetching |
| `read_url` | Fetch and extract text from any URL |
| `download_file` | Download files (ZIP, images, etc.) from URLs |

### Memory (2 tools)

| Tool | Description |
|---|---|
| `save_memory` | Store key-value data persistently across sessions |
| `recall_memory` | Retrieve or search saved memories |

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

### MCP Integration (3 tools)

| Tool | Description |
|---|---|
| `mcp_connect` | Connect to an external MCP server |
| `mcp_call_tool` | Call a tool on a connected MCP server |
| `mcp_list` | List connected servers and their tools |

### Undo / Rollback (2 tools)

| Tool | Description |
|---|---|
| `undo` | Revert the last file modification |
| `undo_history` | View recent file change history |

---

## 🤖 Agent System

### Agent Personas

CoderAI supports **17 specialist agent personas** defined as Markdown files with YAML frontmatter in `.coderAI/agents/`. Each persona has:

- **`name`** — Identifier used for delegation
- **`description`** — What the agent specializes in
- **`tools`** — Whitelist of allowed tools (read-only tools are always available)
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

The `delegate_task` tool spawns **isolated sub-agents** in their own sessions:

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

Skills are predefined step-by-step workflows stored in `.coderAI/skills/*.md`:

| Skill | Description |
|---|---|
| `security-audit` | 5-step security review (credentials, injection, auth, deps, logging) |
| `tdd-workflow` | Test-driven development workflow guide |
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
| **OpenAI** | `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `o1`, `o1-mini`, `o3-mini` | `OPENAI_API_KEY` |
| **Anthropic** | `claude-4-sonnet`, `claude-3.5-sonnet`, `claude-3.5-haiku`, `claude-3-opus` | `ANTHROPIC_API_KEY` |
| **Groq** | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `llama3-70b-8192`, `llama3-8b-8192` | `GROQ_API_KEY` |
| **DeepSeek** | `deepseek-v3.2`, `deepseek-r1` | `DEEPSEEK_API_KEY` |
| **LM Studio** | Any local model | LM Studio running locally |
| **Ollama** | Any local model | Ollama running locally |

All providers implement the `LLMProvider` interface: `chat()`, `stream()`, `count_tokens()`, `supports_tools()`.

---

## ⚙️ Configuration

Configuration is stored in `~/.coderAI/config.json` and managed via `coderAI config` or `coderAI setup`.

| Key | Default | Description |
|---|---|---|
| `default_model` | `gpt-5-mini` | Default LLM model |
| `temperature` | `0.7` | Sampling temperature |
| `max_tokens` | `8192` | Max output tokens |
| `context_window` | `128000` | Context window size |
| `max_iterations` | `50` | Max agentic loop iterations |
| `reasoning_effort` | `medium` | Reasoning depth (`high`/`medium`/`low`/`none`) |
| `streaming` | `true` | Enable streaming responses |
| `save_history` | `true` | Persist conversation sessions |
| `budget_limit` | `0` | Max cost in USD (0 = unlimited) |

---

## 🧪 Testing

```bash
# Run the full test suite
pytest

# Run specific test categories
pytest tests/test_agent.py
pytest tests/test_web.py

# Validate installation
python test_installation.py
```

---

## 📄 CLI Commands

| Command | Description |
|---|---|
| `coderAI` / `coderAI chat` | Start interactive chat |
| `coderAI chat -m <model>` | Chat with specific model |
| `coderAI chat --resume <id>` | Resume a previous session |
| `coderAI setup` | Interactive setup wizard |
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

---

## 🧩 Extending CoderAI

### Adding a New Tool

```python
from coderAI.tools.base import Tool

class MyCustomTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    is_read_only = True  # Set False if the tool mutates state

    def get_parameters(self):
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Input value"}
            },
            "required": ["input"]
        }

    async def execute(self, input: str, **kwargs):
        return {"success": True, "result": f"Processed: {input}"}

# Register in agent.py → _create_tool_registry()
registry.register(MyCustomTool())
```

### Adding a New Agent Persona

Create `.coderAI/agents/my-specialist.md`:

```markdown
---
name: my-specialist
description: Expert in my domain
tools: ["read_file", "grep", "run_command"]
model: claude-4-sonnet
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

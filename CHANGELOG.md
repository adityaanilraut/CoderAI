# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Discovery & Search Subsystem (`glob` / `grep`)**: First-class workspace discovery with bundled ripgrep binary (`coderai/vendor/rg`), automatic Python fallback, result caps (100 glob / 250 grep), and spill-to-disk locators.
- **Output Spill Management (`coderai.core.spill`)**: Session-scoped spill store for oversized tool results (bash, search, terminal), providing head/tail previews and locator retrieval hints.
- **OS Sandboxing & Security Presets (`coderai.core.sandbox`, `coderai.core.permissions`)**:
  - 3 permission presets: `read-only`, `workspace-write`, and `danger-full-access`.
  - OS-level sandboxing via macOS Seatbelt (`sandbox-exec`) and Linux Bubblewrap (`bwrap`).
  - `/permission` slash command for viewing and switching presets dynamically.
  - PreToolUse hooks loaded from `.coderai/hooks.json`.
- **Continuable Subagent Control Plane (`coderai.core.agents`)**:
  - Background subagent orchestration with `subagent`, `send_message`, `interrupt_agent`, and `list_agents`.
  - One-shot child agent task delegation (`subagent_fork` / `Task`).
  - Child-only `report` tool and configurable depth cap.
- **Agent Teams Swarm & Shared Task Board (`coderai.core.teams`)**:
  - Multi-agent collaboration with `spawn_teammate`, `team_task_create`, `team_task_update`, `team_task_get`, `team_task_list`, and `wait_agent`.
- **Developer Subsystems**:
  - **Language Server Protocol (`coderai.core.lsp`)**: Definition lookup, references, document symbols, and workspace symbols (`lsp`).
  - **PTY Terminal Subsystem (`coderai.core.terminal`)**: Persistent pseudoterminals (`terminal_create`, `terminal_send`, `terminal_read`, `terminal_close`, `terminal_list`).
  - **Workflow Engine (`coderai.core.workflow`)**: Multi-phase async Python workflow scripting engine with structured logging and parallel branches (`workflow`).
  - **Ralph Verification Harness (`coderai.core.tools.ralph`)**: Automated test-driven feedback and task completion verification loop (`ralph`).
  - **Code Mode (`coderai.core.code_mode`)**: Sandboxed in-process Python execution for fast data manipulation (`code_mode`).
  - **Session Query & Search (`coderai.core.session_query`)**: Full-text indexing and search across session history (`session_query`).
  - **Cross-Platform PowerShell (`coderai.core.tools.pwsh`)**: PowerShell command execution on Windows, Linux, and macOS (`pwsh`).
  - **Scheduling Subsystem (`coderai.core.schedule`)**: One-shot timers and recurring cron jobs (`schedule_create`, `schedule_list`, `schedule_delete`).
- **Goals & Structured Todos (`coderai.core.goals`, `coderai.core.tools.todo_write`)**: Session goals management (`/goal`) and structured todo items wrapping plan updates.
- **Web & MCP Enhancements**:
  - Pluggable web search backends (`coderai.core.web_providers`).
  - `WebFetch` tool with SSRF protection, private IP filtering, and same-origin redirect policies.
  - MCP `streamable-http` transport alongside `stdio` and `sse`.
- **Append-Only Event Stream & Message Derivation (`coderai.core.session_log`)**:
  - Session events stored as append-only JSONL entries.
  - Compaction writes a `compact/summary` event without rewriting historical rows.
  - LLM retry-as-new-turn for transient rate limits and transport failures.

### Fixed
- **MCP Configuration & Server Merging**: Preserves `url`, `headers`, `cwd`, `disabled`, `enabled`, and `allowPrivateIps` across user and workspace configurations.
- **Tool Catalog & Prompt Section Stability**: System prompt composition uses deterministic tool ordering and dynamic capability detection.
- **Model List & Frontier Routing Alignment**: Aligned `CURATED_MODELS` with frontier LLMs (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gemini-3.7-flash`, `deepseek-v4-pro`, `deepseek-v4-flash`).

## [0.4.0] - 2026-08-18

### Fixed
- **Process Tree & Subprocess Isolation**: Hardened `kill_process_tree` to isolate subprocess process groups and prevent signal leakage to parent runners on POSIX.
- **MCP Lifecycle & Disabled Server Handling**: Added strict verification for `disabled: true` MCP server configurations in `prepare` and `initialize`, preventing unwanted connections.
- **Session Serialization & IDE Protocol Parity**: Added native `to_dict()` methods to `SessionEntry` and `SessionMessage`, empty session creation, and direct permission resolution helpers.
- **Type Safety & Code Quality**: Fully resolved static type checks across core modules with strict Mypy and Ruff validation.

## [0.3.5] - 2026-08-17

### Added
- **Multi-line & Advanced Input Buffering Engine**: Support for multi-line code block entry (` ``` ` triple backtick blocks), trailing backslash (`\`) line continuations, and paste buffering without premature turn submission.
- **Fuzzy Subsequence Completion & Ranking**: Integrated fuzzy matching engine across slash commands (`/undo`, `/compact`, `/history`), subcommands (`/mcp reconnect`, `/thinking summary`), `@file` workspace suggestions, model selection, and session search.
- **Dynamic Context Window Gauges**: Accurate real-time token % gauges in status bar with green (<60%), yellow (60-80%), and red (>80%) context utilization indicators.
- **Headless JSON-RPC 2.0 Server Mode & IDE Companion Bridge**: Added `coderai serve` / `coderai --server` protocol for editor extensions with streaming event notifications (`turn_start`, `stream_chunk`, `tool_call`, `tool_result`, `ask_permission`, `ask_question`, `turn_finish`).
- **Dual CLI Entry Point**: Registered official shorthand alias `cai` alongside `coderai`.
- **Complete Interactive Slash Commands Suite**: Added `/fork`, `/delete` (`/rm`), `/mcp`, `/tokens` (`/cost`), `/config` (`/settings`), `/history`, `/export`, `/compact`, `/thinking`, and `/clear`.
- **Readline Autocompletion & Persistent History**: Full `readline` integration with tab completion for slash commands, models, and `@file` workspace paths, plus persistent prompt history across sessions in `~/.coderai/history`.
- **Session Export Utilities**: Export active session conversations with reasoning traces, tool results, and metrics to clean GitHub-Flavored Markdown or JSON files via `/export`.
- **Dedicated Rich Tool Result Cards**: Added styled tool cards for `bash` (command, exit status, terminal panel), `WebSearch` (structured search results), `read` (anchored snippets and line counts), and `mcp` tools.
- **Enhanced Status Bar & Interactive Menus**: Dynamic status bar with turn counters and MCP server status indicators; interactive MCP inspector, config viewer, token analytics breakdown, session timeline, and multi-action session manager (resume, delete, fork).

## [0.3.4] - 2026-08-17

### Added
- **UI-Agnostic Core Engine**: Decoupled engine architecture (`coderai.core`) from interactive terminal UI presentation (`coderai.cli`).
- **Snippet-Scoped File Editing**: Scoped file editing (`read` generates anchored `snippet_id`, `edit` targets verified snippet with staleness detection).
- **Bounded Agent Loop**: Deterministic agent loop (`stream -> tool_calls -> permissions -> execute -> loop`) with turn loop-guards and token compaction.
- **Granular Side-Effect Permissions**: Granular permission scopes (`read-in-cwd`, `read-out-cwd`, `write-in-cwd`, `write-out-cwd`, `delete-in-cwd`, `delete-out-cwd`, `query-git-log`, `mutate-git-log`, `network`, `mcp`) with `allow`, `ask`, and `deny` rules.
- **GitFileHistory Checkpointing & Multi-Turn Undo**: Automatic turn checkpointing, unified diff inspection (`/diff`), and instant rollback (`/undo`).
- **Interactive Menus & Commands**: Rich interactive model selector (`/model`), sessions browser (`/sessions`), skills explorer (`/skills`), and Plan Mode toggle (`/plan`).
- **Frontier LLM Support & Auto-Routing**: Native compatibility with OpenAI (`gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-4.5-preview`, `o3-mini`), Anthropic (`claude-3-7-sonnet`), DeepSeek (`deepseek-v4-pro`, `deepseek-r1`, `deepseek-v3`), and Google Gemini (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.0-flash`) with reasoning / thinking token rendering.
- **Dynamic MCP Client**: Stdio Model Context Protocol client and dynamic tool manager.
- **Context Expansion via File Mentions**: `@filename` and `@path/to/file` automatic prompt context expansion.
- **Subagent Task Delegation**: Isolated subagent execution with specialized personas and scratch workspaces.
- **Rich Terminal UI Components**: ASCII brand banner, dynamic token status bar, thinking block cards, tool execution cards, diff syntax highlighting, and exit summary metrics.

### Changed
- Minimal core dependencies pruned to `rich`, `openai`, `requests`, and `pyyaml`.
- Standardized configuration format in `~/.coderai/settings.json` and `.coderai/settings.json`.
- Streamlined tool surface: `read`, `edit`, `write`, `bash`, `AskUserQuestion`, `UpdatePlan`, `WebSearch`, `UnderstandImage`, and `subagent`.

## [0.3.3] - 2026-07-15

### Added
- Modular system-prompt composition and runtime context builder.
- Dedicated skills frontmatter loader and resource file scanner.
- Background process tracking with timeout enforcement in bash tool.

### Changed
- CLI and REPL initialization moved to asynchronous execution pipeline.

## [0.3.2] - 2026-07-15

### Changed
- Distribution package renamed to `coderai-agent` on PyPI (`pip install coderai-agent`).

## [0.3.0] - 2026-07-07

### Added
- Initial session persistence with JSONL event streaming.
- Side-effect permission checks for file and terminal tools.

[0.4.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.4.0
[0.3.5]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.5
[0.3.4]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.4
[0.3.3]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.3
[0.3.2]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.2
[0.3.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.0

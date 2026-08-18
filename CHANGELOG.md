# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **Frontier LLM Support & Auto-Routing**: Native compatibility with OpenAI (`gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-4o`), Anthropic (`claude-3-7-sonnet`, `claude-3-5-sonnet`), DeepSeek (`deepseek-v4-pro`, `deepseek-r1`), and Google Gemini (`gemini-2.5-pro`, `gemini-2.5-flash`) with reasoning / thinking token rendering.
- **Dynamic MCP Client**: Stdio Model Context Protocol client and dynamic tool manager.
- **Context Expansion via File Mentions**: `@filename` and `@path/to/file` automatic prompt context expansion.
- **Subagent Task Delegation**: Isolated subagent execution with specialized personas and scratch workspaces.
- **Rich Terminal UI Components**: ASCII brand banner, dynamic token status bar, thinking block cards, tool execution cards, diff syntax highlighting, and exit summary metrics.

### Changed
- Minimal core dependencies pruned to `rich`, `openai`, and `requests`.
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

[0.3.4]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.4
[0.3.3]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.3
[0.3.2]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.2
[0.3.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.0

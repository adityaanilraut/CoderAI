# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

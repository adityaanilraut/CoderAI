# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Native tools: `directory_tree`, `read_file_slice`, `write_bg_input`, `workspace_status`, `context_stats`, `export_session`.
- Deterministic offline capability-routing evaluation with accuracy, conservative-fallback, and schema-savings gates.
- `manage_tasks` bulk `titles` so a full execution checklist can be created in one call.

### Fixed
- Mutating sub-agents can edit files in isolated `~/.coderAI/worktrees/<id>/workspace` sandboxes; the home `.coderAI` credential store stays protected.
- Agent loop no longer dump-reads the workspace: exploration-stall reminders, first-turn checklist nudge, and a narrower default tool surface.
- Capability inference now avoids cross-domain matches from overloaded words such as `run`, `search`, `context`, and `server`.
- Modern MCP responses now reject missing or unsupported `resultType` values instead of silently accepting malformed results.
- Repository benchmark scripts now run directly from a clean checkout, create their output directory, avoid warning-log distortion, and have a documented `make benchmark` workflow.
- Pre-commit type checking now runs in a pinned, self-contained environment instead of relying on a global `python` alias and mypy installation.
- `export_session` now requires confirmation (it writes a file; `safe` is reserved for internal state).
- `context_stats` reads the live agent context controller instead of constructing a new provider.
- `workspace_status` runs git through `run_scrubbed` and caps the recent-file walk.
- Completion-gate mutation catalogs now match the live registry (`multi_edit`; dropped stale `edit_file` / `replace_file_content` names).
- `/show models` renders from the unified `ALL_SPECS` registry.

### Changed
- Session HTML/Markdown renderers live in `coderAI/system/session_render.py` so tools do not import the TUI.
- Documentation tool count: 67 auto-discovered native tools, plus `manage_context` and `request_plan_amendment`.
- Default capability routing no longer attaches inspect-only tools (`directory_tree`, `read_file_slice`, `file_stat`, `file_readlink`) to every code-search request. `list_directory` is no longer universal.
- Default `max_tool_output` is 100000 so a normal source file fits in one tool result.
- File inspection reads a known path once unless the file is huge or a prior result was truncated; covering reads are cached and same-path range storms are stalled.
- `outline_file` extracts JavaScript from HTML `<script>` tags. The `outline` keyword routes to code search instead of inspect-only metadata tools.

## [0.3.4] - 2026-08-13

### Added
- Native `multi_edit` filesystem tool supporting simultaneous non-contiguous text edits with atomic write safety.
- Root `pythonpath` configuration in pytest configuration for deterministic test runner imports.

### Fixed
- Fixed MCP stdio launcher validation to fail fast with clean `FileNotFoundError` across OS sandboxes.
- Fixed macOS sandboxed execution compatibility in subprocess environment scrubbing test assertions.
- Fixed Document typing annotations in Textual TUI autocomplete component.
- Fixed distribution integrity verification in `verify_wheel.py` to match current packaged personas and prompts.
- Fixed token counter and loopback web tests to eliminate all network socket test warnings.

### Changed
- Cleaned up obsolete prompt dumps and archive files from workspace.
- Updated documentation (`README.md`, `COMMANDS.md`, `ARCHITECTURE.md`, `CLAUDE.md`) to accurately reflect the 62 native tools, agent personas, and architecture.

## [0.3.3] - 2026-07-15

### Added
- Shared `coderAI.types` package for provenance, tool results, and error codes.
- `session_bootstrap` for unified TUI/headless session create/resume wiring.
- Modular system-prompt composition (`prompts/compose.py`) and persona loading (`core/personas.py`).
- Dedicated `use_skill` tool and shared `command_safety` / `display` helpers.

### Changed
- Architecture cleanup: thinner CLI/bootstrap, agents, terminal tools, and system prompt.
- Tests reorganized under domain folders (`cli/`, `core/`, `tools/`, `tui/`, etc.).

## [0.3.2] - 2026-07-15

### Changed
- PyPI distribution renamed to `coderai-agent` (`coderai` / `coder-ai` are taken).
  Import path and CLI remain `coderAI` (`pip install coderai-agent`).

## [0.3.1] - 2026-07-15

### Fixed
- Make Windows test suite portable (shell quoting, hook payload embedding).
- Allow Windows asyncio under pytest-socket.
- Make CI type checks cross-platform.

## [0.3.0] - 2026-07-07

### Added
- Configurable tool/subprocess timeouts, transient tool retries, and background job caps.
- Hardened security suite (workspace trust, provenance, SSRF, MCP/OAuth, FS hygiene, supply-chain lockfile).
- Textual TUI as the primary interactive surface; headless `coderAI run`.

### Changed
- Tool suite remediation (batch `search_replace`, registry snapshot, detection dedup).
- Architectural modularization of agent session/capabilities and TUI controller.

## [0.2.0] - 2026-06

Pre-0.3 Beta line. See git history for details.

[Unreleased]: https://github.com/adityaanilraut/CoderAI/compare/v0.3.4...HEAD
[0.3.4]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.4
[0.3.3]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.3
[0.3.2]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.2
[0.3.1]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.1
[0.3.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.0
[0.2.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.2.0

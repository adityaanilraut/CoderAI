# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/adityaanilraut/CoderAI/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.3
[0.3.2]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.2
[0.3.1]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.1
[0.3.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.0
[0.2.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.2.0

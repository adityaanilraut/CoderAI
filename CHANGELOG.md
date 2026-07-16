# Changelog

All notable changes to CoderAI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Release notes now advertise the correct PyPI version (`0.3.0`, not `v0.3.0`).
- Documentation drift: README project tree, INSTALL dependency guidance, env docs.

### Changed
- CI coverage floor raised from 65% → 70% (suite currently ~74%).
- CI `pip-audit` uses `--strict` (still non-blocking; Dependabot drives remediation).
- Bundled demo skills (`spotify-control`, `test-skill`) removed; keep `security-audit` and `tdd-workflow`.
- Documented POSIX-first / Windows best-effort platform support.

### Added
- `.env.example` listing provider keys and `CODERAI_*` flags.
- This changelog.

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

[Unreleased]: https://github.com/adityaanilraut/CoderAI/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.2
[0.3.1]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.1
[0.3.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.3.0
[0.2.0]: https://github.com/adityaanilraut/CoderAI/releases/tag/v0.2.0

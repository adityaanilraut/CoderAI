# Changelog

## 0.5.1

- Separate terminal interaction, submission, navigation and rendering into
  focused modules; improve session browsing, attachments and cancellation.
- Harden agent continuation, compaction, team coordination, goals, hook
  dispatch, MCP lifecycle and session storage.
- Consolidate tool definitions and permission policies; improve bounded file
  search, subprocess handling and web-tool validation.
- Add OpenRouter routing and attribution, session migration and protocol
  compatibility regressions.
- Remove unused binary packaging and obsolete helpers, refresh dependency
  constraints, and expand isolated source, SDK, terminal and security checks.

The headless SDK retains its independent version 0.1.0.

## 0.5.0

- Split session streaming, tool dispatch and child execution into focused modules;
  consolidate filesystem, hook, flow and wire adapters.
- Reject duplicate approval IDs before mutating ownership or publishing events.
- Honor atomic text encodings and error policies; keep settings and OAuth
  temporary files private throughout replacement.
- Prevent Kimi/Moonshot routing from inheriting rejected or generic OpenAI keys.
- Correct search result counts and close/reap the stream-JSON example process.
- Remove the vulnerable PyKAOS/AsyncSSH dependency chain by bundling the licensed
  local KAOS interfaces. Require patched PyJWT and refresh the runtime lockfile.
- Exclude bytecode caches from release archives and reject generated caches in
  both wheel and source archive verification.
- Preserve strict source, isolated regression, real-SDK Jev and installed-wheel
  audit gates. Use UTF-8 in CI and source inventory reads on Windows; guard
  POSIX terminal/process operations and provide Windows task-listing fallback.
- Align documentation with current CLI/configuration behavior, remove completed
  plans and empty live-provider test placeholders, and clean generated caches.

The separate headless SDK retains its independent version 0.1.0. Tests use offline
providers; this release does not claim live-provider certification.

# Changelog

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
- Preserve strict source, isolated regression, real-SDK Jev and installed-wheel
  audit gates. Use UTF-8 in CI and source inventory reads on Windows.
- Align documentation with current CLI/configuration behavior, remove completed
  plans and empty live-provider test placeholders, and clean generated caches.

The separate headless SDK retains its independent version 0.1.0. Tests use offline
providers; this release does not claim live-provider certification.

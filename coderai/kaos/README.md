# Local KAOS adapter

Adapted from PyKAOS 0.9.0 (wheel SHA256
`a725910ff167131dbda91e502f40c3b5ff8b67652358ecdd810a0e0339775e10`).
Upstream: https://github.com/MoonshotAI/kimi-cli/tree/9ab1286b8fe4e6bcd116949a27ce5e0ac3389c82/packages/kaos

The Apache-2.0 LICENSE and upstream NOTICE are included. CoderAI namespaces
imports, uses standard typing aliases, and excludes the unused SSH backend and
its type-only stream checks. The local path, context and process APIs retain
upstream behavior for session metadata and ACP fallback operations. Backend
selection remains scoped by ContextVar. ACP's filesystem backend remains in
`coderai.acp.kaos`.

This avoids PyKAOS's exact vulnerable AsyncSSH pin without overriding dependency
metadata or introducing an audit exception. Keep local filesystem, path, process,
backend isolation and installed-wheel contracts covered when updating this code.

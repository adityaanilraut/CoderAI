# Tool roadmap implementation

The five phases preserve all 49 built-in names and the `session_query` alias. Existing working-tree changes were retained. Definitions now live beside their owning tools; the legacy registry remains the public facade. The [generated catalog](tools.md) records the authoritative parameter contracts.

## 1. Policy and ownership

Global restrictions apply to lookup, listing, schema export, validation and execution. Aliases are normalized before checks; session restrictions cannot widen global permissions. Job cancellation verifies ownership before touching a worker. Teams use the canonical project root and trusted root-session ancestry for task visibility, assignment, waiting and messaging. Descendants share their root's team; unrelated roots cannot act on it.

Every built-in declares effects, with invocation-dependent policies for editor viewing, goal status and read-only subagent calls. Planning rejects unclassified external tools. Shells run inside the read-only OS sandbox, reject escalation, and fail closed if containment cannot start. Only the exact session-owned plan file can be written or edited during planning; symlink redirection is rejected.

`exit_plan_mode` uses the explicit `plan-approval` permission scope. Acceptance binds the session, call, arguments, project and plan-file digest. The complete presented plan must fit 50 KiB and decode as UTF-8. Rejection, cancellation, AFK and missing approval leave planning active. Changed plans require fresh acceptance. Typed and dictionary follow-up messages use the same recursive sanitizer before persistence or display.

Undo previews leave files and history intact. Restores retain saved encoding and line endings, and remove recovery history only after successful atomic replacement.

## 2. Schemas and durable state

Cached JSON Schema validators check nested constraints, unions, nullable values and local references. Built-in, plugin and MCP schemas are validated before exposure; automatic remote reference retrieval is disabled and unresolved references identify the tool in an actionable error. Runtime schemas are retained separately from provider projection. Arguments are validated before hooks or subprocesses, with existing approval digests preserved.

Teammate mode/tool limits, task priority/dependencies and wait conditions are exposed. Dependency updates use the existing DAG validator and reject dependencies outside the owning team. Results retain existing fields while adding error codes, retryability and timeout metadata. Todo persistence keeps supplied IDs, full titles and cancelled status, reads legacy `done` as completed, and reports persistence failures.

## 3. File observations

One observation records the raw digest, device/inode identity, size, timestamp, encoding, line endings and displayed completeness. Reads stream raw content and hashing while retaining at most the 50 KiB display window. Truncated, paged, byte-limited and redacted reads cannot authorize a complete overwrite.

Mutations verify content even when timestamps are unchanged or older; verification errors deny writes. Atomic replacement checks identity and digest again immediately before committing. Nonblocking regular-file opens prevent special files from hanging reads. Local images are limited to 10 MiB in both image tools. PDF metadata inspection uses a seekable file, with a 50 MiB input limit.

## 4. Search and process lifecycle

Search and plugin subprocess collection caps stdout at 20,000,000 bytes and stderr at 64 KiB during collection. Search discovery and matching share a monotonic deadline. Exhausted deadlines and output limits report incomplete results with a reason and continuation guidance.

Both search backends honor ignore files outside Git repositories as well. Fallback search honors explicit ignore inclusion and path-separator-aware globs, streams ordinary large text files, and runs regex matching in a cancellable worker process. Lookaround/backreferences require ripgrep; fallback multiline input over 1,000,000 characters is explicitly unsupported. These cases produce errors instead of silently changing semantics.

Persistent-shell cancellation retires the terminal and terminates its process tree before reuse. Plugin timeouts and cancellation clean up descendants; PowerShell uses the same sandbox policy and bounded foreground/background timeout rules. Resource locks remain held until cancelled workers settle, and executor shutdown drains cleanup tasks. External tools retain conservative global scheduling unless trusted local settings declare their resources.

Trusted user settings, or trusted project settings, can declare external policies:

```json
{
  "toolPolicies": {
    "mcp__docs__lookup": {
      "effects": "read",
      "resources": [
        {"path": "docs", "operation": "search", "recursive": true}
      ]
    }
  }
}
```

Server annotations cannot grant this permission. The local declaration must accurately describe the tool; resources outside the canonical project do not relax scheduling. Untrusted project settings cannot supply `toolPolicies`.

## 5. Inspection and maintenance

`/tools` lists the effective session catalog. `/tools <name>` also accepts aliases and displays parameters, source, effects, timeout and restrictions. Generate or verify the reference with:

```bash
.venv/bin/python scripts/tool_catalog.py
.venv/bin/python scripts/tool_catalog.py --check
```

Results record queue wait, execution duration and output size. A bounded cleanup timing log records cancellation settlement without raw arguments or credentials. Regression coverage checks all built-in effect declarations, generated documentation and handler/schema options.

## Verification and review artifacts

The shared verification gate runs each test file independently with isolated homes and scrubbed credentials. Roadmap regressions cover ownership, planning/approval, schemas, sanitization, transactional undo, observation safety, bounded reads, fallback search, process descendants, shutdown and durable todo state. They also run in the dedicated security and Windows suites.

```bash
.venv/bin/python scripts/verification.py check --report .verification/tool-roadmap-final.json
.venv/bin/python -m build --outdir .verification/tool-roadmap-dist
.venv/bin/python scripts/verify_wheel.py .verification/tool-roadmap-dist
```

Ordered review patches under `.verification/tool-roadmap-series/` are based on the saved dirty working tree, rather than Git HEAD, so unrelated user changes are excluded. Phase 1 has an independently reconstructed source tree and focused isolated verification. Applying all five patches reconstructs the implementation; `manifest.json` lists each phase's files.

The final local gate passed on macOS with Python 3.14.7:

- 1,564 tests across 106 independent files; zero failures or skips, including the real-SDK Jev calibration tests.
- Formatting, lint, typing, dependency compatibility, vulnerability audit and CLI version smoke checks passed.
- Built wheel and sdist checks passed. The wheel was installed outside the checkout with dependencies, and native, forced-fallback and no-ripgrep search probes passed.
- Phase 1 independently passed 103 tests across five files. All five patches apply in order and reproduce 75 changed or new files exactly.

Machine-readable reports are `.verification/tool-roadmap-final.json`, `.verification/tool-roadmap-phase1-independent.json` and `.verification/tool-roadmap-series/roundtrip.json`. Build and wheel evidence are in `.verification/tool-roadmap-build.log` and `.verification/tool-roadmap-wheel.log`.

Local provider evidence uses offline or mocked services. Linux containment, Windows process cleanup and live web/MCP/plugin interoperability require their platform/provider runs. The existing CI source matrix exercises Linux/macOS; Windows compatibility remains advisory. A local wheel smoke test does not replace the clean release job's source-commit identity checks.

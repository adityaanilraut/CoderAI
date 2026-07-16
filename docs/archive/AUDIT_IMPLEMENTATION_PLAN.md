# CoderAI Audit Implementation Plan

This plan was produced from a read-only repository audit on July 14, 2026. It
covers missing capabilities, improvements to existing tools, and removal of
stale or generated-looking product surface.

## Audit Result

CoderAI already has 68 native tools. Adding more tools now would increase risk
and maintenance cost. The best next release should harden existing tools,
correct session and index behavior, and remove stale or misleading product
surface.

## Highest Priorities

| Priority | Finding | Key references |
|---|---|---|
| P0 | `/show config` can expose Meta, Tavily, and Exa credentials because TUI redaction duplicates an outdated key list | `coderAI/tui/commands.py:43`, `coderAI/system/config.py:364` |
| P0 | Browser navigation bypasses egress/provenance controls; screenshots write arbitrary paths while marked read-only; subresources are not SSRF-filtered | `coderAI/tools/browser.py:220`, `coderAI/tools/browser.py:589`, `coderAI/tools/browser.py:823` |
| P0 | Read-only/browser subagent isolation is scheduling metadata rather than an enforced capability boundary, especially for dynamic MCP tools | `coderAI/tools/subagent.py:335`, `coderAI/tools/subagent.py:484`, `coderAI/core/tool_executor.py:1120` |
| P0 | MCP's forced mutation approval can be bypassed by an `"allow"` permission hook | `coderAI/core/tool_executor.py:650-699` |
| P0 | `lint`, `format`, `run_tests`, and `manage_tasks` accept paths outside the active project | `coderAI/tools/lint.py:127`, `coderAI/tools/format.py:155`, `coderAI/tools/testing.py:316`, `coderAI/tools/tasks.py:16` |
| P0 | HTTP limits call `resp.read()` before truncation, permitting unbounded memory use; truncated downloads are reported as successful | `coderAI/tools/web/_http.py:351-388`, `coderAI/tools/web/tools.py:547-603` |
| P1 | Semantic indexing leaves deleted vectors searchable and corrupts the manifest during scoped indexing | `coderAI/context/code_indexer.py:83-104`, `coderAI/context/code_indexer.py:173-184`, `coderAI/context/code_indexer.py:245-285` |
| P1 | Resuming a session resets cost and token accounting, bypassing the documented per-session budget | `coderAI/system/history.py:95-104`, `coderAI/core/agent_session.py:70-109` |
| P1 | Automatic skill detection makes a hidden, unmetered LLM call despite the prompt calling skills opt-in | `coderAI/core/agent.py:211-231`, `coderAI/skills/skill_manager.py:274-305`, `coderAI/prompts/intro.mdx:19` |
| P1 | Batch deduplication can suppress repeated mutating operations; batching can reorder calls | `coderAI/core/tool_executor.py:924-978`, `coderAI/core/tool_executor.py:1105-1303` |

## Implementation Plan

### 1. Establish A Clean Behavioral Baseline

- Preserve the current modified files and reconcile overlapping provider, web,
  approval, and TUI changes rather than reverting them.
- Run formatting, lint, type checking, full tests, and security tests before
  changing behavior.
- Capture the current 67 discovered plus one manually registered tool inventory
  as the baseline.

### 2. Close Trust-Boundary Failures

- Create one secret-redaction function in `system/config.py` and use it from
  CLI, TUI, logs, and diagnostics.
- Classify browser tools as network egress and browser output as
  `UNTRUSTED_EXTERNAL`.
- Intercept every Playwright request, redirect, subresource, click navigation,
  and form request before dispatch.
- Make `browser_screenshot` mutating and confirmed; reuse project-scope,
  protected-path, symlink, and atomic-write guards.
- Enforce explicit `read_only`, `browser`, `desktop`, and `workspace` capability
  sets when constructing subagents.
- Exclude dynamic MCP functions from read-only agents unless trusted metadata
  explicitly marks them read-only.
- Prevent permission hooks and generic confirmation overrides from auto-allowing
  MCP-tainted local mutations.
- Apply a shared `resolve_under_project()` guard to lint, format, testing, tasks,
  downloads, screenshots, and undo.

### 3. Harden MCP And Web Transports

- Validate MCP server/tool names against provider function-name limits and
  reject collisions.
- Clamp and sanitize server-controlled descriptions before placing them in
  model-facing schemas.
- Mark MCP discovery, connection metadata, resources, prompts, and results as
  untrusted.
- Replace concurrent stdio reads with one reader task dispatching responses by
  JSON-RPC ID.
- Keep legacy SSE connections open and dispatch responses from the stream;
  enforce HTTPS or an explicit local exception and same-origin message
  endpoints.
- Make reconnect transactional and purge stale tools, resources, and prompts.
- Stream HTTP responses in bounded chunks and stop at `max_bytes + 1`.
- Fail oversized downloads instead of writing truncated files; use atomic
  writes.
- Reject or separately approve cross-origin redirects carrying credentials or
  request bodies.

### 4. Fix Core Correctness

- Expand directory arguments during scoped indexing.
- Only remove manifest entries during full scans, or entries within the
  explicitly scoped subtree.
- Delete ChromaDB records when source files disappear.
- Allow ranged reads of oversized files and stream only requested lines instead
  of calling `readlines()`.
- Stream grep, glob, and symbol results and stop traversal once the output limit
  is satisfied.
- Add explicit `idempotent` or `dedupe_safe` tool metadata and never deduplicate
  mutating calls by default.
- Preserve model-requested ordering around mutation barriers.
- Canonicalize path lock keys so `x.py`, `./x.py`, absolute paths, and aliases
  serialize together.
- Keep undo records until restoration succeeds and reuse normal filesystem
  guards during restoration.
- Normalize all tool results through one contract used by both `ToolRegistry`
  and `ToolExecutor`.

### 5. Repair Sessions And Accounting

- Version the persisted session schema and store prompt tokens, completion
  tokens, cache tokens, and cost totals.
- Restore accounting when resuming instead of resetting it.
- Replay persisted messages into the TUI so resumed conversations can be
  viewed, searched, exported, and rewound.
- Route auxiliary LLM calls such as skill matching through the same metering and
  budget service.
- Recommended default: make automatic skill matching opt-in, try deterministic
  matching first, and visibly report skill activation.
- Update `skill_manager.provider` during model switches and close the previous
  provider client.
- Decide workspace trust consistently: reload safe project configuration
  immediately or clearly require restart.

### 6. Remove AI Slop And Stale Product Surface

- Remove the dead plan pane and nonexistent `plan` tool claims. Preserve
  `/plan` temporarily only as a documented alias for `/tasks`.
- Correct `/init`, which currently instructs agents to call a nonexistent tool
  at `coderAI/tui/commands.py:1068`.
- Remove `get_plan`, `planning.py`, incorrect provider counts, unsupported
  environment variables, and tool-count claims from documentation.
- Fix `docs/COMMANDS.md:266`: the current approval UI remembers a reviewed
  scope and explicitly does not enable YOLO (`coderAI/tui/app.py:615-628`).
- Reduce the header to model, context, cost, and exceptional states. Show
  iteration, elapsed time, persona, reasoning, and agent count only when useful.
- Keep keyboard hints in one location instead of the header, composer, and
  welcome card.
- Either implement real `/verbose` tool-result expansion and searchable result
  navigation or narrow those feature claims.
- Make `@file` mentions ephemeral, or label the action "mention and pin."
- Build desktop and browser prompt sections only when those tools are
  registered.
- Remove migration-phase commentary such as "Phase 3 bridge demolition" while
  retaining security-invariant comments.
- Move or exclude `FIFA-World-Cup-2026-Analysis.md`; it is unrelated generated
  content and the clearest repository-level AI-slop artifact.

### 7. Add Missing Capabilities Only After Stabilization

- **OS-level execution sandbox:** highest-value missing capability, already
  acknowledged in `SECURITY.md:133`.
- **Local embeddings:** enables private semantic search without an OpenAI
  dependency.
- **Session controls:** configurable retention, naming/tags, CLI transcript
  export, and accurate resume.
- **NDJSON event streaming for `coderAI run`:** useful for CI and editor
  integrations.
- **LSP-backed code intelligence:** diagnostics, references, and safe rename
  across languages. Add this only if refactoring is a product priority; do not
  create several thin LSP wrapper tools.

## Additional Tool Improvements

These issues are lower priority than the main phases but should remain in the
backlog.

- Make MCP connection and reconnection transactional, return disconnect errors,
  and deduplicate discovered entries by server and name.
- Add MCP cursor pagination, list-change notifications, request cancellation,
  and structured non-text result handling.
- Resolve approval paths to canonical filesystem targets and support explicit
  source/destination argument keys for move and copy operations.
- Mark broad write and execution tools as ineligible for blanket approval unless
  they have safe argument-scoped policies.
- Avoid reporting cancellation or timeout while non-cancellable worker-thread
  mutations may still be running.
- Make web cache writes atomic and locked, offload cache I/O, and enforce a total
  byte cap rather than only an entry-count cap.
- Reject duplicate tool registrations and isolate discovery failures per tool
  class rather than per module.
- Make `coderAI tasks list` display `in_progress` tasks as well as pending and
  completed tasks.
- Preserve and render per-agent model, token, cost, context, and depth fields
  already emitted by serializers.
- Document that vision currently has an Anthropic-specific rendering path or
  implement equivalent support for other multimodal providers.
- Make session retention configurable and document the current 30-day default.

## Documentation Drift To Correct

- README claims seven providers while the current implementation supports eight,
  including Meta.
- README and architecture diagrams contain conflicting counts such as 96, 94,
  and approximately 68 tools.
- README and contributor documentation reference nonexistent `planning.py` and
  `project.py` modules.
- `docs/CHAT_EVENTS.md` documents nonexistent `get_plan` behavior.
- `.env.example` and `docs/COMMANDS.md` advertise unsupported variables including
  `CODERAI_THEME`, `CODERAI_MODEL`, `CODERAI_RESUME`,
  `CODERAI_AUTO_APPROVE`, and `CODERAI_MAX_BACKGROUND_JOBS`.
- Installation documentation uses ineffective or invalid forms such as
  `coderAI "What is Python?"` and `coderAI --model lmstudio chat`.
- `/init` help text inconsistently refers to `.coderai/` instead of `.coderAI/`.
- Metadata documentation claims chown support although no chown tool exists.
- The Background Jobs command-reference section is empty despite background
  process tools being present.

## AI-Slop Assessment

The compact timeline rails, approval previews, responsive layout, centralized
theme, and concise output prompt are strong and should remain. The slop is
concentrated in duplicated documentation, zombie feature references, capability
overclaims, repeated UI hints, migration archaeology, and generated-looking test
scaffolding, not the underlying visual language.

Specific cleanup targets:

- The retired plan feature still occupies UI space and appears as a real tool in
  docs and generated project instructions.
- The README duplicates reference documentation and has already drifted from
  runtime behavior.
- Browser and desktop prompt sections claim capabilities even when those tools
  are unavailable on the current platform.
- Header, composer, and welcome content repeat shortcuts and state, creating
  dashboard ornamentation rather than task-focused terminal UX.
- `/verbose`, transcript search, and `@file` behavior overpromise what the UI
  currently does.
- Setup asks for every provider before the user selects one and contains
  inconsistent step numbering.
- Compatibility aliases and migration-phase comments shape production code
  around old tests and completed refactors.
- `FIFA-World-Cup-2026-Analysis.md` is unrelated generated content with uncited
  claims and should not be committed with the application.

## Verification

Run after each phase:

```bash
ruff format --check coderAI/
ruff check coderAI/
mypy coderAI/
pytest -q
pytest -q -m security
```

Add focused regressions for:

- Browser subresources, click redirects, DNS rebinding, provenance, and
  screenshot traversal.
- Read-only and browser-domain subagents attempting native writes and mutating
  dynamic MCP calls.
- MCP-tainted mutation with permission-hook `"allow"` and headless approval
  overrides.
- Traversal through lint, format, tests, tasks, download, screenshots, and undo.
- Concurrent out-of-order stdio MCP responses and a real long-lived SSE fixture.
- Malformed MCP IDs and errors, invalid function names, duplicate reconnects,
  and cross-origin redirects.
- Path aliases such as `x.py`, `./x.py`, absolute paths, and symlinked parents.
- Cancellation while a worker-thread mutation is in flight.
- Oversized chunked HTTP responses without `Content-Length`.
- Undo restoration failures preserving the corresponding index entry.
- Scoped semantic indexing, directory inputs, and deleted vector cleanup.
- Resumed budget and token accounting.
- Mutating calls with identical arguments remaining distinct.

## Recommended Sequence

1. Baseline and preserve the current worktree.
2. Fix credential redaction and browser/MCP trust boundaries.
3. Enforce project path scope and bounded network I/O.
4. Fix semantic indexing and executor correctness.
5. Persist session accounting and replay resumed transcripts.
6. Remove stale plan/UI/docs surface and simplify TUI chrome.
7. Add the sandbox and local embeddings before considering broader new tools.

## Default Product Decisions

- Do not expand the native tool count until P0 and P1 findings are resolved.
- Fail closed for browser and MCP behavior where trust metadata is missing.
- Keep `/plan` only as a temporary alias for `/tasks`; do not restore a second
  planning data model.
- Make automatic skill matching opt-in unless it becomes deterministic, visible,
  and fully metered.
- Preserve the existing rail-based visual language while reducing repeated
  chrome and unsupported claims.

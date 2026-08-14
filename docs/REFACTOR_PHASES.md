# CoderAI refactor phases

Saved handoff from the design review. Open this file at the start of a
new session. The interactive review is
[coderai-design-review.canvas.tsx](/Users/adityaraut/.cursor/projects/Users-adityaraut-Desktop-CoderAI-main/canvases/coderai-design-review.canvas.tsx).

## Review (short)

CoderAI is a real coding-agent runtime (trust, RunContext isolation,
progressive schemas, TUI reducer). The next leap is not more tools.

Biggest debts:

1. God modules: `tools/mcp.py` (2739), `tui/app.py` (1807),
   `tui/commands.py` (1686), `tool_executor.py` (1647), `agent_loop.py` (1594).
2. Mixins split files, not the Agent god object.
3. ~67 auto-discovered tools, overlapping editors, keyword capability routing.
4. Config is a flat 50+ field model; provider factory is still if/elif.

Do not start a new tool family until the runtime files shrink.

Order: **freeze → ports → split → types → catalogs**.

---

## Phase 1 — Freeze current-branch defects

Status: **DONE** (this session)

- [x] `export_session`: drop `safe=True`; require confirmation; `approval_scope="path"`
- [x] Move session HTML/Markdown renderers to `coderAI/system/session_render.py` (tools must not import `tui/`)
- [x] `context_stats` uses the live `ContextController` via `ToolServices`
- [x] `workspace_status` uses `run_scrubbed` (via `_run_git_command`) and caps the recent-file walk
- [x] `objective.py` mutation tables match the live registry (`multi_edit`, drop stale `edit_file` / `replace_file_content` / …)
- [x] `/show models` renders from `ALL_SPECS`
- [x] Docs tool count 62 → 67 discovered (+ `manage_context` / `request_plan_amendment`)
- [x] `SECURITY.md` pip-audit wording matches CI (`--strict`, blocking)
- [x] `CHANGELOG.md` `[Unreleased]` updated
- [x] Move `context_stats` / `export_session` out of the `code_search` family; add `multi_edit` to `workspace_edit`

---

## Phase 2 — Ports

Status: **DONE** (verified 2026-08-14)

Replace the leftover process-boundary seam with typed ports so core does
not know Textual exists.

- [x] Introduce `ApprovalPort` (`request(tool, args, preview) -> Approval`)
- [x] `UIBridge` implements it; `coderAI run` implements deny-by-default
- [x] Delete `Agent.ipc_server`; stop `getattr(..., "ipc_server")` in
      `agent_loop.py`, `tool_executor.py`, `hooks_manager.py`,
      `agent_capabilities.py`, `subagent.py`
- [x] Typed `AgentRuntime` protocol + explicit `RuntimeView` for
      `ExecutionLoop` / `ToolExecutor` (ban `vars(self.agent)` /
      unstructured `getattr` in orchestration)
- [x] Tests inject a fake port; headless and TUI share the same interface

Done when: `coderAI/core/` imports nothing from `coderAI/tui/`; grepping
`ipc_server` in `coderAI/` returns only comments or is gone.

---

## Phase 3 — Split god modules

Status: **DONE** (verified 2026-08-14)

Split by job, not by “phase N mixin”.

- [x] `tools/mcp.py` → transport / session / native tools (helpers already exist:
      `mcp_oauth.py`, `mcp_config.py`, `mcp_sanitize.py`)
- [x] `tool_executor.py` → confirmation gate / batch scheduler / transaction bracket
- [x] `agent_loop.py` → LLM phase / tools phase / finish-reason / recovery
- [x] Start shrinking `tui/commands.py` and `tui/app.py` (commands call
      application services; UI only formats)

Done when: no orchestration file is over ~800 lines without a documented reason.

Result: the compatibility facades are 641 lines (`tools/mcp.py`), 621 lines
(`core/tool_executor.py`), 612 lines (`core/agent_loop.py`), 438 lines
(`tui/app.py`), and 143 lines (`tui/commands.py`). Transport, session,
catalog/discovery, native-tool, execution-phase, UI-controller, and application
service modules are each below 600 lines. Focused seam/regression tests, Ruff,
changed-module mypy, and the complete 2,164-test suite are green.

Repository-wide files still above ~800 lines are outside the Phase 3 facade
scope and remain deliberately unchanged:

| File | Lines | Reason to defer |
|------|------:|-----------------|
| `tui/screens.py` | 1,518 | View/widget definitions rather than orchestration; split with a dedicated UI-component pass. |
| `tools/browser.py` | 1,458 | One browser-tool implementation and protocol surface; extracting it needs its own compatibility/test pass. |
| `context/context_controller.py` | 1,113 | Context-budget orchestration is tightly coupled to compaction/index state; defer until the Phase 4 protocol/type boundary is strict. |
| `tools/subagent.py` | 1,106 | Delegation tool and child-runtime lifecycle share trust/worktree invariants; split only with focused isolation coverage. |
| `system/history.py` | 831 | Persistence models and format migration code, not turn orchestration. |
| `context/code_indexer.py` | 816 | Indexing implementation, not turn orchestration. |
| `cli/mcp_cmd.py` | 813 | Click command surface; the runtime MCP implementation was split in this phase. |
| `llm/anthropic.py` | 806 | Provider adapter and wire-format conversion, not orchestration. |

---

## Phase 4 — Types ratchet

Status: **DONE** (verified 2026-08-14)

- [x] Add `agent.py`, `agent_loop.py`, `agent_capabilities.py`,
      `agent_session.py` to the mypy strict list in `pyproject.toml`
- [x] Replace duck-typed Agent access with the Phase 2 protocol
- [x] Keep the ratchet rule: the strict-module list only grows

Done when: `make typecheck` is green with those modules on the strict list.

Result: all four Agent facades and the five extracted Phase 3 loop modules are
on the append-only strict override. The extracted modules now declare their
`AgentRuntime`-backed mixin contracts directly instead of relying on file-wide
mypy suppressions, while `RuntimeView` owns the documented construction/test
defaults. Focused protocol and agent-boundary tests, Ruff, targeted mypy,
`make typecheck`, and the complete 2,168-test suite are green.

---

## Phase 5 — Catalogs and config

Status: **DONE** (verified 2026-08-14)

- [x] Single `ToolSemantics` table feeding the completion gate,
      first-useful-action filter, and capability families
- [x] Capability routing by declared tags, with legacy free-form inference kept
      at an explicit compatibility boundary
- [x] Nest `Config` (`providers`, `tools`, `session`, `ui`) behind explicit
      flat-file, environment, attribute, and CLI migration compatibility
- [x] Put `provider_cls` on `ModelSpec`; delete per-provider `SUPPORTED_MODELS`
      tables and the factory if/elif
- [x] Keep Milestone 3 isolation (background mutations, shared `.git` in
      worktrees) as an explicit follow-up rather than a Phase 5 prerequisite

Done when: adding a tool is one registry row + one capability tag; adding a
model is one `ALL_SPECS` row.

Result: the live native registry is covered by one typed semantics catalog,
and completion evidence, first-useful-action accounting, and routing all read
that catalog. Config persists sparse nested sections while accepting existing
flat constructors, attributes, files, environment variables, project overlays,
and CLI keys. `ALL_SPECS` now owns provider construction, supported-model
compatibility views, and context windows. Focused catalog/config/CLI/routing/
provider tests, Ruff, targeted mypy, `make typecheck`, and the complete
2,178-test suite are green (1 intentionally deselected).

---

## Phase 5 start prompt

Copy this into a new coding session:

```
Continue CoderAI refactor Phase 5 (Catalogs and config) from docs/REFACTOR_PHASES.md.

Phases 1–4 are complete. Phase 4 added the Agent facades and every extracted
loop phase to the append-only strict mypy ratchet, replaced duck-typed Agent
access with explicit runtime boundaries, and kept all compatibility imports and
behavior intact.

Constraints:
- Do not add tools.
- Preserve all public imports, behavior, and user-owned worktree changes.
- Keep the mypy strict-module ratchet append-only.
- Preserve compatibility for existing flat Config files, environment variables,
  CLI config commands, and provider/model aliases while introducing the nested
  representation.
- Do not make Milestone 3 isolation work a prerequisite for this phase.

First audit the live tool registry, completion gate, first-useful-action filter,
capability routing, Config load/save/CLI paths, model registry, provider factory,
and per-provider supported-model tables. Then implement one typed
`ToolSemantics` catalog that feeds completion, useful-action, and capability
selection; route capabilities from declared tags instead of prompt bag-of-words
checks; nest Config into `providers`, `tools`, `session`, and `ui` with an
explicit compatibility/migration boundary; and put `provider_cls` on
`ModelSpec` so the redundant provider tables and factory if/elif can be removed.

Add focused catalog, config migration/precedence, CLI compatibility, routing,
and provider-construction tests. Run focused tests, Ruff on changed files, mypy
on changed modules, `make typecheck`, and the complete test suite. Mark Phase 5
complete only when adding a tool is one registry row plus one capability tag,
adding a model is one `ALL_SPECS` row, and all validation gates are green.
```

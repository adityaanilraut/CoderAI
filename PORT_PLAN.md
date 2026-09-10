# PORT PLAN — remaining Kimi-structure files for CoderAI

Gold standard: `/Users/adityaraut/Downloads/kimi-cli-main`
Target package: `coderai/` (mirrors `src/kimi_cli/` layout, package name stays `coderai`).

## Current Status
- **Phase 0 (Dependencies & Trivial Leaves)**: COMPLETED
- **Phase 1 (Leaf Features with In-Repo Analogues)**: COMPLETED
- **Phase 2 (Engine Core on `kosong` + `kaos`)**: COMPLETED (8/8 passed, 273/273 suite)
- **Phase 2b (Session Monolith Modularization)**: COMPLETED (273/273 passed)
- **Phase 3 (ACP Server & Subagent Wire Runner)**: COMPLETED (7/7 passed)
- **Phase 4 (Interactive Shell & Rich Print / Live View)**: COMPLETED
- **Phase 5 (Web UI & Server Backends)**: **DROPPED** (Pure CLI decision)
- **Phase 6 (Repo Structure, Scripts & Docs)**: PENDING

## Ground rules (carry over)

1. Reuse CoderAI code where it exists; port Kimi logic only where there is no counterpart.
2. No placeholder files — every created file must import and be exercised.
3. New implementation goes at the Kimi path; if an old path exists it becomes a
   forwarding shim (`from coderai._moved import forward; forward(__name__, "<new>")`).
4. RAM rule: never run the full suite in one process. Per-file:
   `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest <file> -p no:cacheprovider --benchmark-disable -q`
   (bare `python3` lacks deps; the Framework build has them).
5. After each phase: `python3 -m compileall -q coderai` must be clean.
6. **Pure CLI focus**: CoderAI remains a dedicated CLI terminal tool. All Web UI, browser session viewers, and HTTP/FastAPI server backends (`vis/`, `web/`) are completely removed from scope.

## Key finding: no vendoring needed

All of Kimi's extracted dependencies are on PyPI **and already installed** in the
deps interpreter: `kosong[contrib]==0.56.0`, `pykaos==0.9.0` (provides `kaos`),
`typer==0.21.1`, `fastmcp==3.2.4`, `tenacity`, `trafilatura==2.0.0`,
`aiofiles`, `jinja2`, `agent-client-protocol` (`acp`). Phase 0 declared them.
*(Note: `fastapi` and `uvicorn` are removed as Web UI is dropped).*

## Phase 0 — dependencies + trivial leaves (~30 min) [COMPLETED]

- `pyproject.toml`: declared `kosong[contrib]==0.56.0`, `pykaos==0.9.0`, `typer>=0.21`,
  `fastmcp>=3.2`, `tenacity>=9`, `jinja2>=3.1`, `aiofiles>=24`, `agent-client-protocol>=0.8`.
  *(Dropped `fastapi` and `uvicorn` — not needed for pure CLI).*
- `coderai/constant.py` — PORT `src/kimi_cli/constant.py` (116 lines, pure constants).
- `coderai/prompts/compact.md` (73), `init.md` (21) — COPY verbatim + package-data.
- `coderai/acp/version.py` (45), `acp/mcp.py` (46) — PORT verbatim (no coderai source).
- `coderai/tools/display.py` (46), `tools/test.py` (55) — PORT verbatim.
- `coderai/soul/message.py` (92) — PORT verbatim (history-derivation helpers).
- `coderai/background/ids.py` (19), `background/summary.py` (66) — PORT verbatim.
- Status: DONE.

## Phase 1 — leaf features with in-repo analogues (~2–3 h) [COMPLETED]

| Source (kimi) | Lines | Target | Strategy | Status |
|---|---|---|---|---|
| `utils/clipboard.py` | 246 | `coderai/utils/clipboard.py` | PORT; CoderAI `cli/image_attachment.py` is file-path-only — wire clipboard grab into it, keep old path working | DONE |
| `telemetry/crash.py` | 149 | `coderai/telemetry/crash.py` | ADAPT onto `core/common/error_logger.py` (same concern, different shape) | DONE |
| `telemetry/transport.py` | 318 | `coderai/telemetry/transport.py` | PORT; keep `telemetry/sink.py` API stable | DONE |
| `background/worker.py` | 209 | `coderai/background/worker.py` | ADAPT onto `core/common/process_tree.py` kill-tree + `jobs.py` runner | DONE |
| `soul/dynamic_injections/afk_mode.py` | 74 | `coderai/soul/dynamic_injections/afk_mode.py` | EXTRACT from `soul/dynamic_injection.py` inline providers | DONE |
| `soul/dynamic_injections/plan_mode.py` | 245 | `coderai/soul/dynamic_injections/plan_mode.py` | EXTRACT ditto | DONE |
| `skill/flow/d2.py` + `mermaid.py` | 482+266 | `coderai/skill/flow/` | PORT; `core/flow/` keeps shim (check `flow/runner.py` consumers first) | DONE |
| `tools/plan/heroes.py` | 277 | `coderai/tools/plan/heroes.py` | PORT (slug-gen, no coderai counterpart) | DONE |
| `tools/plan/enter.py` | 197 | `coderai/tools/plan/enter.py` | PORT (EnterPlanMode; coderai has exit-only) + sidecar `enter_description.md` | DONE |
| `acp/tools.py` | 169 | `coderai/acp/tools.py` | PORT (needs `kosong.tooling.mcp.convert_mcp_content` — now a declared dep) | DONE |
| `acp/convert.py` (full) | 128 | extend `coderai/acp/convert.py` | PORT remaining codec halves (agent C did types/codec subset only) | DONE |

Status: DONE.

## Phase 2 — engine core, needs `kosong` + `kaos` [COMPLETED]

| Source | Lines | Target | Strategy | Status |
|---|---|---|---|---|
| `llm.py` | 565 | `coderai/llm.py` (exists: llm_types+openai_client merge) | ADAPT: keep `create_openai_client` + pool intact (tests depend on it); add Kimi's `kosong.chat_provider` factory (Kimi/Anthropic/OpenAI-legacy+responses/GoogleGenAI/Chaos/Echo) as new constructors (`create_llm`, `LLM`) | DONE |
| `soul/toolset.py` | 1104 | `coderai/soul/toolset.py` | PORT on `kosong.tooling`; `CoderAIToolset` (alias `KimiToolset`), `_build_repeat_reminder`, `_canonical_tool_arguments`, contextvars `current_tool_call` & `_current_step_no` | DONE |
| `soul/context.py` | 339 | `coderai/soul/context.py` | PORT context persistence, checkpointing, token counting, revert mechanisms | DONE |
| `soul/slash.py` | 341 | `coderai/soul/slash.py` | PORT engine-side slash command registry and handlers (`init`, `compact`, `clear`, `yolo`, `afk`, `plan`, `add-dir`, `export`, `import`) | DONE |
| `session.py` | 319 | `coderai/session.py` | NEW slim facade over `core.session` exposing Session create/get/open/reply + JSONL store delegating inward | DONE |
| `metadata.py` | 134 | `coderai/metadata.py` | PORT workspace directory metadata (`WorkDirMeta`, `Metadata`, `load_metadata`, `save_metadata`) | DONE |
| `hooks/*` | 400 | `coderai/hooks/*` | Hooks config, runner, and engine (`HookEventType`, `HookEngine`, `WireHookSubscription`) | DONE |
| `utils/*` | 300 | `coderai/utils/*` | `slashcmd.py`, `sensitive.py`, `string.py`, path helpers | DONE |

Verification: `tests/test_phase2_engine.py` (8/8 passed), all 15 test suites passed (273/273 tests), and `python3 -m compileall -q coderai` clean.

## Phase 2b — carve `core/session.py` monolith into modular pieces [COMPLETED]

Carved ~940 lines of disparate concerns out of `core/session.py` into focused, cohesive modules while preserving 100% backward compatibility and test stability:
- `coderai/core/session_models.py`: `SessionMessage`, `SessionEntry`, dictionary serialization, token/usage accumulators.
- `coderai/core/session_background.py`: Background process completion tracking, log tail slicing, schedule dispatch, task completion notification.
- `coderai/core/session_fork_ops.py`: History slicing, branching, and git-checkpoint undo.
- `coderai/core/session_completion.py`: Tool call normalization, streaming chunk routing, and completion response formatting.
- `coderai/core/session_approval.py`: Repetition loop collapsing and global/session AFK registry.

Verification: All 14 test suites passed (273/273 tests) and `python3 -m compileall -q coderai tests` clean.

## Phase 3 — ACP server (~2 h, after Phase 2) [COMPLETED]

| Source | Lines | Target | Strategy | Status |
|---|---|---|---|---|
| `acp/server.py` | 468 | `coderai/acp/server.py` | PORT multi-session ACP server (`ACPServer`, version negotiation, capabilities, terminal auth, models/modes) | DONE |
| `acp/session.py` | 580 | `coderai/acp/session.py` | PORT in-proc `ACPSession` (turn streaming, wire message mapping, permission approvals, plan updates, replay); keep `AcpSubagentRunner` re-exports | DONE |
| `core/acp/runner.py` | 220 | `coderai/core/acp/runner.py` | RESTORE out-of-proc runner (`AcpSubagentRunner`, `AcpRunConfig`) without circular forwarding | DONE |
| `acp/kaos.py` | 291 | `coderai/acp/kaos.py` | PORT on `pykaos` (`ACPKaos`, `ACPProcess`, `_NullWritable`, terminal execution routing) | DONE |
| `acp/tools.py` | 169 | `coderai/acp/tools.py` | PORT `Terminal(CallableTool2)` + `replace_tools()` tool replacement; keep `HideOutputDisplayBlock` | DONE |
| `tools/utils.py` | 200 | `coderai/tools/utils.py` | PORT `ToolResultBuilder`, `ToolRejectedError`, `truncate_line`, `load_desc` | DONE |
| `tools/shell/__init__.py` | +95 | `coderai/tools/shell/__init__.py` | ADD `Params` + `Shell(CallableTool2)` while preserving legacy bash API | DONE |
| `soul/approval.py` | +140 | `coderai/soul/approval.py` | ADD `Approval`, `ApprovalResult`, `ApprovalState`, `ApprovalRuntime` while preserving legacy permission registry | DONE |
| `app.py` | 330 | `coderai/app.py` | NEW headless coordinator (`KimiCLI`, `CoderAICLI = KimiCLI`, `enable_logging`) | DONE |
| `acp/__init__.py` | 25 | `coderai/acp/__init__.py` | PORT `acp_main()` entrypoint and export `ACPServer` | DONE |

Verification: `tests/test_acp_server.py` (7/7 passed), all existing suites passing (including `test_phase2_engine.py`, `test_subagents.py`, `test_session_engine.py`, `test_config_setup.py`, `test_tools.py`), and `python3 -m compileall -q coderai tests` clean.

## Phase 4 — interactive shell + print (~3–4 h, after Phase 2) [COMPLETED]

Pure CLI interactive experience, Rich terminal visualization, prompt_toolkit interactive shell, and theme consistency:

| Source | Lines | Target | Notes | Status |
|---|---|---|---|---|
| `ui/shell/echo.py` | 18 | `coderai/ui/shell/echo.py` | Render user input echo with `PROMPT_SYMBOL` (`✨`) | DONE |
| `ui/shell/replay.py` | 215 | `coderai/ui/shell/replay.py` | Turn replay from history/wire with step & tool result synthesis | DONE |
| `ui/shell/migration_nudge.py` | 120 | `coderai/ui/shell/migration_nudge.py` | Upgrade/exit nudges with date throttle and command strings | DONE |
| `ui/shell/mcp_status.py` | 115 | `coderai/ui/shell/mcp_status.py` | MCP status snapshots for both console and prompt toolbar | DONE |
| `ui/shell/update.py` | 749 | `coderai/ui/shell/update.py` | Full version check, semver comparison, and `do_update` | DONE |
| `ui/shell/prompt.py` | 2300+ | `coderai/ui/shell/prompt.py` | Kimi `CustomPromptSession` + PTK/readline backward compatibility layer | DONE |
| `ui/print/visualize.py` | 197 | `coderai/ui/print/visualize.py` | Text, Json, FinalOnly printers and non-interactive `visualize()` | DONE |
| `ui/shell/visualize/_live_view.py` | 921 | same path | Event-driven Rich Live orchestration and streaming display | DONE |
| `ui/shell/visualize/_interactive.py` | 530 | same path | Interactive Live view with PTK prompt and question/approval delegation | DONE |
| `ui/shell/visualize/_approval_panel.py` | 130 | same path | Tool approval modal delegate for prompt_toolkit | DONE |
| `ui/shell/visualize/_question_panel.py` | 135 | same path | User question modal delegate for prompt_toolkit | DONE |
| `utils/rich/diff_render.py` | 520 | `coderai/utils/rich/diff_render.py` | Structured hunk diffs + polymorphic `render_diff_preview` | DONE |
| `utils/broadcast.py` | 45 | `coderai/utils/broadcast.py` | SPMC queue with shutdown propagation and late subscriber replay | DONE |
| `cli/_lazy_group.py` | 238 | `coderai/cli/_lazy_group.py` | Typer command group lazy loader | DONE |
| `cli/toad.py` | 73 | `coderai/cli/toad.py` | Toad ACP terminal runner integration | DONE |
| `cli/web.py` + `vis.py` | — | **DROPPED** | Dropped: CoderAI is pure CLI. No browser/server viewer commands. | DROPPED |

Verification:
- `tests/test_phase4_ui.py`: 12/12 passed in 0.31s.
- `tests/test_cli.py`: 26/26 passed in 0.62s.
- `tests/test_phase2_engine.py`: 8/8 passed in 0.41s.
- `tests/test_acp_server.py`: 7/7 passed in 0.74s.
- `tests/test_prompt_context.py`: 38/38 passed in 0.55s.
- `tests/test_session_engine.py`: 20/20 passed in 0.73s.
- `tests/test_config_setup.py`: 21/21 passed in 0.68s.
- CLI Entrypoint: `coderai --version` (0.4.0) and `coderai --help` cleanly executable.
- `python3 -m compileall -q coderai tests`: 100% clean.


## Phase 5 — [DROPPED] `vis/` + `web/` Web UI & Server Backends (Pure CLI Decision)

**REMOVED & PURGED**: In accordance with keeping CoderAI a pure terminal CLI application, all Web UI components, browser session viewers, and HTTP/FastAPI server backends are removed from scope:
- **`coderai/vis/`**: DROPPED (FastAPI session viewer, statistics, system endpoints).
- **`coderai/web/`**: DROPPED (FastAPI web server, runner/process, store/sessions).
- **`coderai/cli/web.py` & `web_cmd.py`**: DELETED (removed `/web` and `/vis` browser preview commands).
- **React Frontends**: DROPPED (`vis/`, `web/` webapps, Vite builds, node/npm assets).
- **Build Scripts**: DROPPED (`scripts/build_vis.py`, `scripts/build_web.py`).
- **Web Dependencies**: Removed `fastapi` and `uvicorn[standard]` from required runtime dependencies.
- **Pure Terminal Focus**: All visualization, session inspection, and user interaction are handled strictly in the terminal via Rich and prompt-toolkit (`coderai/ui/print/visualize.py`, `coderai/ui/shell/visualize/`, Rich live rendering).
- **CLI Commands & Menus**: `/web` and `/vis` purged from autocomplete, help menus, documentation, and subcommand tables. If entered, CLI informs the user that CoderAI is pure CLI.

## Phase 6 — repo structure + SDK + docs (parallelizable, low risk)

- `packages/kimi-code/` → DECISION (a); `sdks/kimi-sdk/` → DECISION (b).
  `packages/kaos` + `packages/kosong`: NOT ported — covered by `pykaos` /
  `kosong` PyPI deps (verified same versions Kimi pins).
- `scripts/`: add `check_kimi_dependency_versions.py`, `check_version_tag.py`,
  `cleanup_tmp_sessions.py`, `inject_build_sha.py`, `install.sh`, `telemetry_debug_server.py`
  (adapt paths; `build_vis.py` and `build_web.py` dropped).
- `.agents/skills/` (9 skills), `klips/` (process docs, copy relevant),
  `examples/` (adapt 2–3: custom-tools, kimi-psql-style), `docs/` (pure CLI usage and configuration; customization pages),
  `tests_ai/` + `tests_e2e/` layout (wire-style tests after Phase 3).
- `.github/workflows/` + root files (`uv.lock` vs requirements, `flake.*`,
  `kimi.spec`, `pytest.ini`, `AGENTS.md`, `CONTRIBUTING.md`, `NOTICE`) — after
  code phases; keep `requirements*.txt` until dep set is final.

## Decisions needed (answer before Phase 2/4/6)

a. `packages/kimi-code` (agent spec packaging): port as `packages/coderai-code`,
   or is `coderai/agents/` + `pyproject` package-data enough? (Recommend: enough — skip.)
b. `sdks/kimi-sdk` → `sdks/coderai-sdk`: wanted? (Recommend: defer; no consumer yet.)
c. Typer migration for `cli/` (`_lazy_group`, `__main__`): port now or keep
   argparse + thin Typer shim later? (Recommend: keep argparse; revisit post-Phase 4.)
d. `vis/` + `web/` Web UI & React frontends: **RESOLVED: DROPPED**. User decided to remove the Web UI and keep CoderAI as a pure CLI application. No web server, no browser session viewer, no frontend assets.

## Suggested execution order

0 → 1 → 2 (and 2b) → 3 → 4 → 6 (Repo Structure & Docs).
- Phase 0, 1, 2, 2b, 3, and 4 are **COMPLETED**.
- Phase 5 (Web UI / Vis backends) is **DROPPED** (Pure CLI decision).
- Final step: **Phase 6** (repo structure, packaging, scripts, and documentation).

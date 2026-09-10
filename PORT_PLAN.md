# PORT PLAN — remaining Kimi-structure files for CoderAI

Gold standard: `/Users/adityaraut/Downloads/kimi-cli-main`
Target package: `coderai/` (mirrors `src/kimi_cli/` layout, package name stays `coderai`).
Status of previous session: core/cli/tools/wire/acp/skill/plugin/utils modularized,
109 forwarding shims via `coderai/_moved.py`, **765 passed / 0 failed** (68 files,
per-file runs; `test_benchmark_optimizations.py` excluded).

## Ground rules (carry over)

1. Reuse CoderAI code where it exists; port Kimi logic only where there is no counterpart.
2. No placeholder files — every created file must import and be exercised.
3. New implementation goes at the Kimi path; if an old path exists it becomes a
   forwarding shim (`from coderai._moved import forward; forward(__name__, "<new>")`).
4. RAM rule: never run the full suite in one process. Per-file:
   `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest <file> -p no:cacheprovider --benchmark-disable -q`
   (bare `python3` lacks deps; the Framework build has them).
5. After each phase: `python3 -m compileall -q coderai` must be clean.

## Key finding: no vendoring needed

All of Kimi's extracted dependencies are on PyPI **and already installed** in the
deps interpreter: `kosong[contrib]==0.56.0`, `pykaos==0.9.0` (provides `kaos`),
`typer==0.21.1`, `fastmcp==3.2.4`, `fastapi`, `tenacity`, `trafilatura==2.0.0`,
`aiofiles`, `jinja2`, `agent-client-protocol` (`acp`). Phase 0 just declares them.

## Phase 0 — dependencies + trivial leaves (~30 min)

- `pyproject.toml`: add `kosong[contrib]==0.56.0`, `pykaos==0.9.0`, `typer>=0.21`,
  `fastmcp>=3.2`, `tenacity>=9`, `jinja2>=3.1`, `aiofiles>=24`, `fastapi>=0.115`,
  `uvicorn[standard]>=0.32` (server extras), `agent-client-protocol>=0.8`.
- `coderai/constant.py` — PORT `src/kimi_cli/constant.py` (116 lines, pure constants).
- `coderai/prompts/compact.md` (73), `init.md` (21) — COPY verbatim + package-data.
- `coderai/acp/version.py` (45), `acp/mcp.py` (46) — PORT verbatim (no coderai source).
- `coderai/tools/display.py` (46), `tools/test.py` (55) — PORT verbatim.
- `coderai/soul/message.py` (92) — PORT verbatim (history-derivation helpers).
- `coderai/background/ids.py` (19), `background/summary.py` (66) — PORT verbatim.
- Accept: each imports; `test_core`-style smoke (constructors only).

## Phase 1 — leaf features with in-repo analogues (~2–3 h)

| Source (kimi) | Lines | Target | Strategy |
|---|---|---|---|
| `utils/clipboard.py` | 246 | `coderai/utils/clipboard.py` | PORT;CoderAI `cli/image_attachment.py` is file-path-only — wire clipboard grab into it, keep old path working |
| `telemetry/crash.py` | 149 | `coderai/telemetry/crash.py` | ADAPT onto `core/common/error_logger.py` (same concern, different shape) |
| `telemetry/transport.py` | 318 | `coderai/telemetry/transport.py` | PORT; keep `telemetry/sink.py` API stable |
| `background/worker.py` | 209 | `coderai/background/worker.py` | ADAPT onto `core/common/process_tree.py` kill-tree + `jobs.py` runner |
| `soul/dynamic_injections/afk_mode.py` | 74 | `coderai/soul/dynamic_injections/afk_mode.py` | EXTRACT from `soul/dynamic_injection.py` inline providers |
| `soul/dynamic_injections/plan_mode.py` | 245 | `coderai/soul/dynamic_injections/plan_mode.py` | EXTRACT ditto |
| `skill/flow/d2.py` + `mermaid.py` | 482+266 | `coderai/skill/flow/` | PORT; `core/flow/` keeps shim (check `flow/runner.py` consumers first) |
| `tools/plan/heroes.py` | 277 | `coderai/tools/plan/heroes.py` | PORT (slug-gen, no coderai counterpart) |
| `tools/plan/enter.py` | 197 | `coderai/tools/plan/enter.py` | PORT (EnterPlanMode; coderai has exit-only) + sidecar `enter_description.md` |
| `acp/tools.py` | 169 | `coderai/acp/tools.py` | PORT (needs `kosong.tooling.mcp.convert_mcp_content` — now a declared dep) |
| `acp/convert.py` (full) | 128 | extend `coderai/acp/convert.py` | PORT remaining codec halves (agent C did types/codec subset only) |

Accept per file: import + nearest existing test file still passes
(`test_tools.py`, `test_phase2_search.py`,.unit for skill/flow if present).

## Phase 2 — engine core, needs `kosong` + `kaos` (~4–6 h, highest value)

| Source | Lines | Target | Strategy |
|---|---|---|---|
| `llm.py` | 565 | `coderai/llm.py` (exists: llm_types+openai_client merge) | ADAPT: keep `create_openai_client` + pool intact (tests depend on it); add Kimi's `kosong.chat_provider` factory (Kimi/Anthropic/OpenAI-legacy+responses/GoogleGenAI/Chaos/Echo) as new constructors |
| `soul/toolset.py` | 1104 | `coderai/soul/toolset.py` | PORT on `kosong.tooling`; keep `core/tools/registry.py` + `executor.py` delegating (do NOT rewire dispatch yet — that's Phase 2b) |
| `soul/context.py` | 339 | `coderai/soul/context.py` | EXTRACT context-budget logic out of `core/agent_loop.py` + `prompt.py` halves; `soul/agent.py` imports it |
| `soul/slash.py` | 341 | `coderai/soul/slash.py` | ADAPT: slash catalog lives in `ui/shell/slash.py`; build engine-side registry that reads from it (no duplication) |
| `session.py` | 319 | `coderai/session.py` | NEW slim facade over existing `core/session.py` monolith (3140 lines). Do NOT split the monolith yet: facade exposes Session create/get/reply + JSONL store, delegating inward. Monolith slim-down is Phase 2b |

Phase 2b (follow-up, risky): carve `core/session.py` monolith into the facade +
already-existing `soul/*`, `background/*`, `subagents/*` pieces, one extraction
at a time with the session tests (`test_core.py`, `test_session_query.py`) green
after each cut.

## Phase 3 — ACP server (~2 h, after Phase 2)

- `coderai/acp/server.py` (468) — PORT; CoderAI `core/acp/runner.py` is an
  out-of-proc client driver, Kimi's is an in-proc server session: keep both,
  server does not replace runner.
- `coderai/acp/kaos.py` (291) — PORT (`kaos` dep, declared Phase 0).
- Accept: `packages/kaos`-equivalent behavior via `pykaos`; add
  `tests/test_acp_server.py` (new, small: init + one session round-trip).

## Phase 4 — interactive shell + print (~3–4 h, after Phase 2)

| Source | Lines | Target | Notes |
|---|---|---|---|
| `ui/shell/echo.py` | 17 | `coderai/ui/shell/echo.py` | trivial PORT |
| `ui/shell/replay.py` | 215 | `coderai/ui/shell/replay.py` | PORT; needs session-store read API (exists) |
| `ui/shell/migration_nudge.py` | 119 | `coderai/ui/shell/migration_nudge.py` | PORT; wire into `ui/shell/startup.py` where welcome lives |
| `ui/shell/mcp_status.py` | 111 | `coderai/ui/shell/mcp_status.py` | PORT; console half overlaps `interactive_menu.py` — reuse, don't duplicate |
| `ui/shell/update.py` | 749 | `coderai/ui/shell/update.py` | PORT; CoderAI has only `cmd_upgrade` stub — replace stub with delegation |
| `ui/print/visualize.py` | 194 | `coderai/ui/print/visualize.py` | PORT Text/Json/FinalOnly printers; `exec_runner.py` keeps default path |
| `ui/shell/visualize/_live_view.py` | 921 | same path | PORT event-driven Live orchestration; `cli/app.py` monolith adopts it last, keep old path until green |
| `ui/shell/visualize/_interactive.py` | 530 | same path | ditto |
| `cli/_lazy_group.py` | 238 | `coderai/cli/_lazy_group.py` | DEFER unless Typer migration decided (see Decisions). Stub that re-exports argparse entrypoints if needed |
| `cli/__main__.py` | 34 | `coderai/cli/__main__.py` | PORT once `_lazy_group` decided |
| `cli/toad.py` | 73 | `coderai/cli/toad.py` | PORT only if `batrachian-toad` installs on this platform, else PENDING with note |
| `cli/web.py` + `vis.py` split | — | `coderai/cli/vis.py` | SPLIT `cli/web_cmd.py` snapshot-server half into `vis.py` (backend lands Phase 5) |

## Phase 5 — `vis/` + `web/` backends (~1–2 days, separable)

- `coderai/vis/` (`app.py` 175, `api/sessions.py` 687, `statistics.py` 209,
  `system.py` 19) — PORT on FastAPI; CoderAI `core/session_query/` engine feeds
  `api/sessions.py` instead of Kimi's viewer model (ADAPT, don't duplicate).
- `coderai/web/` (`app.py` 451, `api/sessions.py` 1245, `runner/process.py` 754,
  `store/sessions.py` 432, rest) — PORT; reuse `wire/server.py` stdio transport
  where it fits.
- React frontends (`vis/`, `web/` top-level dirs): COPY as-is, wire `scripts/build_vis.py` /
  `build_web.py` equivalents last. Frontend is explicitly out of scope until backends pass.
- Accept: `tests/test_vis_api.py`, `tests/test_web_api.py` (new, FastAPI TestClient).

## Phase 6 — repo structure + SDK + docs (parallelizable, low risk)

- `packages/kimi-code/` → DECISION (a); `sdks/kimi-sdk/` → DECISION (b).
  `packages/kaos` + `packages/kosong`: NOT ported — covered by `pykaos` /
  `kosong` PyPI deps (verified same versions Kimi pins).
- `scripts/`: add `check_kimi_dependency_versions.py`, `check_version_tag.py`,
  `cleanup_tmp_sessions.py`, `inject_build_sha.py`, `install.sh`, `telemetry_debug_server.py`
  (adapt paths; drop `build_vis/web.py` until Phase 5).
- `.agents/skills/` (9 skills), `klips/` (process docs, copy relevant),
  `examples/` (adapt 2–3: custom-tools, kimi-psql-style), `docs/` (defer full
  VitePress; start with `docs/en/` customization pages that differ),
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
d. `vis/` + `web/` React frontends: copy now or backends-only? (Recommend: backends-only.)

## Suggested execution order

0 → 1 → 2 → 3 → 4 → 5 → 6. Within a phase, files are independent — parallelize
with one agent per file, disjoint paths only. Phase 2 files must stay sequential
(llm → toolset → context → slash → session facade).

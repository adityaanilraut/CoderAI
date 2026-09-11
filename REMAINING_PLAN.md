# CoderAI — Remaining Work Plan (handoff)

**Written:** after commit `5e06ff6`  
**Kimi CLI reference (gold standard for structure):** `/Users/adityaraut/Downloads/kimi-cli-main`

This file is the single handoff document for the remaining modularisation work. It
records verified numbers, the operating rules for this repo, and the open items in
priority order.

---

## 1. Where the project stands

The port now has a single modular tree. The legacy `coderai/core/**` package and
the `_moved.forward()` shim layer have been removed.

| Tree | Path | Purpose |
|---|---|---|
| **Modular** | `coderai/{soul,tools,ui,wire,acp,subagents,background,hooks,skill,plugin,auth,notifications,telemetry,utils,approval_runtime,lsp,terminal,teams,workflow,mcp,network,prompt,session_query,code_mode,goals}/`, `config.py`, `llm.py`, `session.py`, `log.py`, `events.py`, `state.py`, `orchestration.py` | Live implementation and Kimi-mirrored public surface |

### Completed

- **Phases 0–7** of `PORT_PLAN.md`: engine leaves, `soul/`, `tools/`, `ui/`, `wire/`,
  `acp/`, `subagents/`, `hooks/`, `config.py`, `llm.py`; `tests_e2e/` (13 files);
  `scripts/`, `docs/`, `clips/`.
- **Dead code and duplicates removed** (commits `6bb7fae`, `e4c9d50`, `33ee4f3`, `5e06ff6`):
  - `utils/diff.py` (byte-identical to `utils/path.py`), `tools/file/grep_local.py`
    (byte-identical to `glob.py`), `core/lifecycle/*`, `core/common/output_retention.py`,
    `core/subagent_types.py`, `core/turns.py`, `session_fork.py`, `cli/_lazy_group.py`,
    `cli/toad.py`, `utils/windows_paths.py`, 7 orphaned `core/**` shims
  - `cli/metadata.py` merged into `metadata.py` (the two wrote **incompatible JSON
    schemas to the same file** — real data-loss bug); added `record_last_session`,
    `get_last_session_id`, and legacy-schema migration
  - duplicate `ApprovalRuntime` in `soul/approval.py` deleted (its `wait_for()`
    referenced `asyncio` that was never imported); canonical one is
    `approval_runtime/runtime.py`
  - `format_elapsed`, `normalize_line_endings` collapsed to one implementation each
- **Option 3 migration done** — ACP runs on the single live engine:
  - new `coderai/acp/engine.py` (`SessionManagerEngine`) implements the async-iterator
    contract `ACPSession` expects (`run(user_input, cancel_event)` → wire messages)
  - **the parallel Kimi runtime is deleted**: `app.py` (KimiCLI), `soul/toolset.py`
    (`CoderAIToolset`), `soul/slash.py`, `acp/tools.py`, `KimiSoul`,
    `Runtime`/`Agent`/`load_agent`, `soul/context.py`, `ui/shell/visualize/_live_view.py`,
    `_interactive.py`, `ui/shell/replay.py`
  - ACP went from **0 tools to 62 tools** (full `ToolRegistry`, incl. teams)
  - fixed a real bug: `BroadcastQueue.subscribe()` replays history, so every per-turn
    subscriber re-emitted all prior turns' messages. `WireUISide.try_receive_nowait()`
    / `drain_nowait()` exist for this; both the ACP engine and `wire/server.py` use it.

### Verified state at `5e06ff6`

```
unit suite     19/19 files, 311 tests, 0 failures
e2e suite      49 passed, 4 skipped (real-LLM gated)
compileall     clean
ruff           clean on every touched file
ACP             initialize -> session/new -> session/list working, 62 tools
working tree   0 uncommitted
dead code      reachability sweep from product entrypoints: 0 unreferenced modules
```

---

## 2. Operating rules (learned the hard way)

**Interpreter.** Use `/usr/local/bin/python3` — it has `kosong`, `kaos`, `acp`,
`fastmcp`. The default `python3` on PATH (Homebrew) does **not** and will fail on
`import kosong`.

**Tests.** Never run the whole suite in one process (RAM rule from `AGENTS.md`).
Per file:

```bash
/usr/local/bin/python3 -m pytest tests/<file>.py -p no:cacheprovider --benchmark-disable -q
```

Full sweep:

```bash
for t in tests/test_*.py; do
  printf "%-45s " "$t"
  /usr/local/bin/python3 -m pytest "$t" -p no:cacheprovider --benchmark-disable -q 2>&1 | tail -1
done
```

**macOS has no `timeout` command.** Guard hanging tests with `asyncio.wait_for(...)`
inside the test instead.

**Verify a commit independently** (do this after any large commit):

```bash
TMP=$(mktemp -d) && git archive HEAD | tar -x -C "$TMP" && cd "$TMP"
/usr/local/bin/python3 -m compileall -q coderai tests tests_e2e
# then run the suite from there
```

**Don't commit build artifacts.** `.gitignore` covers `*.egg-info/`, caches.
When staging broadly, `git add -A --dry-run` first and grep for
`egg-info|pycache|.benchmarks|.coderai/sessions`.

**Classify every symbol you touch as LIVE or DEAD before deleting.** The working
method used throughout: build an import graph from the product entrypoints
(`coderai/cli/app.py`, `coderai/acp/__init__.py`, `coderai/main.py`,
`coderai/__main__.py`), then check test/script references *and* symbol names.
Two false positives to always rule out:
- `__init__.py` files are executed by submodule imports even if the package is
  never imported directly — never auto-delete them.
- Beware docstring mentions of a symbol (e.g. `_blocks.py` mentions
  `_live_view.py` only in prose) and name coincidences (e.g. `compose_interactive_panels`
  exists both in `cli/app.py` and the deleted `_live_view.py` as separate methods).

---

## 3. Remaining work, in priority order

### P0 — Step 6: relocate the legacy engine (the bulk of the port)

**Status: complete.** All real legacy implementation clusters were moved to the modular tree, `coderai/cli/app.py` moved to `coderai/ui/shell/app.py`, and the legacy `coderai/core/**` tree plus `coderai/_moved.py` were deleted.

These are CoderAI-native features with **no Kimi counterpart**, so they cannot be
"ported from Kimi" — they need a new home in the modular layout. Largest:

```
4065  coderai/cli/app.py          <- the entire interactive CLI (should become ui/shell/)
2260  coderai/core/session.py      <- SessionManager (should become session/ or soul/)
1882  coderai/core/tools/registry.py   <- ToolRegistry (vs Soul A tools/)
 987  coderai/core/workflow/engine.py
 935  coderai/core/prompt.py
 825  coderai/core/tools/executor.py
 759  coderai/core/mcp/manager.py
 649  coderai/core/web_providers.py
 570  coderai/core/tools/ralph.py
 543  coderai/core/events.py
 475  coderai/core/mcp/transport.py
 464  coderai/core/teams/manager.py
 463  coderai/core/common/file_history.py
 457  coderai/core/code_mode/engine.py
 447  coderai/core/state.py
 416  coderai/core/lsp/client.py
 407  coderai/core/sandbox.py
 407  coderai/core/tools/browser.py
 ... (full list reproducible with the reachability script in §5)
```

Rough grouping to consider for target packages:

| Legacy cluster | Suggested new home |
|---|---|
| `core/session.py`, `session_*.py`, `session_store.py`, `state.py`, `session_query/` | `coderai/soul/` (next to `kimisoul.AgentLoop`) or a new `coderai/session/` |
| `core/tools/{registry,executor,schema,types,sanitizer,path_lock}.py` | `coderai/tools/` (or `coderai/soul/toolset.py`) |
| `core/teams/`, `core/goals.py`, `goals_dsh.py`, `goal_round_driver.py` | `coderai/teams/`, `coderai/goals/` |
| `core/workflow/`, `core/tools/ralph.py` | `coderai/workflow/` |
| `core/lsp/` | `coderai/lsp/` |
| `core/mcp/`, `core/mcp_files.py` | `coderai/mcp/` (Kimi has no equivalent) |
| `core/code_mode/` | `coderai/code_mode/` |
| `core/terminal/`, `core/sandbox.py`, `core/spill.py`, `core/schedule.py` | `coderai/terminal/`, etc. |
| `core/prompt.py`, `prompt_sections.py`, `core/common/*` | `coderai/prompt/`, `coderai/utils/` |
| `cli/app.py`, `cli/{info,mcp,plugin,export,doctor}.py` | `coderai/ui/shell/`, `coderai/cli/` |

**Result:** the relocation was executed cluster by cluster, all shims were removed, and no consumer imports legacy paths.

### P1 — Remove the 96 forward shims

**Status: complete.** All forward shims, `coderai/_moved.py`, and the `coderai/core/**` compatibility tree were removed after repointing consumers, tests, scripts, and examples to the modular paths. Legacy-path grep is clean.

### P2 — Phases 9 & 10 (partial)

**Status:** partial. PyInstaller spec/verifier and the headless SDK skeleton exist. Remaining: real binary build verification and SDK integration/real-session validation.

- **Phase 9 — PyInstaller:** `coderai.spec` (ref `kimi.spec`), `coderai/utils/pyinstaller.py`
  (ref `src/kimi_cli/utils/pyinstaller.py`, ~80 lines), `scripts/verify_binary.py`.
  `scripts/inject_build_sha.py` and `scripts/check_dependency_versions.py` already exist.
- **Phase 10 — Headless SDK:** `sdks/coderai-sdk/` with `pyproject.toml`,
  `src/coderai_sdk/client.py`, `models.py`, `tests/test_sdk.py`.
  `coderai/acp/engine.py` shows how to drive a session programmatically without a terminal.

### P3 — Phase 8 skills gap (complete)

**Status:** complete. The five missing skills were added under `.coderai/skills/`.

Planned 5 bundled skills in `.coderai/skills/`; only 2 exist (`security-audit`,
`tdd-workflow`). Missing: `skill-creator`, `feature-smoke-test`, `pull-request`,
`release`, `worktree-status` (refs in `kimi-cli-main/skills/` and `.agents/skills/`).

### P4 — ACP capability gaps (partial)

**Status:** cross-process resume persistence is done. The remaining gaps are:

Deferred during the Option 3 migration; each is a known limitation:

| Gap | Where to work |
|---|---|
| Cross-process resume doesn't rehydrate `SessionManager` history (ACP↔engine id binding is in-process) | `acp/server.py::_setup_session`; persist the mapping |
| ACP-client terminal bridge removed (`replace_tools` was deleted) — Stack A runs local terminal tools instead | would need a `Terminal` bridge over `coderai/terminal/manager.py` |
| MCP injection is process-global via `CODERAI_MCP_CONFIG_JSON`, so concurrent sessions with different servers collide | `acp/server.py::_build_engine` |
| Images are persisted to temp files and referenced in the prompt for `read` (indirect) | `acp/engine.py::build_prompt` |
| Model switching maps ACP model keys onto Stack A's model override; the two config systems can disagree | `acp/server.py::set_session_model` |

### P5 — Small leftovers

- `normalize_proxy_env` duplicate: **done** — canonical in `coderai/utils/proxy.py`.
- `subagent_backends`: moved to `coderai/subagents/backends/` and covered by tests; not dead.
- `core/acp/protocol.py`: removed with the legacy `coderai/core/**` tree.
- `core/session.py` lint debt: removed with the legacy tree; some unrelated pre-existing lint remains across the repo.
- `AGENTS.md` legacy layout: updated for the new modular layout.

---

## 4. Definition of done (per phase, from `PORT_PLAN.md`)

1. Targeted unit + integration tests pass.
2. No regressions: the per-file unit suite stays green.
3. `python3 -m compileall -q coderai tests scripts` is clean.
4. `ruff check` and `mypy` pass (no PEP 695 syntax — Python 3.10 compat).
5. Manual CLI verification: `coderai` runs interactively and `coderai acp` starts.

---

## 5. Reproducing the analysis

Reachability + dead-code sweep used throughout (adjust roots as needed):

```python
import ast, os, re
from collections import deque

def m2p(m):
    p = m.replace(".", "/")
    for c in (p + ".py", os.path.join(p, "__init__.py")):
        if os.path.exists(c): return c

def imp(path):
    src = open(path, encoding="utf-8", errors="replace").read()
    try: t = ast.parse(src)
    except Exception: return set()
    o = set()
    def add(m):
        if m and m.startswith("coderai"): o.add(m)
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom):
            add(n.module)
            if n.module:
                for a in n.names: add(n.module + "." + a.name)
        elif isinstance(n, ast.Import):
            for a in n.names: add(a.name)
    for m in re.findall(r'["\'](coderai(?:\.[a-zA-Z_][\w]*)+)["\']', src): add(m)
    return o

def reach(roots):
    seen, q = set(), deque(roots)
    while q:
        m = q.popleft()
        if m in seen: continue
        seen.add(m)
        for i in imp(m):
            p = m2p(i)
            if p: q.append(p)
    return seen

PROD = reach(["coderai/cli/app.py", "coderai/acp/__init__.py",
              "coderai/main.py", "coderai/__main__.py"])
```

Then for each `.py` under `coderai/` not in `PROD`: skip `__init__.py` and files
whose basename appears in `tests/`, `tests_e2e/`, `scripts/`, `examples/`.
What's left is dead.

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

- **P0/P1 complete at `c66dc64`**: all real legacy implementation clusters were moved
  into the modular tree, `coderai/cli/app.py` moved to `coderai/ui/shell/app.py`, and
  the `coderai/core/**` tree, all `_moved.forward()` shims, and `coderai/_moved.py`
  were deleted.
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

P0 and P1 are complete. The legacy `coderai/core/**` tree, `coderai/_moved.py`, and
all forwarding shims have been deleted. The live implementation is the modular tree.

### Verified state at `c66dc64`

- Full per-file unit sweep: all files pass except 6 known environment-only failures:
  - 3 × `~/.coderai/sessions` permission denied
  - 3 × `out of pty devices`
- E2E: 49 passed, 4 skipped.
- SDK tests: 12 passed.
- `compileall` clean across `coderai`, `tests`, `tests_e2e`, `scripts`, `examples`, and `sdks`.
- `python -m coderai --help` and `python -m coderai.cli --help` work.
- Full `ruff check coderai` still reports ~409 pre-existing errors (mostly E402/F401/F811/F821) outside the P1 migration scope.

### P2 — Finish packaging and SDK integration (partial)

**Status:** PyInstaller artifacts and the headless SDK skeleton exist; real build and
integration validation remain.

- **PyInstaller**
  - Verify `coderai.spec`, `coderai/utils/pyinstaller.py`, and `scripts/verify_binary.py`
    against a real PyInstaller build.
  - Install/add `pyinstaller` where appropriate.
  - Build the binary and run `scripts/verify_binary.py` against it.
  - Fix missing datas/hidden imports, especially skills, agents, prompts, vendor `rg`,
    and dynamic MCP/tool imports.
  - If needed, update `pyproject.toml`, `requirements-dev.txt`, and CI workflow.
- **Headless SDK**
  - Validate `sdks/coderai-sdk/src/coderai_sdk/client.py` against the real
    `coderai.acp.engine.SessionManagerEngine` + `coderai.cli.session_factory.build_session_manager`.
  - Add an optional integration test, gated behind an environment variable so the
    default SDK test remains offline.
  - Confirm the SDK can import without the main repo on `sys.path` after packaging.

### P3 — Skills gap (complete)

**Status:** complete. The five missing skills were added under `.coderai/skills/`:
`skill-creator`, `feature-smoke-test`, `pull-request`, `release`, `worktree-status`.

### P4 — Remaining ACP capability gaps (partial)

**Status:** cross-process resume persistence is done.

1. **ACP-client terminal bridge** — add a `Terminal` bridge over
   `coderai/terminal/manager.py` so ACP clients can drive terminals remotely.
2. **Per-session MCP injection** — stop relying on process-global
   `CODERAI_MCP_CONFIG_JSON` in `coderai/acp/server.py::_build_engine`; make MCP
   config session-scoped so concurrent sessions cannot collide.
3. **Image handling** — avoid persisting images to temp files and referencing them
   indirectly in prompts; wire image blocks through `coderai/acp/engine.py::build_prompt`.
4. **Model switching consistency** — align ACP model keys with Stack A model
   overrides in `coderai/acp/server.py::set_session_model`.

### P5 — Residual cleanup

- Full-repo `ruff check coderai` still reports pre-existing lint debt; decide
  whether to clean by directory or rule category.
- Run `mypy` and fix regressions introduced by the modular import rewrites.
- Optional: rename `scripts/self_check_core.py` to a non-core name and update references.
- Consider updating `README.md`, `PORT_PLAN.md`, and CI references that still describe
  the old `coderai/core/**` tree.

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

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
  `acp/`, `subagents/`, `hooks/`, `config.py`, `llm.py`; `tests_e2e/` (15 files:
  12 `test_wire_*`/`test_mcp_cli` suites + `__init__`/`conftest`/`wire_helpers`
  harness); `scripts/`, `docs/`, `clips/`.
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
`import kosong`. (CI is the exception: workflows use the setup-python matrix
`python`, which installs deps from `pyproject.toml` on the runner.)

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

### Verified state post-P2/P4/P5 (working tree, uncommitted)

```
unit suite     20/20 files, all pass, 0 failures (new: tests/test_acp_terminal.py)
e2e suite      49 passed, 4 skipped (real-LLM gated) — matches baseline
sdk suite      12 passed offline + 4 integration (CODERAI_SDK_INTEGRATION=1)
compileall     clean across coderai, tests, tests_e2e, scripts, examples, sdks
ruff check     clean on every file touched by P2/P4/P5 work
ruff format    clean on every touched hunk (remaining diffs are pre-existing)
mypy           0 new errors (98 baseline → 98 on touched-file closure; new
               acp/terminal.py is clean). Default-config run stays blocked by
               the pre-existing upstream kosong PEP-695 syntax error.
self_check     scripts/self_check.py passes ("All self-checks passed.")
binary         61MB single-file build; --help/--version + archive markers OK
cli smoke      python -m coderai --help / python -m coderai.cli --help work
```

### P2 — Finish packaging and SDK integration (complete)

**Status:** done. Real build + integration validation performed.

- **PyInstaller**
  - `coderai/utils/pyinstaller.py`: hidden imports now cover every first-party
    subpackage (`teams`, `workflow`, `mcp`, `network`, `prompt`,
    `session_query`, `code_mode`, `goals`, `lsp`, `terminal`, `skills`, …);
    `datas` fixed (dropped the stale in-package `CHANGELOG.md` entry — ours
    lives at the repo root — and the meaningless `tools/*.py` exclude);
    new `binaries` slot ships vendored `rg` with its exec bit intact.
  - New `excludes` list drops the ML/notebook/dev stack that static analysis
    reaches through guarded optional imports (`torch`, `transformers`,
    `datasets`, `IPython`, `pandas`, `pygame`, `pytest`, …): the runtime
    never imports them (zero references in `coderai/`, verified by import
    test). Binary went from **438MB / >30s startup to 61MB / ~17s**.
  - `coderai.spec` consumes `binaries` + `excludes`.
  - `scripts/verify_binary.py` now also checks bundled data: onedir layouts
    by glob (`agents/default/agent.yaml`, `prompts/compact.md`,
    `skills/*/SKILL.md`, `tools/*/*.md`), single-file builds via the
    `build/*/PKG-*.toc` archive manifest.
  - Real build run: `dist/coderai` reports the expected version;
    `verify_binary.py dist` passes all checks.
  - `pyinstaller>=6.0` added to `[project.optional-dependencies] dev`.
  - `dist/` + `build/` stay gitignored; no CI workflow change (release.yml
    ships wheel/sdist, not the PyInstaller binary).
- **Headless SDK**
  - `CoderAIClient.create()` validated against the real
    `SessionManagerEngine` + `build_session_manager` (model/plan/skills
    reach the live manager; bind/set_model/interrupt delegate).
  - New `sdks/coderai-sdk/tests/test_sdk_integration.py`, gated behind
    `CODERAI_SDK_INTEGRATION=1` (4 tests; skipped by default so the SDK
    suite stays offline).
  - Standalone packaging confirmed: `pip install ./sdks/coderai-sdk
    --target <dir>` then `import coderai_sdk` with only that dir on
    `sys.path` works (client/engine imports stay lazy).

### P3 — Skills gap (complete)

**Status:** complete. The five missing skills were added under `.coderai/skills/`:
`skill-creator`, `feature-smoke-test`, `pull-request`, `release`, `worktree-status`.

### P4 — Remaining ACP capability gaps (complete)

**Status:** cross-process resume persistence was done; the four open items are done.

1. **ACP-client terminal bridge** — new `coderai/acp/terminal.py`
    (`TerminalBridge` over `coderai/terminal/manager.py`, per-ACP-session
    namespaced ownership) exposed via `ext_method` as
    `terminal/bridge` (capability catalogue) and
    `terminal/list|open|send|read|signal|close`. Timeouts clamped
    (defaults 2s read / 10s send, 30s cap). Covered by
    `tests/test_acp_terminal.py` (9 tests, fake manager — no PTY flakiness).
2. **Per-session MCP injection** — `_build_engine` no longer writes the
    process-global `CODERAI_MCP_CONFIG_JSON`. New
    `_mcp_configs_to_servers()` flattens `MCPConfig`/dict configs to plain
    server dicts and `build_session_manager(..., mcp_servers=...)` merges
    them session-scoped into resolved `mcpServers`. This also fixed a live
    bug: callers pass `MCPConfig` objects, which the old `isinstance(dict)`
    filter silently dropped — ACP-supplied servers never reached the
    engine at all. Covered by 3 tests incl. a two-session no-collision test.
3. **Image handling** — `build_prompt` no longer persists images to temp
    files. New `build_acp_prompt()` splits text + `image_url` content
    params; `SessionManager.create_session`/`reply_session` accept
    `content_params` and store them in the user message
    `meta["contentParams"]`, which `OpenAIMessageConverter` already sends
    as multimodal input when the model supports it (same path as CLI
    `/image`). Oversized images (>20MB, mirroring the CLI limit) are
    dropped with a warning; image-only turns still append on the reply
    path. Covered by 4 engine tests + 2 manager message-meta tests.
4. **Model switching consistency** — `set_session_model` is now fully
    session-scoped: `engine.set_model()` + new `engine.set_thinking()`
    (`,thinking` suffix ↔ `SessionManager` thinking override via new
    `set_thinking_enabled`/`get_thinking_enabled`, wired into turn
    execution). Removed the global `config.toml` rewrite (one session's
    choice leaked to all future sessions) and fixed the stale advertised
    model (`self.sessions` conv is now updated). Covered by 6 tests.

### P5 — Residual cleanup (complete)

- **Ruff debt strategy: clean touched hunks, not the repo.** Full-repo
  `ruff check coderai` debt (~409 pre-existing E402/F401/F811/F821) is left
  for a dedicated lint pass — reformatting whole files under a newer ruff
  (0.15.x vs whatever formatted the tree) would bury functional diffs.
  Every file touched by P2/P4/P5 passes `ruff check`, and every touched
  hunk passes `ruff format --diff` (remaining diffs in those files are
  pre-existing).
- **mypy:** 0 regressions from this work (touched-file closure 98 → 98;
  new `acp/terminal.py` clean). Note the default-config run
  (`python_version = "3.10"`) is wholly blocked by a pre-existing upstream
  `kosong/message.py` PEP-695 `type` statement; advisory runs used
  `--python-version 3.14`. No PEP 695 syntax added to `coderai/`.
- **Rename done:** `scripts/self_check_core.py` → `scripts/self_check.py`
  (README reference updated). Also fixed a latent async drift the rename
  surfaced (`ask_user.handle` is now a coroutine; the script awaited
  nothing and failed at HEAD too) — the self-check passes again.
- **Docs:** `README.md` example now references the live modular paths
  (`soul/session/manager.py`, `soul/approval.py`). `PORT_PLAN.md` keeps its
  single historical `core/` mention (Phase 2b completion record — history,
  not a live reference). No CI references to the old tree remain
  (workflows only touch `coderai/`, `tests/`, `tests_e2e/`, `scripts/`).

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

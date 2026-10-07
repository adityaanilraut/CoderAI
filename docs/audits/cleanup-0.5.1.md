# CoderAI Cleanup & De-Bloat Audit

> Historical audit of the working tree before the 0.5.1 cleanup. File paths,
> counts and recommendations below describe that snapshot, not the current
> release. The implemented changes are summarized in [the changelog](../../CHANGELOG.md).

**Target:** `CoderAI-main` — commit `dd7db96` (`main`, release 0.5.0) **plus 274 uncommitted working-tree changes**
**Date:** 2026-10-07
**Scale:** 334 `.py` files / ~90,972 LOC in `coderai/`; 115 test files / ~32,958 LOC in `tests/` + `tests_e2e/`
**Method:** 10 mandated agent roles — 8 specialist passes in parallel, an adversarial dynamic-dispatch veto pass, and a triage/synthesis pass. Every candidate below was checked against the live tree with concrete searches; claims that failed verification were dropped or downgraded.

---

## ⚠️ Read this first: two conditions that shape every finding

**1. The audit target is a mid-refactor working tree, not a release.**
`git status` shows **184 modified files, 89 untracked files, 1 deletion**, with `+10,438 / −7,415` lines versus `HEAD`. Much of the "new" code is in-flight work that is not yet wired up. Consequently:

- Newly added but not-yet-referenced modules are **not dead code** — they are unfinished work. I verified this for the sample that mattered: every new `coderai/tools/*/definitions.py`, `coderai/ui/shell/*.py`, `coderai/utils/storage.py`, `coderai/cli/migrate.py`, `coderai/openrouter.py` has inbound references (1–39 each).
- Untracked docs (`docs/architectural-parity-*`, `docs/tool-roadmap-*`, `docs/benchmarks/*`, `docs/agent-review-remediation-*`) are **working notes**, not orphaned documentation. They are called out separately and should be archived/finished, not silently deleted.
- Deletions should land **after** the in-flight refactor is committed, so this audit's baseline and the PR baseline are the same.

**2. A prior dead-code pass already ran.**
`docs/type-suppression-budget.json` states its baseline was measured "before dead-code removal and renderer extraction," and `tenacity` has already been dropped from `pyproject.toml`. This codebase is **well-maintained**; the honest headline is that remaining dead weight is a *small fraction* of the tree, not a large one. Numbers below are deliberately conservative and labelled as estimates.

---

## Section 1 — Executive Impact Matrix

| Metric | Finding | Basis |
|---|---|---|
| **Verified dead LOC (whole-unit deletions)** | **~805 LOC** | 56 (`check_version_tag.py`) + 74 (`coderai.spec`) + 211 (`verify_binary.py`) + 146 (`utils/pyinstaller.py`) + 268 (`tests/test_support/`) + ~10 (dead `HookDef`) + ~40 (8 dead flags) |
| **Additional dead internal symbols** | **~150–400 LOC** (estimate) | 130 undecorated zero-reference definitions; the large majority are public-API/framework callbacks, so only a minority are safely deletable |
| **Refactor-recoverable LOC** | **~600–700 LOC** (estimate) | Section 3, dominated by the hooks boilerplate cluster (~half) |
| **Total realistic reduction** | **~1,500–1,900 LOC ≈ 1.7–2.1%** of `coderai/` | Estimate; excludes the 91k figure's blank/comment lines |
| **Files safe to delete immediately** | **4 tracked + 1 directory** | `scripts/check_version_tag.py`; `coderai.spec`; `scripts/verify_binary.py`; `coderai/utils/pyinstaller.py`; `tests/test_support/` |
| **Third-party packages safe to remove** | **0 unconditional** | No declared runtime dependency is unused. `fastmcp-slim[client]` looked like a large win and was **vetoed** — see Section 2 |
| **Removable dev dependency** | **1** — `pyinstaller>=6.0` (~7.3 MB) | Only reachable via the orphaned PyInstaller pipeline |
| **Bundle/size reduction** | **~7.3 MB container/venv**; **0 MB shipped bundle** | Pure-Python package: removing code does not change the wheel materially. A further ~8 MB is available *only* if the `keyring` coupling is resolved first |
| **Dead config keys (env vars)** | **0** | See "Notable negative results" — a 161-variable cross-check found no genuinely unread documented key |
| **Dead test assets** | **1 directory** — `tests/test_support/` (268 LOC) | Zero consumers since a consolidation refactor |

### Notable negative results (verified clean — do not re-audit)

These are as valuable as the findings; each was checked and found **clean**:

- **Accidentally committed cruft: none.** Zero tracked `__pycache__`/`.pyc`, `*.egg-info`, `*.bak`, `*.orig`, `*_old`, `*_v2`, `*_backup`. `.verification/` (20 MB) and `.coderai/sessions/` (2.1 MB of transcripts) are correctly gitignored (only 15 files under `.coderai/` are tracked, all agents/skills/rules).
- **Environment-variable drift: none dead.** A bidirectional cross-check of **161 variables consumed by code** against 44 documented found **no documented-but-unread key**. `CODERAI_*` variables are consumed generically by `config.collect_env("CODERAI_")` (prefix-stripping) and read as `system_env.get("TOOLS_PRESET")` etc. — so naive `os.getenv` scans produce false "dead key" reports. The three apparent misses are all explained: `CODERAI_THINKING` is documented as *historical* (`docs/benchmarks/offline-review-improvement.md:32`), `CODERAI_NOTIFY_WEBHOOK` is a user-side variable used inside a hook example (`coderai/skills/coderai-self-refer/references/notify.md:39`), and `DEEPSEEK_KEY` is a shell example variable in `docs/configuration.md:96`, not a config key.
- **Test skip debt: none.** Only 6 skip sites exist, **all environment-conditional with explicit reasons** (ripgrep absent, sandbox socket restriction, Windows symlink privilege, PTY availability, POSIX-only). Zero `@pytest.mark.skip`/`xfail`/`unittest.skip` decorators. No permanently-skipped suite.
- **Orphaned fixtures/snapshots: none.** All 22 non-autouse fixtures are requested; `tests/` and `tests_e2e/` contain **only** `.py` files, so there are no unreferenced snapshot/data files.
- **CI wiring: clean.** Every CI step resolves to an existing script/path; all `[tool.mypy]` override modules and all `[tool.setuptools.package-data]` globs resolve.
- **`.gitignore`: not dead config.** 33 of 49 patterns match nothing today, but these are correct *preventive* patterns (`.coverage`, `.tox/`, `node_modules/`, `.DS_Store`, …). Reporting them would be a false positive. Only `.coderai/skills/workday-autofill/` looks like a genuinely stale project-specific ignore.
- **No Dockerfiles exist**, so there are no dead multi-stage build layers.

---

## Section 2 — Safe Deletions (Zero Risk)

Grouped by component. Every item below was independently re-verified in this audit.

### Files

| Location | Identifier | Reason |
|---|---|---|
| `scripts/check_version_tag.py` (56 LOC) | `check_version_tag.py` | **Zero references repo-wide.** Superseded: `.github/workflows/release.yml:48-68` performs the identical tag↔`_version.py` validation inline in bash. Tracked file. |
| `coderai.spec` (74 LOC) | PyInstaller spec | The entire binary-packaging pipeline is orphaned. `coderai.spec` is referenced **only by a comment** in `pyproject.toml:76`. No Makefile target, no CI job, no pre-commit hook. |
| `scripts/verify_binary.py` (211 LOC) | `verify_binary.py` | Same orphaned pipeline; also referenced only by the `pyproject.toml:76` comment. Note `Makefile` `dist:` calls `python -m build` (wheel/sdist) and `verify-dist:` calls `scripts/verify_wheel.py` — neither touches the binary path. |
| `coderai/utils/pyinstaller.py` (146 LOC) | `pyinstaller.py` | Zero references anywhere. This is the module the spec's hidden-import logic lives in; it is dead in lockstep with the spec. |
| `tests/test_support/` (268 LOC, 2 files) | `test_support` package | Only self-reference: `tests/test_support/__init__.py:3` imports from its own `llm_replay`. Zero consumers across `tests/`, `tests_e2e/`, `scripts/`. |

> **Ordering constraint:** delete `coderai.spec` + `scripts/verify_binary.py` + `coderai/utils/pyinstaller.py` **together with** the `pyinstaller>=6.0` dev dependency (`pyproject.toml:77`). Deleting the spec alone strands `coderai/utils/pyinstaller.py` as a shipped module importing an undeclared dependency.

### Functions & Symbols

| Location | Identifier | Reason |
|---|---|---|
| `coderai/hooks/config.py:62` | `HookDef` (pydantic model) | **Dead duplicate.** The live model is `coderai/typed_config.py:131`, used at `:201` (`hooks: list[HookDef]`) and imported by `coderai/config.py:39` (`HookDef as HookDef` **from `typed_config`**). The `hooks/config.py` copy has exactly one reference — its own definition. ~10 LOC. |
| `coderai/ui/shell/startup.py:337-389` | 8 dead `--subagent-*` flags: `--subagent-worker`, `--subagent-payload`, `--subagent-depth`, `--subagent-parent-id`, `--subagent-runner`, `--subagent-type`, `--subagent-desc`, `--subagent-allowed-tools` | Parsed with an explicit `dest=` but **never read**: `grep 'args\.subagent_'` returns **zero** hits repo-wide; no `vars(args)` / `args.__dict__` / `asdict(args)` escape hatch exists; and none is constructed as a subprocess command line. These are the remains of an internal worker-IPC protocol that the in-process `coderai/subagents/` execution path superseded. ~40 LOC. |

> **⚠️ Do not delete `--subagent-timeout` (same flag family, line 420).** It is **live**: read at `coderai/ui/shell/app.py:2829` via the `getattr(args, flag, None)` → `CODERAI_*` forwarding loop, and consumed at `coderai/orchestration.py:230`. Seven of the eight dead flags share the `--subagent-*` prefix with it — this is exactly the trap that makes blanket prefix-based cleanup dangerous.

> **Caveat on the dead flags:** removing CLI flags is a public-interface change. These flags do nothing, so risk is low, but they should be removed in a PR that also greps docs/README for mentions.

### Dependencies

| Location | Identifier | Reason |
|---|---|---|
| `pyproject.toml:77` | `pyinstaller>=6.0` (dev extra) | No Makefile target, CI job, or pre-commit hook invokes PyInstaller. Installed size ~7.3 MB. Remove together with the spec + `verify_binary.py` + `utils/pyinstaller.py`. |

**Everything else stays.** Full accounting of the declared runtime set (`pyproject.toml:20-51`), verified against an AST import graph of all 334 files plus importlib/metadata inspection of the installed environment:

- **Directly imported and load-bearing:** `rich` (31 files), `kosong` (18), `pydantic` (10), `prompt-toolkit` (8), `agent-client-protocol`, `requests`, `pyyaml`, `pypdf`, `pathspec`, `tomlkit`, `streamingjson`, `jinja2`, `aiofiles`, `jsonschema`, `aiohttp`, `openai`, `certifi`, `referencing`.
- **Declared but never imported — and correctly so (CVE/floor pins):** `multidict>=6.9.1,<7` (0 direct imports; hard transitive of `aiohttp`) and `PyJWT>=2.15.0,<3` (0 direct imports; transitive of `mcp`, documented at `docs/verification.md:70` as a deliberate CVE floor). Removing either saves **0 bytes** and lowers a security floor. **Keep.**
- **Optional extras correctly placed:** `pillow`/`pyperclip` (guarded in `coderai/utils/clipboard.py:15-21`) and `typesafe-sdk` (guarded by `importlib.util.find_spec` at `coderai/triage/engine.py:179`).

#### ❌ Vetoed: `fastmcp-slim[client]` — the audit's largest tempting false positive

An initial dependency pass identified `fastmcp-slim[client]>=3.2,<4` (`pyproject.toml:43`) as an **unused extra worth ~9.6 MB across 14 distributions**, on the grounds that CoderAI touches exactly one fastmcp symbol (`from fastmcp.mcp_config import MCPConfig`, `coderai/acp/mcp.py:6`) and the extra's headline packages are not directly imported (`mcp`, `httpx`, `starlette`, `authlib`, `py-key-value`, `opentelemetry`: **0 direct imports each**).

**This is unsafe.** The dependency chain is:

```
fastmcp-slim[client]  →  py-key-value-aio[keyring]  →  keyring  →  used by coderai/auth/oauth.py:247
```

`coderai/auth/oauth.py:247-262` (`_load_from_keyring` / `_delete_from_keyring`) imports `keyring` to read and migrate **OAuth tokens**. `keyring` is provided by **no other declared dependency** — only `fastmcp-slim[client]` and the `build[keyring]` extra (also present as the `build` dev tool's optional extra). Dropping `[client]` would not raise an exception (the import is wrapped in a defensive `try/except` that returns `None`) — it would **silently stop keyring-backed OAuth token storage and legacy-token migration**. That is a silent credential regression, strictly worse than a crash.

Two secondary traps in the same removal:

- `fastmcp/mcp_config.py:33` performs a **top-level `import httpx`**, and `httpx` is *not* a core dependency of `fastmcp-slim` (core is only `platformdirs`, `pydantic-settings`, `pydantic[email]`, `python-dotenv`, `rich`, `typing-extensions`). It survives today only because `openai` (declared) also requires `httpx`. So the one symbol CoderAI uses already rides on an undeclared transitive.
- The `PyJWT` CVE floor exists to constrain the `mcp` package, which arrives via both the extra *and* `kosong` (`Requires-Dist: mcp>=1,<2`).

**Correct action:** do **not** drop `[client]` as a "free win." If the ~8 MB (all but `keyring`'s 2 MB) is genuinely wanted, the prerequisite is to **declare `keyring` explicitly first** (and accept/declare the `httpx` transitive), then drop the extra — and re-verify OAuth token storage. Until then, leave it.

#### Undeclared imports that work only transitively (latent risk, low severity)

These are imported directly by `coderai/` but declared nowhere; they resolve only because another dependency happens to pull them in. None is currently broken — they are recorded so a future dependency bump doesn't break the build silently:

| Module | Provided by | Import site |
|---|---|---|
| `keyring` | `fastmcp-slim[client]` → `py-key-value-aio[keyring]` | `coderai/auth/oauth.py:250,266` |
| `setproctitle` | **nothing installed** | `coderai/utils/proctitle.py:15` — silently no-ops (best-effort `try/except`) |
| `loguru` | `kosong` | `coderai/log.py:160` |
| `pygments` | `rich` (core) | `coderai/utils/rich/syntax.py:7` |
| `markdown_it` | `rich` (core, `markdown-it-py`) | `coderai/utils/rich/markdown.py:10` |
| `urllib3` | `requests` | `coderai/utils/aiohttp.py:13` |

### Configs

| Location | Identifier | Reason |
|---|---|---|
| `pyproject.toml:76-77` | PyInstaller dev-extra block | Package comment + `pyinstaller>=6.0` — remove with the pipeline above. |
| `.coderai/skills/workday-autofill/` | `.gitignore` entry | Project-specific ignore for a skill directory that does not exist. The only genuinely stale pattern among 49 (the other 32 no-match patterns are correct preventive rules). |

> **Explicitly NOT config deletions** (they look dead and are not): `MANIFEST.in:5 prune coderai/vendor` and `pyproject.toml:108` `exclude-package-data = ["vendor/rg", ...]`. `coderai/vendor/` does not exist, but `scripts/verify_wheel.py:128,246` **assert** that the built wheel/sdist must not contain `coderai/vendor/rg`. These lines are a release-verification guard. Removing them deletes the guard.
>
> Likewise `[tool.mypy] overrides` and `docs/type-suppression-budget.json` are **in sync** (20 modules each, all existing on disk) — no drift.

### Tests

| Location | Identifier | Reason |
|---|---|---|
| `tests/test_support/__init__.py`, `tests/test_support/llm_replay.py` | `test_support` package (268 LOC) | Only self-referenced; zero consumers. |

---

## Section 3 — Recommended Refactors & Consolidations (Medium Risk)

Ranked by value. Each needs focused regression testing.

### 3.1 Hooks boilerplate cluster — **largest single win (~200–350 LOC)**
**Locations:** `coderai/hooks/runner.py` (638 LOC; 27 `run_*` functions at lines 44–564, plus a ~55-line block duplicated verbatim between `run_hook_point` and `run_hook_point_async`)
**Problem:** 26 near-identical `run_*` hook-fire wrappers plus a duplicated dispatch block — the largest boilerplate cluster in the repo.
**Proposal:** generate the wrappers from a single declarative table of (hook event → payload builder), keeping a thin public function per event so the existing call sites and names are unchanged.
**⚠️ Constraint:** the sync/async twins deliberately differ (sync runs inline, async awaits). Do **not** collapse them via `asyncio.to_thread`. The refactor changes the internal call shape of 26 public functions — do it in its own PR with the hooks test suite as the gate.

### 3.2 Home-rolled env-var number resolvers duplicate `utils/envvar.py:get_env_int`
**Locations:** `coderai/orchestration.py:193` (`_env_int`), `:209` (`_float_pick`), `:220` (`_int_pick`), `coderai/skill/flow/runner.py:325` (`resolve_max_ralph_iterations`)
**Problem:** the project already ships `coderai/utils/envvar.py:8 get_env_int` (used at `coderai/ui/shell/placeholders.py:41-42`), yet four sites reimplement "env var → clamped number".
**Proposal:** replace with `get_env_int` (adding a `get_env_float` sibling). ~40 LOC removed, one clamping semantic instead of four.

### 3.3 `render_status_bar` is a duplicate of `render_statusline` **and has a latent bug**
**Locations:** `coderai/ui/shell/prompt.py:1302-1322` (`render_status_bar`) vs `:1767-1787` (`render_statusline`)
**Problem:** byte-identical except that `render_status_bar` uses the lazily-initialised `_ENGINE` instead of `_DEFAULT_ENGINE`, which yields `AttributeError: 'NoneType' object has no attribute 'render'` when called before `format_status_bar`. Its **only** caller is its own test — which passes solely because that test happens to call `format_status_bar` first.
**Proposal:** delete `render_status_bar` and retarget its test at `render_statusline`. Note the dynamic statusline loader at `coderai/ui/shell/prompt.py:1548` resolves the name **`render_statusline`** — so `render_statusline` is the survivor. Deleting the wrong one of these two would break the statusline provider. ~21 LOC and a real bug fix.
**⚠️ Cost:** it is the only coverage of that render path — retarget the test rather than dropping it.

### 3.4 Duplicate secure-directory helper
**Locations:** `coderai/mcp_oauth.py:28` (`oauth_token_dir`) vs `coderai/auth/oauth.py:169` (`_credentials_dir`)
**Problem:** byte-for-byte identical logic — `get_share_dir() / <name>`, `mkdir(parents=True, exist_ok=True)`, `chmod(0o700)` under `suppress(OSError)` — implemented twice. The duplication is the *permission-hardening* logic, which is exactly the kind of thing that silently diverges and leaves one store with wrong modes.
**Proposal:** extract one helper (e.g. `_secure_dir(name) -> Path`) and have both call it. **They must keep their distinct subdirectories** (`mcp-oauth` vs `credentials`) — the goal is one hardened implementation, not one merged token store.

### 3.5 Near-duplicate orchestration resolvers
**Locations:** `coderai/orchestration.py:249` (`resolve_max_continuable_agents`) vs `:265` (`resolve_max_running_jobs`) — structurally identical.
**Proposal:** one parameterised resolver.

### 3.6 Duplicated slash-command bodies
**Locations:** `coderai/ui/shell/dispatch.py:1057` `cmd_skills` / `:1386` `cmd_hooks`; `:1157` `cmd_yolo` / `:1186` `cmd_afk`; `:1302` `cmd_version` / `:1395` `cmd_upgrade`; `coderai/ui/shell/visualize/_blocks.py:217` `_compose_spinner` / `:236` `_compose_thinking_spinner`
**Proposal:** extract the shared body into a private helper per pair. **Keep the decorated command functions** — they are registered via `@registry.command` decorators.

### 3.7 Implemented-but-unexposed MCP surface (decide: wire up or remove)
**Locations:** `coderai/mcp/manager.py:828 get_mcp_prompt`, `:854 read_mcp_resource`, `:131/:145/:149` session tool-mask trio, `:166 eject_server`, `:202 reload_server`, `:206 list_active_servers`, `:213 set_on_status_changed`, `:375 auto_heal_servers`
**Problem:** each has exactly one reference — its own definition. MCP *prompts* and *resources* are implemented but never surfaced through the tool layer; several lifecycle methods are unwired.
**Proposal:** these are **not** zero-risk deletions (public methods on a public class; a third-party ACP/MCP consumer could use them). Decide deliberately: expose them via tools, or remove with a deprecation note. Do not silently delete.

### 3.8 Declare or drop the soft-optional dependencies
**Locations:** `coderai/utils/proctitle.py:15` (`setproctitle`), `coderai/auth/oauth.py:250` (`keyring`)
**Problem:** `setproctitle` is not installed anywhere, so `set_process_title()` silently no-ops in every supported install; `keyring` is undeclared but functionally load-bearing (see the veto).
**Proposal:** add a `keyring` declaration (prerequisite for 3.7's sibling dependency work) and either declare `setproctitle` in an extra or delete the no-op code path.

### 3.9 Vendored `rich` fork depends on rich private internals
**Locations:** `coderai/utils/rich/markdown.py` (908 LOC, forked from `rich@4d6d631`), `syntax.py`, `columns.py`
**Problem:** a deliberately modified fork (markdown.py diverges ~515 diff lines from the installed upstream) that imports **private** rich APIs (`rich._loop`, `rich._stack`). This is intentional and **not** dead code, but it is a standing upgrade hazard: any `rich` minor release can break it silently.
**Proposal:** no deletion. Add a pinned-version canary test and a comment naming the upstream commit, so `rich` upgrades are caught.

---

## Section 4 — Phase-by-Phase Remediation Plan

### Phase 1 — Zero-Risk Deletions (atomic PRs that cannot change runtime)

**Prerequisite:** commit or stash the in-flight refactor first, so the deletion PR has a stable baseline.

1. `rm scripts/check_version_tag.py` — verified zero references; release validation is inline in `release.yml`.
2. `rm tests/test_support/` — verified zero consumers.
3. Delete the dead duplicate `HookDef` at `coderai/hooks/config.py:62` (keep `typed_config.py:131`).
4. Remove the 8 dead `--subagent-*` flags at `coderai/ui/shell/startup.py:337-389` — **re-confirm `--subagent-timeout` (line 420) survives.**
5. Delete the orphaned PyInstaller pipeline **as one atomic change**: `coderai.spec`, `scripts/verify_binary.py`, `coderai/utils/pyinstaller.py`, and `pyinstaller>=6.0` from `pyproject.toml:77`.
6. Remove the stale `.coderai/skills/workday-autofill/` line from `.gitignore`.

**Verification per PR:**
```bash
.venv/bin/python -m pytest tests/<touched_test>.py -p no:cacheprovider --benchmark-disable -q
.venv/bin/python scripts/verification.py lint
.venv/bin/python scripts/verification.py typecheck   # also enforces the type-suppression budget
.venv/bin/python scripts/check_version_tag.py --help  # expected: command not found
```
Do **not** run the whole suite in one process (per `AGENTS.md`); use per-file invocation or `scripts/verification.py test --suite all`, which isolates files.

**Risk: zero.** No item is imported, registered, dispatched, or documented as public API.

---

### Phase 2 — Dependency & Build Trimming (manifest and CI changes)

1. **Re-measure the dependency graph after Phase 1** — the PyInstaller removal changes the dev graph.
2. **Resolve the `keyring` coupling *before* touching `fastmcp-slim[client]`:** add an explicit `keyring` dependency (or a dedicated extra), confirm `coderai/auth/oauth.py` still stores/migrates tokens, *then* drop `[client]` and verify the ~8 MB saving. **Do not skip this ordering** — dropping the extra first causes a silent OAuth regression.
3. **Do not remove `multidict` or `PyJWT`** — 0-byte savings, and both are deliberate security floors.
4. **Decide on the undeclared transitive imports** (§2 table): declare `pygments`, `markdown_it`, `urllib3`, `loguru`, `keyring` explicitly if you want the wheel to be honest about what it imports; at minimum document them.
5. **`.env.example` documentation pass:** 120 `CODERAI_*` variables are consumed by code but absent from `.env.example`; the genuinely user-facing subset (the `CODERAI_JEV_*` family, the `CODERAI_MODEL_*` trio, `CODERAI_MAX_*` orchestration knobs, `CODERAI_ACCESSIBLE`) should be documented. This is a *docs* change, not a code change.

**Verification:**
```bash
.venv/bin/python scripts/verification.py deps        # constraints satisfied
.venv/bin/python scripts/audit_dependencies.py       # pip-audit gate
.venv/bin/python -m build && .venv/bin/python scripts/verify_wheel.py dist
.venv/bin/python -c "import keyring; print('keyring OK')"   # the veto guard
```

**Risk: medium.** `fastmcp-slim[client]` is the one item where a wrong call degrades credentials silently; the wheel/audit gates do not cover runtime keyring availability, so add an explicit OAuth token-storage test.

---

### Phase 3 — Refactoring & Architecture Flattening (needs focused regression testing)

Ordered by value-to-risk:

1. **§3.3 `render_status_bar`** — smallest change, fixes a real latent `AttributeError`. Do first, with the statusline tests retargeted to `render_statusline`.
2. **§3.2 env resolvers** — mechanical; covered by orchestration/config tests.
3. **§3.4 / §3.5 / §3.6** — duplicate helpers and duplicated command bodies; keep every `@registry.command`-decorated function.
4. **§3.8 soft-optional dependencies** — decide declare-vs-delete for `setproctitle`.
5. **§3.7 MCP surface** — a product decision (expose vs remove), not a mechanical cleanup.
6. **§3.1 hooks boilerplate** — the largest win and the highest risk. Do last, alone, with the hooks suite as the gate.
7. **§3.9 vendored rich** — add the upgrade canary; no structural change.

**Verification:**
```bash
.venv/bin/python scripts/verification.py test --suite all     # isolated per-file execution
.venv/bin/python scripts/verification.py check --coverage --report .verification/tests.json
.venv/bin/python scripts/verification.py lint && .venv/bin/python scripts/verification.py typecheck
```
Plus targeted `tests/test_hooks*.py`, `tests/test_shell_*.py`, `tests/test_orchestration*.py`, and the ACP/wire suites for §3.7.

**Risk: medium.** Behaviour-preserving in intent, but §3.1 and §3.3 touch user-visible rendering and hook dispatch; §3.7 changes interface surface.

---

## Appendix — Adversarial False Positives (vetoed candidates)

These were flagged as dead by mechanical analysis and **proven live**. They are recorded so no future cleanup pass deletes them:

| Candidate | Why it is alive |
|---|---|
| `coderai/ui/shell/prompt.py` `_tab_complete`, `_toggle_plan_mode`, `_toggle_shell_mode`, `_steer`, `_external_editor`, `_ctrl_c_clear`, `_expand_pager`, `_attachment_tray`, `_escape_modal`, `_eof` | Registered via the local `@bind("name")` decorator → `bind_shortcut(kb, name)` (prompt_toolkit `KeyBindings`). Name-unreferenced, runtime-live. |
| `coderai/tools/web/fetch.py:183 handle_startendtag`, `:239 handle_data` | stdlib `HTMLParser` callbacks on `_HTMLToMarkdownParser(HTMLParser)` (`fetch.py:83`), invoked by name by the parser framework. |
| `coderai/kaos/__init__.py` `at_eof`, `feed_data`, `feed_eof`, `readexactly`, `readuntil`, `can_write_eof`, `is_closing`, `writelines` | `runtime_checkable` `Protocol` members (`AsyncReadable`/`AsyncWritable`) — structural interface contract, not callables. |
| `scripts/check_type_budget.py` | Reached indirectly: `scripts/verification.py:253` → `run("scripts/check_type_budget.py")`, wired to `make typecheck` and the pre-commit `coderai-types` hook. |
| `docs/web-tools.md` | "Unreferenced" by markdown link, but referenced by `.env.example:28`. |
| `scripts/install.sh`, `scripts/install.ps1` | Zero in-repo references **by design** — distributed installers fetched by URL (`install.sh` documents its own `raw.githubusercontent.com` fetch line). |
| `--subagent-timeout`, `--max-steps-per-turn`, `--max-retries-per-step`, `--max-ralph-iterations`, `--max-subagent-depth`, `--reasoning-effort` | Zero literal `args.<dest>` reads, but read generically by the tuple loop at `coderai/ui/shell/app.py:2827-2839` (`getattr(args, flag, None)` → `CODERAI_*`). |
| `--debug` | Read as a raw argv token (`"--debug" in argv`, `app.py:2771`), not via `args.debug`. |
| `.coderai/agents/*.md`, `.coderai/rules/*.md`, `.coderai/skills/*/SKILL.md`, `coderai/skills/*/SKILL.md`, `coderai/agents/*/*.yaml`, `coderai/prompt/templates/*.md` | Discovered by glob-based loaders (`coderai/subagents/registry.py:157`, `coderai/prompt/__init__.py:838-844`, `coderai/skill/__init__.py:29-62`, `coderai/agentspec.py:29-30,257`). |
| `coderai/agents/default/system.md`, `coderai/tools/plan/enter_description.md` | Named by manifest (`agent.yaml system_prompt_path`, `enter.py`). |
| `coderai/kaos/{LICENSE,NOTICE,README.md}` | Referenced by `[tool.setuptools.package-data]` and asserted by `scripts/verify_wheel.py:43-44`. |
| `MANIFEST.in:5` `prune coderai/vendor`, `pyproject.toml:108` `vendor/rg` exclude | `scripts/verify_wheel.py:128,246` assert the wheel must **not** contain `coderai/vendor/rg`. Deleting these removes a release guard. |
| `coderai/utils/io.py:21-43 atomic_write_text` (pass-through wrapper) | The lazy import inside the body is load-bearing — it keeps `utils/path`'s `KaosPath`/`tools.legacy.observation` machinery out of low-level importers (`config.py`, `authentication`, `goals/core.py`). Do not flatten. |
| `coderai/utils/path.py:27 normalize_content` vs `:31 normalize_line_endings` | Look duplicate; differ in semantics (CRLF-only vs CRLF+bare-CR). Deliberately not merged. |
| `.gitignore`'s 32 no-match patterns | Correct preventive rules, not dead config. |
| `coderai/jev/client.py:41-73` custom LRU | Justified: runtime-resizable capacity, hit/miss counters, a shared lock, and selective non-caching of failure results — `functools.lru_cache` provides none of these fully. |
| `coderai/soul/message.py` dead helpers | Upstream-kosong parity mirror (docstring lines 18-21); deleting increases future merge drift, and the module itself must stay (`wire/server.py:366` imports `check_message`). |
| `tests/test_ui_review_remediation.py:32-36` importing `coderai.cli.web` | A deliberate **negative** test asserting the removed web frontend stays removed (inside `pytest.raises(ImportError)`). |
| `docs/index.md` | Zero inbound links but is the docs hub (outbound-links nearly every other doc); no `mkdocs.yml` exists. A hub, not a dead file. |

## Appendix — Method & Confidence Statement

- **Coverage:** every claim rests on a concrete search (exact grep/glob counts, AST reference graphs, dependency-metadata inspection, or `git ls-files`), not inspection-by-eye. To reproduce the zero-reference counts, exclude `.venv/`, `.verification/` (which contains a full mirrored copy of the source tree — 418 `.py` files — and roughly doubles naive counts), `__pycache__/`, cache directories (`.ruff_cache/`, `.mypy_cache/`), `.coderai/sessions/`, and `.git/` (whose `index` contains every tracked filename and so self-matches any tracked-path search). This report itself now also matches its own quoted identifiers.
- **Agent 1 (AST & Symbol Reachability) failed on its first pass** (returned no structured findings), and the replacement pass did not return within budget. Its mandate was therefore **re-performed directly in this audit**: a decorator-aware, dynamic-dispatch-aware scan of **3,767 definitions**, of which **186 have zero textual references** — 130 undecorated and 56 decorated/registered. Because most of the 130 are framework callbacks or public API (see the veto appendix), only a minority are safe deletions; this is why Section 2's symbol list is deliberately short. The scan's raw output is reproducible and the specific classifiers (`@bind` registration, `HTMLParser` callbacks, `Protocol` members, `args.*` reads) are documented above with the exact sites checked.
- **Agent 9/10 (adversarial validation and synthesis) payloads were truncated** in transport. Rather than relay them unchecked, both passes were **re-performed directly against the live tree** by verifying every Section 2 and Section 3 item by hand. This is what caught the audit's largest temptation — removing `fastmcp-slim[client]` for ~9.6 MB — by tracing `keyring` through `py-key-value-aio` to `coderai/auth/oauth.py`. Honest limitation: a second independent adversarial agent did not complete, so the veto list reflects one rigorous pass over the high-impact claims rather than two independent passes.
- **All size figures are estimates** derived from installed `RECORD`/directory sizes in this environment, not measured uninstalls. Shared-dependency effects mean a real `pip uninstall` would differ.
- **No file in the repository was modified by this audit.** The working tree was left exactly as found; this report is the only file added.

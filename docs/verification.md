# Verification contract

Create the project virtualenv with Python 3.12+ and run `make dev`. Make and the local pre-commit hooks use `.venv/bin/python`; CI installs the same `[dev,jev]` extras and editable SDK with its selected interpreter. `PYTHON=/path/to/python make check` explicitly selects another installed development environment.

| Command | Scope |
|---|---|
| `make check` | Check-only format/lint, type budget and mypy, runtime/dev/Jev constraints, `pip check`, all offline tests, dependency audit, CLI version |
| `make test` | Main, wire and SDK test files, with SDK integration enabled; CLI version |
| `make test-e2e`, `make test-sdk`, `make test-jev` | Focused wire, SDK or real-SDK calibration suites |
| `make lint`, `make format-check` | `coderai`, `tests`, `tests_e2e`, `scripts`, `sdks/coderai-sdk`, `examples` |
| `make typecheck` | Engine and SDK source; measured suppression budget |
| `make check-deps` | Installed runtime, dev and Jev constraints plus `pip check` |
| `make audit` | Actual installed graph; network access required |
| `make format` | Explicit formatting mutation across the shared Python scope |

`scripts/verification.py` is the common implementation of these targets. Each pytest file runs in a fresh process with cache disabled and benchmarks disabled. `--coverage` accumulates main-suite coverage and requires 28%; it is enabled in the required source CI jobs. `--report PATH` saves per-file commands, exit codes, timings and JUnit counts. Empty scopes, missing required SDK/protocol files, failed collection and unexpected skips fail the gate. Required Jev, SDK and security suites need passing tests and cannot skip.

The four tests in `tests_e2e/test_wire_real_llm.py` remain placeholders. Their skips are explicitly reported as absent live-provider coverage. Real-manager turns with mocked providers run in `test_phase2_protocol_events.py` and `test_phase3_sdk_oauth.py`. These are offline integration coverage, not live provider certification.

Ruff 0.16.8, mypy 2.3.1, requests/PyYAML stubs and pip-audit 2.10.1 are pinned in the dev extra. Shared source checks reject mismatched installed pins. Pre-commit uses three fast, check-only source gates with the same scopes and project environment, rather than resolving a second isolated toolchain. Run `make check` for tests and auditing before submitting; hooks alone do not certify a release.

## Release gates

CI and Release call `.github/workflows/verify.yml` from their own commit and pass `github.sha`. Each required job checks out that exact SHA. The source job also verifies `git rev-parse HEAD`; the release build verifies the same identity before stamping/building. Linux and macOS/Python 3.12 source checks, separate security regressions/audit, and real-SDK Jev calibration must succeed before release build or publication. Windows source compatibility retains the existing advisory status; installed-wheel smoke tests on Linux, macOS and Windows are all required.

The release workflow builds and attests one wheel/sdist pair. Smoke jobs install that same uploaded wheel into clean environments, run CLI/core/search/provenance probes, and audit the graph actually installed there. `--audit-report PATH` preserves inventory and findings on audit failure. Both publishing jobs explicitly depend on source verification and required artifact smoke tests. No check looks for success on an unrelated branch or older commit.

## Dependency policy

`scripts/audit_dependencies.py` snapshots all distributions visible to the selected interpreter and generates exact name/version requirements from that snapshot. `pip-audit --no-deps --disable-pip` scans these pins without resolving another graph. Coverage is checked against the inventory, so a missing or silently skipped third-party package fails. Only `coderai-agent` and `coderai-sdk` are excluded from advisory lookup: they are first-party source/artifacts covered by this repository's gates. Audit tooling is included in the inventory and audit.

Every unexcepted known advisory blocks, regardless of severity or fix availability. Audit/service/collection errors, empty reports and incomplete inventory also block. Exceptions in `docs/security/dependency-audit-policy.json` require an exact package/advisory (including aliases), a reason and an unexpired date within 30 days. The policy currently contains no exceptions. Local `make check` and required release checks therefore fail on known advisories. `requirements.lock` remains a reference runtime resolution; it is neither the audited installation nor the audit's input.

On September 30, 2026 the resolved graph contains AsyncSSH 2.21.1, pinned exactly by Pykaos 0.9.0. It triggers PYSEC-2026-3808/GHSA-2wxc-x7rj-hg8f and CVE-2026-62949/GHSA-rw4j-r22c-9gc3. The patched versions are 2.23.1 and 2.24.0 respectively. There is no published newer Pykaos in the allowed series. Raising AsyncSSH's floor alone cannot resolve Pykaos's exact requirement. Release remains blocked pending a compatible upstream change, an explicitly reviewed replacement, or a justified temporary policy exception. No exception or incompatible dependency override was added during remediation.

Sources: [AsyncSSH SCP advisory](https://github.com/ronf/asyncssh/security/advisories/GHSA-2wxc-x7rj-hg8f), [AsyncSSH event-loop advisory](https://github.com/ronf/asyncssh/security/advisories/GHSA-rw4j-r22c-9gc3), [pip-audit](https://github.com/pypa/pip-audit), [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Remaining typing work

The remaining `ignore_errors` modules are enumerated in `docs/type-suppression-budget.json`, together with diagnostic counts from an unsuppressed run and the next removal tranches. Wildcard exclusions are forbidden. Module count, physical suppressed lines and inline-ignore counts cannot increase without changing the reviewed budget. Session ownership/events, permission planning, tool execution/context, factory and SDK boundaries are checked. Remaining explicit suppressions, `ignore_missing_imports`, dynamic `Any` values and existing inline ignores mean that a passing mypy run is not comprehensive static correctness certification.

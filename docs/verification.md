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

The four empty live-provider placeholders were removed during cleanup; no live-provider certification is present. Real-manager turns with mocked providers run in `test_phase2_protocol_events.py` and `test_phase3_sdk_oauth.py`. Real installed-SDK Jev calibration uses mocked transport, and SDK integration is enabled explicitly. These are offline integration contracts. See the [suite audit](test-suite-audit.md) and [case inventory](test-suite-inventory.json).

The shared test runner creates a fresh temporary HOME/USERPROFILE per file, redirects XDG configuration, strips ambient credentials and model/endpoint/configuration overrides, and removes `PYTEST_ADDOPTS`. Tests supply their own fake providers and credentials. Every skip now fails; there is no placeholder exemption. Direct focused pytest commands should use the same isolation.

Ruff 0.16.8, mypy 2.3.1, requests/PyYAML stubs and pip-audit 2.10.1 are pinned in the dev extra. Shared source checks reject mismatched installed pins. Pre-commit uses three fast, check-only source gates with the same scopes and project environment, rather than resolving a second isolated toolchain. Run `make check` for tests and auditing before submitting; hooks alone do not certify a release.

## Release gates

CI and Release call `.github/workflows/verify.yml` from their own commit and pass `github.sha`. Each required job checks out that exact SHA. The source job also verifies `git rev-parse HEAD`; the release build verifies the same identity before stamping/building. Linux and macOS/Python 3.12 source checks, separate security regressions/audit, and real-SDK Jev calibration must succeed before release build or publication. Windows source compatibility retains the existing advisory status and runs the
explicit `windows` suite (CLI and child lifecycle, approval identity, completion/compaction, rendering, flow/hook
adapters, Jev, bundled local KAOS, MCP overlays, token estimation, wire framing
and SDK). POSIX-only PTYs, signals and chmod/umask contracts run in the full
required Linux/macOS suites. Windows still runs source quality, types, installed
dependency constraints and the complete dependency audit; installed-wheel smoke tests on Linux, macOS and Windows are all required.

The Linux source job installs Bubblewrap and loads `.github/bwrap.apparmor`,
a CI-only AppArmor profile attached to `/usr/bin/bwrap` that permits namespace
creation. It replaces the distribution profile for that launcher on the
ephemeral runner and probes actual PID/network isolation before testing.
This follows [Ubuntu's application-specific namespace guidance](https://discourse.ubuntu.com/t/ubuntu-24-04-lts-noble-numbat-release-notes/39890);
AppArmor's global namespace restriction remains enabled.
Shell/PTY regressions therefore exercise the requested OS sandbox; missing
backends remain a runtime error rather than silently running without isolation.

The release workflow builds and attests one wheel/sdist pair. Smoke jobs install that same uploaded wheel into clean environments, run CLI/core/search/provenance probes, and audit the graph actually installed there. `--audit-report PATH` preserves inventory and findings on audit failure. Artifact uploads explicitly include the hidden `.verification` evidence directory.
Both publishing jobs explicitly depend on source verification and required artifact smoke tests. No check looks for success on an unrelated branch or older commit.

## Dependency policy

`scripts/audit_dependencies.py` snapshots all distributions visible to the selected interpreter and generates exact name/version requirements from that snapshot. `pip-audit --no-deps --disable-pip` scans these pins without resolving another graph. Coverage is checked against the inventory, so a missing or silently skipped third-party package fails. Only `coderai-agent` and `coderai-sdk` are excluded from advisory lookup: they are first-party source/artifacts covered by this repository's gates. Audit tooling is included in the inventory and audit.

Every unexcepted known advisory blocks, regardless of severity or fix availability. Audit/service/collection errors, empty reports and incomplete inventory also block. Exceptions in `docs/security/dependency-audit-policy.json` require an exact package/advisory (including aliases), a reason and an unexpired date within 30 days. The policy currently contains no exceptions. Local `make check` and required release checks therefore fail on known advisories. `requirements.lock` remains a reference runtime resolution; it is neither the audited installation nor the audit's input.

Version 0.5.0 removes the external PyKAOS dependency, whose 0.9.0 release pins
vulnerable AsyncSSH 2.21.1. Its local filesystem/path/context adapter is bundled
under `coderai.kaos`, with the upstream Apache-2.0 license, notice and provenance.
CoderAI's ACP fallback uses that adapter; the unused SSH backend is excluded.
This removes AsyncSSH from fresh installations without overriding upstream
metadata or adding an advisory exception. `PyJWT>=2.15.0,<3` prevents the
CVE-2026-101918 version from entering MCP's authentication dependency graph.
`requirements.lock` has been regenerated from the updated runtime constraints.
Existing environments should reinstall the development extras and remove the
obsolete PyKAOS/AsyncSSH packages if no other application needs them. The audit
continues to scan the entire selected environment, including unused packages.

Sources: [AsyncSSH SCP advisory](https://github.com/ronf/asyncssh/security/advisories/GHSA-2wxc-x7rj-hg8f), [AsyncSSH event-loop advisory](https://github.com/ronf/asyncssh/security/advisories/GHSA-rw4j-r22c-9gc3), [pip-audit](https://github.com/pypa/pip-audit), [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Remaining typing work

The remaining `ignore_errors` modules are enumerated in `docs/type-suppression-budget.json`, together with diagnostic counts from an unsuppressed run and the next removal tranches. Wildcard exclusions are forbidden. Module count, physical suppressed lines and inline-ignore counts cannot increase without changing the reviewed budget. Session ownership/events, permission planning, tool execution/context, factory and SDK boundaries are checked. Remaining explicit suppressions, `ignore_missing_imports`, dynamic `Any` values and existing inline ignores mean that a passing mypy run is not comprehensive static correctness certification.

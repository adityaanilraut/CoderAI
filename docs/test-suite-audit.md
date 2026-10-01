# Test-suite audit

September 30, 2026 cleanup, phases 4–7. The complete
[case inventory](test-suite-inventory.json) records every collected node ID,
parameter variant, test definition/contract, fixture, and supporting Python file
under `tests/`, `tests_e2e/`, and `sdks/coderai-sdk/tests/`. Collection used one
pytest process per file, with SDK integration enabled and isolated homes.
The initial source inventory is `.verification/cleanup-suite-inventory-before.json`;
the final collection evidence is `.verification/cleanup-collection.json`.

Phase 3 had 1,137 passing cases, four empty skips and 83 test files. Final cleanup
had **1,186 passing cases, zero skips and 86 independently executed test files**.
The 0.5.0 inventory adds six bundled local-adapter contracts and six archive
cache rejection regressions plus Windows task-listing coverage, for 1,199 cases
across 87 test files.
The four placeholders were the only removed cases. No live-provider coverage was
added or implied by the zero-skip result. Fixtures remain local to matching
boundaries; no universal mock framework was introduced.

## Removal and replacement decisions

| Removed case in `tests_e2e/test_wire_real_llm.py` | Decision and retained coverage |
| --- | --- |
| `test_work_dir_prompt` | Always skipped, body only `pass`; no behavioral coverage to lose. Offline workspace/config/turn contracts remain in `test_wire_config.py`, `test_wire_prompt.py`, `test_phase2_protocol_events.py`, and `test_file_context.py`. Live provider/workdir certification remains absent. |
| `test_parallel_task_subagents` | Always skipped, body only `pass`. Existing child/delegation tests remain in `test_subagents.py`, `test_phase2_child_lifecycle.py`, `test_phase2_teammate_context.py`, `test_subagent_permissions.py`, and `test_tool_dispatch.py`. No live parallel-agent certification. |
| `test_thinking_mode_toggle` | Always skipped, body only `pass`. Thinking and stream behavior remain in `test_prompt_context.py`, `test_completion_streaming.py`, `test_wire_prompt.py`, and `test_cli.py`. No live reasoning-mode certification. |
| `test_cancel_prompt` | Always skipped, body only `pass`. Cancellation and provider/wire interruption remain in `test_phase1_cancellation.py`, `test_completion_streaming.py`, `test_review_phase5.py`, and `test_phase2_protocol_events.py`. No live cancellation certification. |

Removed the now-empty placeholder file and its `OPTIONAL_FILES`/skip exemption
in the shared runner. All skipped cases now fail. No tests were moved or renamed;
discovery patterns and CI/Make invocations remain valid. New approval identity and
credential tests are required files and are included in the security suite;
atomic writer cases are also required. SDK, real installed-SDK Jev calibration,
and protocol requirements remain intact.

AST body comparison across the complete initial source inventory found identical
bodies only in those four placeholders. This was a candidate-screening aid, not
proof that differently shaped tests are unique. Review of observable inputs,
outcomes and boundaries did not justify another removal. Historical filenames
are retained to preserve regression provenance.

| Potential overlap reviewed | Why cases remain separate |
| --- | --- |
| `test_review_phase3/4.py`, `test_security.py`, `test_security_fixes.py`, `test_phase1_authorization.py` | Policy evaluation, untrusted config, SSRF/redirects, shell invocation, and exact-call escalation are distinct boundaries. Real execution grants, project trust, and owner routing are stronger than schema/source checks but do not replace their other cases. |
| `test_tools.py`, `test_tool_platform.py`, `test_review_phase6.py` | Handler results, registry/executor lifecycle, stale edits, readonly/symlink/path enforcement, and cancellation locks have different failure modes. |
| `test_session_engine.py`, `test_review_phase5.py`, `test_runtime_parity.py`, `test_session_storage.py` | Ordinary lifecycle, retry/compaction/error pairing, D-Mail/checkpoint rewind, serialization/modes/orphan recovery, and provider-delta assembly are distinct. |
| `test_subagents.py`, `test_orchestration.py`, `test_review_phase7.py`, child/process/ownership files | Role discovery, loop limits, denial/inheritance, actual process reaping, timeout/cancellation, teammate contexts and owner shutdown cannot be reduced to one mock-heavy smoke test. |
| `test_cli.py`, UI historical files, `test_phase5_rendering.py` | Parser/menu/approval/undo wiring, terminal chrome, streamed block commitment, and render fallback regressions observe different outputs. |
| `test_review_phase1/8/9.py`, `test_review_fixes.py` | Distinct prompt/spec/template, imports/exports, session recall, telemetry and removed-frontend invariants remain. Source/asset assertions with a packaging or forbidden-dependency purpose remain useful. |
| Wire unit/E2E files and SDK files | Framing/serialization/error envelopes differ from real-manager callback ownership and SDK public types. Offline integration is enabled, not replaced by engine-only fakes. |
| Jev calibration, System-One, triage and review files | Installed response schemas, malformed/low-confidence fail-safe behavior, cache/secret boundaries and pipeline presentation each retain unique coverage. |
| Phase 2–3 characterization files | Shared helpers still need adapter equivalence, intentional normalization differences, callback ordering, partial errors, permissions and recovery assertions. All retained. |

Two low-value source-string cases were strengthened in place:
`test_config_ignores_project_notify` now observes user/project/environment
precedence for trusted and untrusted projects;
`test_terminal_scrubs_secrets` observes the actual spawn environment, explicit
secret override, ambient-token removal, and descriptor cleanup after failure.
The type-budget regression now tests exceeding the configured maximum rather
than assuming the current measurement is exactly at that maximum.

## Coverage assessment and justified corrections

Existing tests already cover partial completion/retry/sync fallback and failure
cleanup (`test_completion_streaming.py`, `test_review_phase5.py`), cancellation
and exactly-once dispatch settlement (`test_tool_dispatch.py`, authorization and
cancellation files), process/owner cleanup and late approvals (child lifecycle,
process, runtime ownership and session approvals files), persistence recovery
(`test_session_storage.py`), protocol/SDK ownership, and real-SDK Jev fail-safe
calibration. These contracts were retained. Missing observable contracts were
approval ID reuse, text codecs/errors, privacy before replacement, rejected-key
routing, displayed search totals, and the stdio example's EOF/process lifecycle.

| Added/strengthened boundary | Reproduction and correction |
| --- | --- |
| Approval identity (5 cases) | Five failures before fixing `create_request`. Reused explicit IDs raise `ValueError` before registry/events, including resolved/cancelled records. Existing waiter, source cancellation and owner-filtered wire decisions stay attached to the original record. Generated IDs pass the same collision guard. |
| Atomic text (20 added cases) | Nine failures in the initial 17 additions; three further mode/error cases characterize policy. Sync/async writers honor codec aliases, Latin-1/CP1252, strict failures and replacement errors. Low-level writer gains a trailing `errors` argument; `utf16le` and encoded byte counts remain compatible. Old bytes/modes and temporary cleanup are checked on failure. General `0644`, preserved modes and explicit `0600` remain. |
| Credentials/private persistence (16 cases) | Ten failed before the fixes. Settings and OAuth pass `0600` at exclusive temporary creation even when replacing an old public file. Kimi/Moonshot no longer reintroduce `sk-proj-` explicit credentials or inherit ambient `OPENAI_API_KEY`. Dedicated Kimi/Moonshot, OAuth and non-project explicit precedence stays intact; OAuth-disabled routing is checked without relying on swallowed exceptions. |
| Search cards (6 cases) | Four failed before fixing the displayed count. The card uses result `count` (including zero) instead of header/context rows. Grouped output and legacy flat text retain literal paths with spaces/colons, escaping, context, truncation and spill locator text. No active automatic file links exist here; the old link candidate does not justify adding a UI feature. |
| Stream-JSON example (1 case) | A real local child reading through EOF reproduced a hang. The example now uses the current `prompt` field and launch-prompt requirement, closes stdin, consumes JSON lines and reaps the process, including cancellation/error cleanup. No provider call is made by the regression. |
| Verification isolation (1 case) | The runner supplies distinct temporary homes per file, strips credentials/config/endpoint overrides and forces SDK integration. Skip/empty-collection and audit/type gates remain enforced. |

Before-fix evidence: `.verification/cleanup-reproductions-before.json`,
`.verification/cleanup-search-before.txt`, and
`.verification/cleanup-example-before.json`. These are actual failing runs.
The final full-suite report is `.verification/cleanup-final-tests.json`;
its log includes the **59.02% main-suite coverage** result (28% required).

## Documentation and artifact disposition

README, AGENTS, contributor/security instructions, architecture, CLI/configuration,
verification, navigation, the stdio example, and bundled configuration/notification/permission
references now reflect the active implementation. The audit retains removal decisions and behavior corrections; completed planning
and handoff documents were removed for 0.5.0. Benchmark claims were
left unchanged; no new external benchmark certification is asserted.

Only the empty test artifact was removed in phases 4–7. No additional obsolete
asset was demonstrated. `telemetry_debug_server.py` has behavioral tests and is
an event receiver, not an HTML frontend; `self_check.py` is documented and usable;
installer/build verification scripts are used by workflows/tests; sample plugin
and stdio examples remain standalone examples. Package globs and `MANIFEST.in`
retain skill scripts, references, prompts, agent specs and required source files.
Lockfiles, ignored evidence and user data/assets were preserved.

## Verification limitations

The initial cleanup passed local regression/source gates on macOS with Python
3.14.7, but dependency audits blocked release on AsyncSSH 2.21.1 and PyJWT 2.14.0.
Version 0.5.0 replaces the external PyKAOS dependency with its licensed local
adapter and requires patched PyJWT. See the [verification contract](verification.md)
for the retained audit policy and [release notes](../CHANGELOG.md) for this change.
CI verifies Linux/macOS source gates and Windows compatibility; release gates
also require installed-wheel probes on all three platforms. Offline tests do not
certify live providers or signed standalone binaries.

## Per-file inventory

Counts include all collected parameter variants. Supporting fixtures are listed
in the JSON inventory, including both conftests, wire helpers and the replay fake.

| Test file | Cases | Contract scope |
| --- | ---: | --- |
| `sdks/coderai-sdk/tests/test_sdk.py` | 12 | Self-contained tests for the headless CoderAI SDK (no engine, no network). |
| `sdks/coderai-sdk/tests/test_sdk_integration.py` | 4 | Integration tests for the headless SDK against the real engine. |
| `tests/test_acp_engine.py` | 14 | Tests for the Stack A (``SessionManager``) ACP engine adapter. |
| `tests/test_acp_server.py` | 28 | Unit tests for Phase 3: ACP Server and Kaos Integration. |
| `tests/test_acp_terminal.py` | 9 | Tests for the ACP terminal bridge (``coderai/acp/terminal.py`` + ext_method). |
| `tests/test_approval_identity.py` | 5 | Approval identity must survive collisions and owner shutdown. |
| `tests/test_approval_yolo.py` | 17 | Approval UI + YOLO/AFK wiring. |
| `tests/test_atomic_file_ops.py` | 22 | Unit tests verifying atomic file-write guarantees. |
| `tests/test_background_wire.py` | 22 | Consolidated background/wire: jobs, flows, notifications, wire server, telemetry, hooks, atomic IO. |
| `tests/test_cli.py` | 26 | Consolidated CLI tests: parser, slash catalog, renderers, menus, input edges. |
| `tests/test_cli_subagent_process.py` | 8 | Exercise the production async CLI process path without external providers. |
| `tests/test_compaction_conversion.py` | 4 | Compaction conversion depends on an explicit callable interface. |
| `tests/test_completion_streaming.py` | 4 | Provider stream assembly: ordering, partial failures and cancellation. |
| `tests/test_config_setup.py` | 21 | Consolidated config + setup tests; offline only (probe runs on a mock client). |
| `tests/test_credential_boundaries.py` | 16 | Offline credential routing and private secret persistence boundaries. |
| `tests/test_e2e.py` | 6 | Offline-only in-process e2e: info, validation, version, export. |
| `tests/test_file_context.py` | 6 | File context adapters accept explicit path values and enforce isolation. |
| `tests/test_flow_parsers.py` | 8 | Shared flow inference must preserve both syntax adapters and validation. |
| `tests/test_hierarchical_config.py` | 4 | Unit tests verifying the 5-tier hierarchical configuration cascade. |
| `tests/test_hook_payloads.py` | 13 | Compatibility contracts for lifecycle event payload entry points. |
| `tests/test_jev_calibration.py` | 15 | Jev calibration/boundary tests against the real typesafe_sdk response schema. |
| `tests/test_jev_system_one.py` | 39 | Regression tests for Jev System-One fail-safe contract + fixes. |
| `tests/test_local_kaos.py` | 6 | Bundled adapter: lexical paths, task-local backend isolation, codecs, directory operations and real child process I/O/kill/reaping. |
| `tests/test_mcp_overlays.py` | 3 | MCP layers preserve precedence and distinct settings/overlay normalization. |
| `tests/test_mcp_plugins.py` | 18 | Consolidated MCP transports, OAuth, plugins, skills, and plan review. |
| `tests/test_orchestration.py` | 13 | Consolidated orchestration tests: subagents, goals, teams, jobs, guards. |
| `tests/test_phase1_authorization.py` | 37 | Exact-call sandbox grants and hook decisions through real dispatch/turns. |
| `tests/test_phase1_cancellation.py` | 6 | Path locks outlive cancelled callers while their synchronous workers run. |
| `tests/test_phase1_duplicate_ids.py` | 14 | Tool-call identities remain unique before grants and batch dispatch. |
| `tests/test_phase1_mcp_redirects.py` | 24 | Offline HTTP transport regressions for redirects and surfaced failures. |
| `tests/test_phase1_terminals.py` | 21 | Session ownership and immutable-policy reuse regressions for persistent PTYs. |
| `tests/test_phase2_child_lifecycle.py` | 5 | Actual foreground spawn handles remain cancellable only while running. |
| `tests/test_phase2_engine.py` | 7 | Unit tests for Phase 2: Engine Core (kosong + kaos). |
| `tests/test_phase2_event_lifetime.py` | 22 | Bounded replay and explicit lifetime of live wire subscriptions. |
| `tests/test_phase2_hub_routing.py` | 8 | Runtime-owned root events cannot reach another session's wire client. |
| `tests/test_phase2_protocol_events.py` | 20 | Protocol regressions through real runtime managers and mocked providers. |
| `tests/test_phase2_runtime_ownership.py` | 12 | Project-local schedules and manager-owned shutdown through real runtime objects. |
| `tests/test_phase2_teammate_context.py` | 14 | Exercise teammate tasks through the real spec builder and child runner. |
| `tests/test_phase3_packaging.py` | 30 | Portable installed search, selected interpreter, and source-stamp regressions. |
| `tests/test_phase3_sdk_oauth.py` | 31 | Production SDK types and runtime OAuth boundaries, with offline providers. |
| `tests/test_phase3_session_approvals.py` | 26 | Session grants agree across public adapters and never widen exact-call grants. |
| `tests/test_phase4_ui.py` | 6 | Unit tests for Phase 4: shell branding, theme, status, nudge, and update helpers. |
| `tests/test_phase4_verification.py` | 21 | Release-gate, isolated test runner, audit policy and type-budget regressions. |
| `tests/test_phase5_rendering.py` | 21 | Behavioral contracts for streaming and fallback terminal renderers. |
| `tests/test_phase6_tooling.py` | 10 | Tests for developer tooling scripts. |
| `tests/test_plan_mode_tools.py` | 10 | Plan-mode entry/exit, arguments and idempotence. |
| `tests/test_prompt_context.py` | 43 | Consolidated prompt-context: usage formats, cache prefixes, effort, streams, errors, refs. |
| `tests/test_review_command.py` | 27 | /review pipeline: diff collection, Jev triage/gate wiring, and rendering. |
| `tests/test_review_fixes.py` | 6 | Fixes from the product review: /init, session recall, live /agents, telemetry opt-in. |
| `tests/test_review_phase1.py` | 25 | Regression tests for the 2026-09-22 Phase 1 remediation items. |
| `tests/test_review_phase3.py` | 18 | Phase 3 Security and Permission Model Regression Tests. |
| `tests/test_review_phase4.py` | 33 | Phase 4 trust-boundary, secrets, and untrusted-output regression tests. |
| `tests/test_review_phase5.py` | 17 | Phase 5 agent loop and session correctness regression tests. |
| `tests/test_review_phase6.py` | 20 | Regression tests for Phase 6 (Tools and shell hardening). |
| `tests/test_review_phase7.py` | 16 | Regression tests for Phase 7 (Workflows, orchestration, and event-loop hygiene). |
| `tests/test_review_phase8.py` | 15 | Phase 8 regression test suite: prompt <-> code alignment. |
| `tests/test_review_phase9.py` | 16 | Phase 9 regression test suite: Consolidation, file splits, and dead-code sweep. |
| `tests/test_runtime_parity.py` | 5 | Runtime parity: token cap, context checkpoints, and D-Mail rewind. |
| `tests/test_search_rendering.py` | 6 | Search output, context, literal markup, and existing terminal link policy. |
| `tests/test_security.py` | 19 | Consolidated security rails: SSRF policy, permissions, sandbox, and secrets. |
| `tests/test_security_fixes.py` | 13 | Regression tests for multi-agent review fixes (security hardening). |
| `tests/test_session_engine.py` | 31 | Consolidated session-engine tests: lifecycle, store, events, compaction, state, retry, jobs, query. |
| `tests/test_session_storage.py` | 6 | Session replacement contracts: exact bytes, file modes and orphan recovery. |
| `tests/test_stream_json_example.py` | 1 | The stdio example sends EOF and reaps its child without a live provider. |
| `tests/test_subagent_permissions.py` | 11 | Characterize child policy precedence through the complete isolated loop. |
| `tests/test_subagents.py` | 21 | Consolidated subagent execution-engine tests. |
| `tests/test_token_estimation.py` | 11 | Request budgets and UI estimates deliberately use different heuristics. |
| `tests/test_tool_dispatch.py` | 3 | Dispatch boundaries retain chunk, persistence, callback and abort ordering. |
| `tests/test_tool_platform.py` | 15 | Tool platform tests: registry validation, permissions, guards, disposal, cleanup contracts. |
| `tests/test_tools.py` | 21 | Consolidated tool tests: canonical file/shell tools, search, str_replace, terminal, web (mocked). |
| `tests/test_triage_engine.py` | 4 | Unit tests for CoderAI System 1 TriageEngine. |
| `tests/test_ui_alignment.py` | 10 | Alignment and chrome consistency for the terminal UI. |
| `tests/test_ui_p0_fixes.py` | 8 | P0 UI-review fixes: fail-closed approval, undo wiring, provider validation. |
| `tests/test_ui_review_remediation.py` | 7 | Unit tests for UI Review & Remediation fixes. |
| `tests/test_web.py` | 20 | Consolidated web subsystem: providers, cache, sanitizer, fetch, and cards. |
| `tests/test_wire_framing.py` | 14 | Wire adapters preserve JSON-RPC fields, including omitted versus null data. |
| `tests_e2e/test_mcp_cli.py` | 2 | CLI tests for ``coderai mcp`` server management. |
| `tests_e2e/test_wire_approvals_tools.py` | 6 | Tool approval boundary tests over the wire protocol. |
| `tests_e2e/test_wire_auth.py` | 3 | Wire protocol tests for auth-related error handling. |
| `tests_e2e/test_wire_config.py` | 2 | Wire-driven configuration tests: inline config strings and CLI model overrides. |
| `tests_e2e/test_wire_errors.py` | 8 | Wire errors and lifecycle settlement. |
| `tests_e2e/test_wire_prompt.py` | 7 | Prompt and event-stream tests over the wire protocol. |
| `tests_e2e/test_wire_protocol.py` | 4 | Wire protocol framing tests: handshake, external tools, and init-less prompts. |
| `tests_e2e/test_wire_question.py` | 4 | E2E tests for AskUserQuestion via the Wire protocol. |
| `tests_e2e/test_wire_sessions.py` | 5 | Session lifecycle tests over the wire protocol. |
| `tests_e2e/test_wire_skills_mcp.py` | 5 | Skill and MCP integration tests over the wire protocol. |
| `tests_e2e/test_wire_steer.py` | 3 | Wire errors and lifecycle settlement. |

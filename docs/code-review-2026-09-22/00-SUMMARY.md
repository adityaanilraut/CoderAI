# Executive summary — CoderAI code review (2026-09-22)

Full per-area reports are in `findings/`, and the remediation plan is in `PLAN.md`.

## Headline

1. **The permission model doesn't hold for subagents and agent roles.**
   - "Read-only" subagents can still run `bash` and `str_replace_editor`.
   - `code-reviewer` (`tools: []`) and unknown subagent types get all 47 tools.
   - `planner`, `architect` and `security-reviewer` get **zero** tools because of case-sensitive name matching.
   - `allowedTools` for `--agent`/`/agent` is written but never read.
   - Subagent tool calls skip approval, sandbox mode and plan mode.
   - One session's YOLO/AFK setting auto-approves tool calls in every other session in the process.
2. **The uncommitted tree brings back about 7,000 lines of the "Kimi runtime"** that commit `5e06ff6` deliberately deleted: `coderai/app.py`, `KimiSoul`, the `Shell` class, `_live_view.py`, `_interactive.py`, `replay.py`, `CoderAIToolset` and the kosong tool classes. Nothing reachable from the `coderai` entry point uses it, it is broken on import, and five new test files test only it.
3. **There is no workspace trust boundary.** A cloned repo's `.coderai/settings.json` and `.env` can start arbitrary MCP commands, redirect the user's API key via `baseURL`, and set `CODERAI_OAUTH_HOST`. `~/.coderai/settings.json` holds API keys and is world-readable (0644).

## Numbers

- About 170 raw findings from the seven reviews. About 60 of them are curated in the canvas.
- Ruff pyflakes (F rules) is clean. All 82 B023 loop-closure hits are false positives. The B904 hits are low severity.
- Vulture reported about 340 unused functions, methods and classes. About 190 are **verified dead** and about 100 are false positives.
- There are 10 orphan or test-only modules, and 25 groups of identical function bodies (about 340 redundant lines).
- All 47 test files collect cleanly.

## Verified by hand (spot checks)

| ID | Finding | Location |
|---|---|---|
| WF-A1 | Deleting a session deadlocks: a non-reentrant `threading.Lock` is taken twice | `coderai/background/agent_runner.py:143-168` |
| TL-A1 | The executor re-runs the handler on `TypeError`, so side effects happen twice | `coderai/tools/legacy/executor.py:546-556` |
| TL-A3 | `%%d` inside an f-string means bash never captures the exit code or cwd | `coderai/tools/shell/__init__.py:333` |
| IN-A3 | `asyncio.Queue.shutdown` / `QueueShutDown` are Python 3.13+ but the target is 3.12 | `coderai/wire/server.py:118,151,181` |
| DC-E | `_typed_global_knobs` reads `typed.mcp.tool_call_timeout_ms`, but the field is `typed.mcp.client...`, so it always returns `{}` | `coderai/config.py:204` |
| WF-A4/A5 | Read-only filtering blocks only write/edit; `[]` allowlist means unrestricted; case-sensitive names | `coderai/subagents/runner.py:958,1042-1046` |

## Cross-cutting patterns

- **Parallel systems that drifted apart:**
  - two agent runtimes (`AgentLoop`/`SessionSoul` is live; `KimiSoul` is dead);
  - two tool systems (the legacy registry is live; the kosong classes are dead but still getting new features);
  - three slash-command registries and four session/state modules;
  - two hook engines with opposite fail-open/fail-closed semantics;
  - three compaction trigger paths and three plan-mode instruction sets;
  - five ways to spawn a subagent;
  - four secret scrubbers and three redactors.
- **Silent failure:** about 60 `except Exception: pass` blocks in `soul/session/manager.py` and 21 in `coderaisoul.py`. Several real bugs are hidden this way: the config knobs, the plan-file path, doctor's MCP status and frontmatter parsing.
- **Untrusted text treated as trusted:**
  - Rich markup rendered from tool, web and session-title output, which can crash or spoof the display, including the approval card;
  - AGENTS.md and skills injected as system messages;
  - project config able to start processes and choose the API host.
- **Blocking I/O on the event loop:** hooks, `!` shell mode, approval prompts, `$EDITOR`, MCP stdio writes, JSON stores, and uncancellable LLM streaming threads.
- **Cancellation handling:** `CancelledError` is swallowed in `AgentLoop.run` and in the subagent runners, and fire-and-forget tasks are created without keeping a reference.

## Top findings by area

**Workflows**
- WF-A1: session-delete deadlock.
- WF-A2: subagents swallow cancellation.
- WF-A3: the depth limit can be bypassed.
- WF-A4/A5: read-only and allowlist handling is inverted.
- WF-A6: subagents skip approval.
- WF-A7: shadow job, agent and schedule stores.
- WF-A8/A9: workers that never exit, and a teammate ack storm.
- WF-A10: schedules fire on restart.

**Tools**
- TL-A1: double run.
- TL-A3: bash exit code.
- TL-A4: the sanitizer corrupts `read` output.
- TL-A5: `timeout_ms` is ignored.
- TL-A7: foreground bash output buffer has no limit.
- TL-A9/A10: pwsh protections and jobs that never finish.
- TL-A11: `edit` stale-read check is defeated.
- TL-A13: non-UTF-8 files are corrupted.
- TL-A17: the Linux read-only sandbox has network access.

**Agent loop**
- AL-A1: compaction re-fires every step.
- AL-A2: YOLO/AFK leak.
- AL-A3: an empty summary still hides history.
- AL-A4/A5: cancellation handling and stuck "processing" state.
- AL-A6: a stale interrupt kills the next prompt.
- AL-A8: uncancellable stream.
- AL-A10: plan mode is stuck on after exit.

**Prompts**
- PR-A3: `allowedTools` is never read.
- PR-A4: explore/plan lose their read-only prompt.
- PR-A5: unrendered template text is sent to the model.
- PR-B1: `code-reviewer.md` contains a benchmark answer key and contradicts itself.
- PR-B2: the live prompt lacks the safety guardrails.
- PR-B3: three contradictory plan-mode prompts.
- PR-B4: unmarked untrusted instructions.
- PR-C1: frontmatter parsing fails open.

**Terminal UI**
- UI-A1: a slash-command exception kills the REPL.
- UI-A2/A3/A4/A14: Rich markup injection.
- UI-A5: Ctrl-C leaves the queue locked.
- UI-A6: `/logout` doesn't remove keys.
- UI-A7/A9/A10: blocking the event loop.
- UI-A15: `/effort xhigh` is rejected.
- UI-A16: API keys are echoed while typed.

**Infrastructure**
- IN-A1: no workspace trust.
- IN-A2: 0644 secrets.
- IN-A3: Python 3.13-only asyncio API.
- IN-A4: SSE token redirect.
- IN-A5: MCP servers inherit the environment.
- IN-A8/A9: OAuth loops.
- IN-A10: wire fails open.
- IN-A13: config precedence.
- IN-B1: stale lock file.

**Dead code**
- The resurrected runtime cluster.
- `utils/environment.py` and `utils/shell_quoting.py` are verbatim duplicates.
- About 190 dead symbols.
- Unused config: `BackgroundConfig`, `default_yolo`, `default_plan_mode`.

## Recommended order

These are Phases 1–9 in `PLAN.md`:

1. Stop crashes and hangs.
2. Decide on and remove the resurrected runtime.
3. Fix the permission model.
4. Add a trust boundary and protect secrets.
5. Fix agent-loop correctness.
6. Harden tools and the shell.
7. Harden workflows and orchestration, and remove blocking I/O.
8. Align prompts with the code.
9. Consolidate the duplicate systems and do the final dead-code sweep.

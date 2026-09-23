# Prompt to start Phase 1 (paste into a new agent session)

For later phases, copy this prompt and change the phase number and the "Scope" line.

---

```
You are implementing Phase 1 of the CoderAI remediation plan.

Context
- Repo: /Users/aditya/Desktop/CoderAI-main (Python 3.12+ terminal AI coding agent).
- All code-review findings and the plan are in docs/code-review-2026-09-22/:
  - PLAN.md: the phased plan, working rules, and the Phase 1 checklist. Read the "Working rules" section and the "Phase 1" section first.
  - findings/*.md: detailed evidence for each finding ID (WF = 01-workflows-orchestration.md, TL = 02-tools-platform.md, AL = 03-agent-loop-sessions-llm.md, PR = 04-prompts-agent-specs-skills.md, UI = 05-terminal-ui.md, IN = 06-infrastructure-config-security.md, DC = 07-dead-code-and-tests.md).
  - 00-SUMMARY.md: overview. PROGRESS.md: the log you append to at the end.
- Also follow AGENTS.md at the repo root.

Scope: Phase 1 only, "Stop crashes, hangs and wrong results (quick wins)". That means the 19 items in PLAN.md sections 1a and 1b: WF-A1, TL-A1, IN-A3, UI-A1, UI-A5, UI-A12, UI-A11, TL-A21, TL-A3, DC-E, TL-A4, TL-A12, TL-A16, TL-A14, WF-A10, WF-A15, UI-A15, IN-A9, IN-A11.

How to work
1. For each item, open its entry in findings/*.md, then find the code with rg. Line numbers are from 2026-09-22 and may have drifted. Re-verify that the bug still exists. If it doesn't, or the finding is wrong, mark it [~] in PLAN.md with a one-line reason and move on.
2. Write a failing regression test first, then make the smallest fix that passes it. Put tests in the most relevant existing tests/test_*.py file, or in a new tests/test_review_phase1.py.
3. Only fix the live code path: coderai.main -> coderai/ui/shell/app.py -> SessionManager (coderai/soul/session/manager.py) -> AgentLoop/SessionSoul -> ToolExecutor/ToolRegistry (coderai/tools/legacy/). Do NOT touch KimiSoul, coderai/app.py, the Shell class in coderai/ui/shell/__init__.py, CoderAIToolset, or the kosong CallableTool2 classes. Phase 2 removes those.
4. Stay minimal. No refactors outside these items. Match the surrounding code style. No PEP 695 syntax. No direct use of Python 3.13+ asyncio APIs; use coderai.utils.aioqueue. The local .venv is Python 3.14, so 3.13+ APIs won't fail locally. Be deliberate about this.
5. Run tests one file per process, never the whole suite at once:
   .venv/bin/python -m pytest tests/<file>.py -p no:cacheprovider --benchmark-disable -q
   Run every test file you touched plus every test file that imports a module you changed. Also run `ruff check coderai`, which must stay clean for F rules, and `ruff format --check` on the files you changed.
6. The working tree has many uncommitted user changes. Don't revert or discard anything unrelated, and don't commit.
7. Use a todo list to track the 19 items.

When done
- In PLAN.md, tick each Phase 1 checkbox: [x] done, [~] skipped/invalid with a reason, [!] blocked with a reason.
- Append a "Phase 1" entry to docs/code-review-2026-09-22/PROGRESS.md using its template: IDs done, skipped and blocked, tests run with results, and follow-ups.
- Reply with a short summary: what was fixed, which tests you added, test results, anything skipped or blocked and why, and anything Phase 2 needs to know.
```

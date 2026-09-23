# CoderAI code review — 2026-09-22

This folder holds a read-only review of the CoderAI working tree as of 2026-09-22, including the uncommitted changes, plus the phased remediation plan. It covers about 89k lines in 280 Python files. Seven review agents ran in parallel, alongside ruff, vulture and an import-graph orphan scan.

**Everything needed to do the fixes is in this folder.** Start with `PLAN.md`.

## Contents

| Path | What it is |
|---|---|
| `00-SUMMARY.md` | Executive summary: headline risks, cross-cutting patterns, top findings |
| `PLAN.md` | Phased implementation plan (Phases 1–9), with checkboxes, exit criteria and working rules |
| `PHASE-1-PROMPT.md` | Copy-paste prompt to start Phase 1 in a fresh agent session |
| `findings/01-workflows-orchestration.md` | Subagents, teams, background jobs, hooks, schedule, goals, triage, notifications |
| `findings/02-tools-platform.md` | `coderai/tools/`, sandbox, approval runtime, shell, file ops, web |
| `findings/03-agent-loop-sessions-llm.md` | `coderai/soul/`, session manager, compaction, `llm.py` |
| `findings/04-prompts-agent-specs-skills.md` | System prompts, agent YAML/markdown specs, skills, injections |
| `findings/05-terminal-ui.md` | `coderai/ui/`, terminal, Rich rendering, slash commands |
| `findings/06-infrastructure-config-security.md` | Config, CLI, ACP, wire, MCP, OAuth, telemetry, dependencies |
| `findings/07-dead-code-and-tests.md` | Verified dead code, orphan modules, duplicates, unused config, test hygiene |
| `static-analysis/` | Raw outputs: `vulture60.txt`, `vulture_with_tests.txt`, `orphans.txt`, `ruff_bugs.txt`, `ruff_stats.txt`, plus the dead-code audit scripts |

An interactive, filterable version of the summary is in the Cursor canvas at
`~/.cursor/projects/Users-aditya-Desktop-CoderAI-main/canvases/coderai-code-review-sep-2026.canvas.tsx`.

## Finding IDs

Every finding has an ID made of a report prefix plus its local number (for example `WF-A1`, which is section A, item 1 of the workflows report).

| Prefix | Report |
|---|---|
| `WF` | `findings/01-workflows-orchestration.md` |
| `TL` | `findings/02-tools-platform.md` |
| `AL` | `findings/03-agent-loop-sessions-llm.md` |
| `PR` | `findings/04-prompts-agent-specs-skills.md` |
| `UI` | `findings/05-terminal-ui.md` |
| `IN` | `findings/06-infrastructure-config-security.md` |
| `DC` | `findings/07-dead-code-and-tests.md` (lettered sections, for example `DC-E`) |

Some issues were found independently by more than one agent. `PLAN.md` lists the aliases, for example `WF-A5` = `PR-A1`/`PR-A2`.

## Caveats

- Line numbers are from the 2026-09-22 working tree and will drift. **Re-verify each finding against the current code before fixing it.**
- The reports were produced by review agents. The top items were spot-checked by hand (marked "verified" in `00-SUMMARY.md`), but any single finding can still be wrong. Treat each one as a strong lead, not ground truth.

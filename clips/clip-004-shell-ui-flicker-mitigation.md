---
Status: Implemented
Date: 2026-09-11
---

# CLIP-004: Shell UI Flicker Mitigation

Rich Live rendering pipeline, terminal buffer management, and screen redraw
optimization for the interactive shell.

## Summary

The terminal has two regions: the **viewport** (visible, updatable in place)
and the **scrollback** (immutable history). When a Rich `Live` display grows
taller than the viewport, its top is pushed into scrollback — and any update
then requires clearing and repainting everything, which reads as flicker.
CoderAI mitigates this with a shared line budget, pager off-ramps, and
surgical repaints.

## Problem background

Observed failure modes:

1. **Oversized approval requests**: full shell commands embedded in the
   approval `description` made panels arbitrarily tall.
2. **Unrendered display payloads**: `ApprovalRequest.display` blocks
   (`DiffDisplayBlock`, `ShellDisplayBlock`) had no renderer, so reviewers
   approved blind.
3. **Truncated context**: long diffs/tools output scrolled out of reach with
   no way to inspect the full content.

## Design

### 1. Unified line budget

All live regions share a fixed row budget. Content renders in priority order
until the budget is exhausted; overflow is summarized (`+N lines`) instead
of pushed into scrollback. The budget accounts for the approval panel,
streaming tool cards, and the input line so the common case never exceeds
the viewport.

### 2. Pager off-ramp

Full content is always one keystroke away via Rich `console.pager()` (the
same mechanism as `/help` and `/diff` full views). The pager uses the
alternate screen, fully isolated from the `Live` display: zero flicker,
plus search/scroll for free. Exit restores the exact prior frame.

### 3. Display-block rendering

`ApprovalRequest.display` now renders by block type:

- `DiffDisplayBlock` → unified diff with path header, hunk-aware truncation.
- `ShellDisplayBlock` → command + cwd + preview rows, capped to budget.

Approvers see *what* runs and *what* changes before confirming — the two
fields that previously caused blind approvals.

### 4. Surgical repaints

- The interactive layer (`coderai/ui/shell/_interactive.py`,
  `coderai/visualize/_live_view.py`) diffs frames and rewrites only changed
  rows; static regions (prompt, status bar) are excluded from the `Live`
  region entirely.
- Streaming markdown (`markdown_stream.py`) appends within its card instead
  of re-rendering the whole viewport.
- `broadcast.py` fans status updates out-of-band so progress ticks don't
  invalidate the content frame.

## Verification

Phase 4 UI suite (`tests/test_phase4_ui.py`, 12/12) plus the UI review
remediation corpus (`tests/test_ui_review_remediation.py`) pin the renderer
contracts: budget accounting, pager entry points, and display-block output.
Manual check: run a long tool-heavy turn in a short terminal (`rows=24`)
— no full-screen flash should occur.

## Non-goals

- No alternate-screen REPL; the shell stays inline so scrollback remains
  useful.
- No GPU/kitty-graphics fast paths; portable ANSI only.

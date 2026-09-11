---
name: feature-smoke-test
description: End-to-end smoke testing for new or changed CoderAI features. Use when validating a feature with non-interactive CLI runs, checking process exit codes, inspecting session artifacts, or diagnosing regressions after code changes.
---

# Feature Smoke Test

## Determine Scope

From code changes, identify the functional boundary:

```sh
git diff main --name-only
git diff main --stat
```

Write down:
- What feature boundary is being tested
- User-visible behavior changes
- Pre-run setup and post-run cleanup needed
- Evidence that proves success or failure

## Read Fact Sources

Read the minimal set of files defining the feature's real behavior:
- User-facing docs, changelog, or design notes
- Agent/tool prompts exposing the feature
- Implementation entry points
- Existing tests (prefer tests over docs when they better describe behavior)

Do not trust stale prompt examples. Reconstruct the real interface from code.

## Plan Minimal Test Matrix

Default coverage — three scenarios:

1. Happy path
2. Edge cases, invalid input, or capacity limits
3. Interruption, retry, cleanup, or recovery

For each scenario record: goal, precondition, prompt strategy, success signal, failure signal, artifacts to check.

## Execute in Isolation

Use a temp directory for `--work-dir` to isolate sessions:

```sh
SMOKE_DIR="$(mktemp -d /tmp/coderai-smoke-XXXXXX)"
```

### Default execution

```sh
/usr/local/bin/python3 -m coderai --print \
  --prompt "your test prompt" \
  --work-dir "$SMOKE_DIR"
echo "exit_code=$?"
```

Check exit code first — non-zero means the CLI crashed or timed out.

### Alternatives

- **Long prompt**: pipe via stdin with `cat <<'PROMPT' | coderai --print --input-format text --work-dir "$SMOKE_DIR"`
- **Structured output**: add `--output-format stream-json`
- **Final result only**: use `--quiet`

## Inspect Session Artifacts

Check session files after each run:
- Session context and wire logs
- Files created by the feature

Use `inspect_session` scripts or directly examine the session directory.

## Report Results

Categorize findings:

- **Confirmed** behavior
- **Unexpected** behavior (with prompt, session path, artifact proof, minimal repro steps)
- **Ambiguous** behavior (needs further investigation)

## Problem Investigation

When unexpected behavior is found, launch parallel root-cause exploration:
- Trace the call chain from the triggering entry point
- Check data transformations across processing stages
- Verify persistent state consistency
- Run related unit/integration tests

For each exploration direction, report: scope, findings (with file:line), conclusion.
Summarize with: root cause, evidence chain, minimal fix, regression risk.

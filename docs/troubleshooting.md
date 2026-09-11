# Troubleshooting

## `/doctor`

First stop for anything environmental — auth, paths, permissions, sandbox:

```text
/doctor
```

`run_doctor_diagnostics()` (`coderai/cli/doctor.py`) probes the project
root and `~/.coderai` (write probes: `.doctor_probe`), provider credentials,
and tool sandbox health, then `render_doctor()` prints a pass/fail table
with remediation hints. Run it before filing a bug; paste its output into
the report.

## Permission resets

Symptoms: every tool prompts despite `/yolo`; edits fail with approval
errors; Plan Mode won't exit.

```text
/yolo     # toggle auto-approve back on
/afk       # toggle auto-pilot off if it swallowed a question
/plan      # toggle Plan Mode off to re-enable mutations
```

Notes:

- Plan Mode forces mutating scopes to prompt **even with `--yolo`** — by
  design. Exit Plan Mode to restore auto-approve.
- `--print` implies AFK for that invocation only; it doesn't persist.
- Session approval state also persists per session — a resumed session keeps
  the mode it was saved with. Check with `/sessions` + resume if a fresh
  `coderai` behaves but the resumed one doesn't.

## Sandboxing issues

The tool platform sandboxes file/process execution per session
(`sandbox_mode`, default `workspace-write`). When edits or commands fail:

1. Confirm the working directory: `coderai --work-dir <dir>` / CWD at launch
   defines the writable root. `--add-dir <dir>` extends scope (repeatable).
2. Check the target isn't outside every scoped root — reads outside scope
   are blocked, writes doubly so.
3. Skill scan roots (`.coderai/skills`, etc.) are read-exempt by design;
   anything else read-blocked is a scope problem, not a bug.
4. Run `/doctor` — it write-probes both the project `.coderai/` and home
   `~/.coderai/` and reports which side is read-only.

## Proxy settings

No magic proxy detection: the process honors standard `HTTP_PROXY` /
`HTTPS_PROXY` / `NO_PROXY` from the environment plus `CODERAI_BASE_URL`
endpoint overrides. Debug path:

```bash
# confirm the endpoint is reachable at all
curl -sS -o /dev/null -w "%{http_code}\n" $CODERAI_BASE_URL/models

# bypass proxy for local providers
NO_PROXY=localhost,127.0.0.1 coderai --setup --test
```

`coderai --setup --test` exercises auth against the active provider and
reports connection vs credential failures separately — trust its verdict
over the model's apology.

## Session recovery

Sessions persist under the share dir (`~/.coderai/sessions/<workdir-hash>/`):

```bash
coderai --resume            # picker over saved sessions
coderai --last              # resume most recent for this directory
coderai --continue          # continue previous session for the workdir
coderai --resume <prefix>   # id, id-prefix, or checkpoint hash all resolve
coderai --fork <id>         # branch without touching the original
```

In-session:

```text
/sessions [query]   # browse, search, resume, delete, fork
/undo               # roll back last turn's file changes (GitFileHistory checkpoint)
/diff               # uncommitted working-tree changes
/compact            # compress history when context runs hot
```

File edits are checkpointed per turn via isolated git history — `/undo`
restores files, not conversation. To recover *conversation* after a crash,
`--resume`/`--last` replays the persisted event stream; compaction markers
(`CompactionBegin`/`CompactionEnd`) survive the round trip.

## Still stuck?

1. `coderai --setup --status` — credentials sane?
2. `/doctor` — environment sane?
3. `coderai --setup --test` — network + auth sane?
4. Reproduce with `--print` (deterministic, no REPL rendering involved):
   `coderai --print -p "..."`.
5. File an issue with: `/doctor` output, `--setup --status` (redact keys),
   and the minimal `--print` reproducer.

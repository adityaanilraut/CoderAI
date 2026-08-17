# CoderAI — Remaining Work (handoff)

Redesign toward DeepCode is **complete and verified**. See `docs/REDESIGN.md` for
the final architecture. This file is the remaining plan.

## Verified (green)
- `python -m ruff check coderai/ tests/ scripts/` — clean
- `python -m mypy coderai/` — clean
- `python -m pytest -q` — 8 passed
- `python scripts/self_check_core.py` — all checks
- `python -m coderai --version` / `--help` / interactive REPL — runs

## P0 — Git reconciliation (uncommitted, case-sensitive mess)
The repo tracks the OLD `coderAI/` (capital) package; the NEW `coderai/`
(lowercase) package is untracked. macOS (case-insensitive FS) treats them as one
dir, so `git status` shows old files as `D` and new as `??`.

To reconcile (only when the user asks to commit):
```
git rm -r --cached coderAI
git add coderai/ tests/ scripts/ docs/ .env.example Makefile pyproject.toml
# verify `git status` shows a clean new tree, then commit
```

## P1 — Docs / polish
- `README.md` still describes the old CLI (`coderai chat`, setup wizard). Rewrite.
- `requirements.lock` is stale (references removed deps). Regenerate:
  `uv pip compile pyproject.toml --universal --generate-hashes -o requirements.lock`

## P2 — Feature parity (deliberately skipped, port from DeepCode if worth it)
- Bash `run_in_background` — schema exists in `core/prompt.py`, handler ignores it.
- `GitFileHistory` checkpoint undo — `core/common/file_history.py` was deleted.
- LLM-assisted edit self-correction (escape/quote repair) — deterministic path done
  (exact match, tab-strip, `replace_all` guard, `candidates`); LLM fallback skipped.
- Streaming token display — turns render whole today (not a correctness issue).
- Skills auto-matching via LLM — only explicit `/skills` loading today.

## P3 — Verification checklist (run after every change)
```
python -m ruff check coderai/ tests/ scripts/
python -m mypy coderai/
python -m pytest -q
python scripts/self_check_core.py
python -m coderai --version
```

## Do NOT reintroduce (deleted aider-era layer)
`coders/`, `models.py`, `repo.py`, `history.py`, `llm.py`, `args.py`,
`commands.py`, `prompts.py`, `utils.py`, `exceptions.py`, `io.py`.

## Reference paths
- CoderAI: `/Users/adityaraut/Desktop/CoderAI-main`
- DeepCode gold standard: `/Users/adityaraut/Downloads/deepcode-cli-main`

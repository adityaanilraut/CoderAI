# CoderAI Architecture — Final State

> Reference: `/Users/adityaraut/Downloads/deepcode-cli-main` (`packages/core` engine, `packages/cli` UI).

CoderAI now mirrors DeepCode's UI-agnostic-core architecture. The old aider-style
layer (`coders/`, `models.py`, `repo.py`, `history.py`, `llm.py`, `args.py`,
`commands.py`, `prompts.py`, `utils.py`, `exceptions.py`, `io.py`) is deleted.

## Final layout

```
coderai/
  main.py             # console entry -> cli.app.main
  cli/app.py          # argparse + REPL + permission prompt + markdown (UI only)
  core/               # UI-agnostic engine (no rich/prompt_toolkit imports)
    session.py        # SessionManager: stream -> tool_calls -> permissions -> execute -> loop
    prompt.py         # get_system_prompt / get_tools / get_runtime_context / skills
    permissions.py    # compute_tool_call_permissions (side-effect scopes)
    settings.py       # resolve_current_settings (user + project + env)
    state.py          # snippet store + fileVersion + rebuild-from-history
    openai_client.py  # create_openai_client (cached)
    tools/            # executor + read/write/edit/bash + AskUserQuestion/UpdatePlan/WebSearch/UnderstandImage
    mcp/              # stdio MCP client + manager (dynamic tool defs)
    common/           # file_utils, state helpers, shell/process/llm-error/logging
  skills/             # bundled skills (markdown, lazily loaded) — optional
```

## Key DeepCode patterns implemented

| Pattern | Where |
|---|---|
| Bounded agent loop (stream → tools → permissions → execute → loop) | `core/session.py:_activate` (MAX_ITERATIONS) |
| Snippet-based editing (`read` → `snippet_id`, `edit` scoped to it, stale-file rejection) | `core/state.py` + `core/tools/{read,edit,write}.py` |
| Cache-aware context ordering (stable system/tools/runtime before volatile history) | `core/session.py:create_session` + `core/prompt.py` |
| Side-effect-scoped permissions (`computeToolCallPermissions`, allow/deny/ask, `always allow` persistence) | `core/permissions.py` |
| Skills as lazily-loaded context (not plugins) | `core/prompt.py:list_skills/load_skill/build_skill_documents_prompt` |
| JSONL session persistence + sessions index + token-threshold compaction | `core/session.py` (storage under `~/.coderai/projects/<code>/`) |
| Small built-in tool surface + dynamic MCP | `core/tools/` + `core/mcp/` |
| UI-agnostic core with a thin CLI | `core/` vs `cli/` |

## Deleted (aider-era bloat)

`coders/` (all edit-format coders), `models.py` (litellm Model), `llm.py` (litellm
retry wrapper), `repo.py` (GitRepo commit attribution), `history.py` (ChatSummary),
`commands.py` (slash commands), `prompts.py`, `args.py` (argparse), `utils.py`,
`exceptions.py`, `io.py`, and the stub `cli/cli_args.py`, `cli/exec_runner.py`,
`cli/exec_input.py`, `cli/common/`. Unused `core/common/` ports (`file_history`,
`telemetry`, `notify`, `validate`) removed too.

Dependencies pruned to `rich`, `openai`, `requests` (from 14).

## Verification

```bash
python -m ruff check coderai/ tests/ scripts/   # clean
python -m mypy coderai/                          # clean
python -m pytest -q                              # 74 passed
python scripts/self_check_core.py                # all checks
python -m coderai --version                      # runs
```

## Remaining (deliberately out of scope)

- `requirements.lock` needs regeneration against the pruned dependency set.
- Bash background execution + live timeout control (`run_in_background`).
- GitFileHistory checkpoint-based undo.
- LLM-assisted edit self-correction (escape/quote repair) — the deterministic
  path (exact match, tab-strip, `replace_all` guard, `candidates` metadata) is
  implemented; the LLM fallback is skipped.
- Streaming token progress UI (turns are rendered whole; streaming is a display
  optimization, not a correctness issue).

# CoderAI Session Persistence

## Project-local storage

CoderAI stores session data in:

```text
<project>/.coderai/sessions/
```

Main contents:

| Path | Purpose |
| --- | --- |
| `sessions-index.json` | Up to 50 recent session summaries |
| `<session-id>.jsonl` | Append-oriented message and event log |
| `file-history/.git/` | Internal checkpoints used by `/undo` and `/diff` |
| `images/<session-id>/` | Session image copies when present |

The internal Git repository is separate from the project's repository. Do not edit it manually.

## Legacy migration and fallback

Older versions used:

```text
~/.coderai/projects/<project-name>-<path-hash>/
```

On first access, if the local `sessions-index.json` does not exist and the legacy index does, CoderAI copies the legacy index, JSONL logs, `file-history`, and `images` into the project-local directory. Existing local session data is not overwritten, and the legacy source is left in place.

If the project-local directory cannot be created or used, CoderAI falls back to the legacy global directory.

## Logs and compaction

JSONL files can contain current typed events and legacy message rows. Malformed rows are skipped while loading. Long conversations may mark older messages as compacted and add a summary event; the original rows remain in the log.

## Commands

- `/sessions` browses, resumes, deletes, or forks saved sessions.
- `/resume <id>` resumes a saved session.
- `/fork` creates an independent branch of a session and its checkpoint history.
- `/delete` and `/rename` update saved session metadata.
- `/export` writes session history to Markdown or JSON.
- `/continue` continues the current agent execution; it is not a session-storage migration command.

## Undo

User turns carry checkpoint metadata when a restorable snapshot exists. `/undo` can restore:

- conversation history only,
- tracked workspace files only, or
- both.

Conversation restore rewrites the selected session JSONL. Code restore modifies files tracked by the internal checkpoint. Use `/diff` to inspect the current checkpoint diff before restoring when needed.

Deleting a session removes its index entry, JSONL log, and session image directory. It does not guarantee immediate removal of every object from the internal Git repository.

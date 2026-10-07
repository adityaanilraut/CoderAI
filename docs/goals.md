# Session goals

Goals save an objective, acceptance criteria, milestone progress, notes, and a bounded number of completed attempts. They belong to a real session and persist under the project's `.coderai/goals/` directory.

## Terminal workflow

```text
/goal add Fix login and verify the regression
/goal list
/goal start a1b2c3d4
/goal pause a1b2c3d4
/goal done a1b2c3d4
/goal cancel a1b2c3d4
```

Use the exact ID printed by `add` or `list`; `a1b2c3d4` above is an example. Adding saves a **pending** goal and creates a session if needed. Starting runs the goal in the foreground, so the shell retains cancellation, approval, question, and queue controls. `pause`, `done`, and `cancel` affect only the specified goal. `/stop` also pauses an executing or suspended goal. Starting an exhausted goal requires increasing its round budget first through the `goal` tool.

The agent continues until the objective is verified and marked completed, its budget is exhausted, or execution stops for user input, approval, interruption, an error, or a hook. A round is a completed goal attempt, which may contain multiple model calls and tool steps. Ordinary conversation turns do not consume goal rounds. Errors, refusals, cancellations, and hook stops pause execution without consuming a completed round. Permission and question waits keep the current round open; approving or answering resumes it, including after a runtime restart. Denying approval pauses the goal.

The default budget is 20 completed rounds. Export `CODERAI_GOAL_MAX_ROUNDS` to change the default, or supply `max_rounds` when creating or updating a goal. Each foreground run retains its initial remaining-round limit, even if the model later changes the saved budget. Exhaustion produces a visible failed-goal notice. Goal execution respects plan mode and the existing tool permission policies.

## Agent tool

The registered `goal` tool accepts these actions:

| Action | Behavior |
| --- | --- |
| `create` | Requires `objective`; optionally accepts `description`, `milestones`, `notes`, `max_rounds`, and `status`. Default status is running; the runtime continues after the current turn. Use pending to save for later. |
| `status` | Lists goals and milestone progress; available in plan mode. |
| `update` | Changes objective, description, milestones, completed milestone count, notes, budget, or status. |
| `start` | Starts or resumes the selected goal after the current turn. |
| `pause` | Pauses the selected goal. |
| `complete` | Marks the verified objective completed, preserving notes unless explicitly supplied. |
| `cancel` | Cancels the selected goal. |

For management actions, use `goal_id`; omission selects the currently running goal. IDs are session scoped. `completed_milestones` records how many leading milestone checkpoints are complete and cannot exceed the milestone count. `expected_revision` optionally rejects stale updates. Invalid inputs return explicit errors without changing goal data.

The model receives updated goal state when revisions change and after context compaction. It should use notes to retain verification evidence and complete a goal only after checking its acceptance criteria. It cannot replace a running goal during autonomous execution to reset the budget; it can save a new pending goal.

## Persistence and session lifecycle

Each write reloads the file while holding a cross-process OS lock, validates the changes, and atomically replaces JSON. Returned goals are immutable snapshots. Separate CLI/daemon processes cannot overwrite newer records from stale caches, and only one process can drive goals for a session at a time. OS locks release when their owner exits; lock files are retained to preserve lock identity.

Version 1 records migrate on the next successful write: legacy `done` becomes completed and the old next-round counter becomes a completed-attempt count. Invalid records are reported; valid records remain visible, but changes are blocked until the original file is repaired. Unsupported versions and malformed JSON are preserved rather than overwritten.

Forks copy goal state, pausing running goals until explicitly started in the new session. Forks at a historical point use the latest retained goal snapshot. Conversation undo restores the snapshot at the retained user checkpoint. Deletion removes goal records and leaves a deletion marker so a late worker cannot recreate them. Goal state events and notices stay outside pending model tool-call sequences.

## Verification

`tests/test_goals.py` exercises the public tool through its executor, the real agent loop with simulated model responses, CLI session binding, concurrent processes, write failures, corruption, cancellation, permissions, hooks, context injection, fork, undo, and deletion. Run it independently:

```sh
.venv/bin/python -m pytest tests/test_goals.py -p no:cacheprovider --benchmark-disable -q
```

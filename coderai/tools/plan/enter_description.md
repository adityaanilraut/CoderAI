Use this tool proactively when you're about to start a non-trivial implementation task.
Getting user sign-off on your approach before writing code prevents wasted effort.

Use it when ANY of these conditions apply:

1. New Feature Implementation — e.g. "Add a caching layer to the API"
2. Multiple Valid Approaches — e.g. "Optimize database queries" (indexing vs rewrite vs caching)
3. Code Modifications — e.g. "Refactor auth module to support OAuth"
4. Architectural Decisions — e.g. "Add WebSocket support"
5. Multi-File Changes — involves more than 2-3 files
6. Unclear Requirements — need exploration to understand scope
7. User Preferences Matter — if user input would materially change the implementation approach, use enter_plan_mode to structure the decision

Auto-approve mode notes:
- Yolo mode only bypasses permission approval. It does not make the session non-interactive.
- In yolo mode, enter_plan_mode is approved automatically, but exit_plan_mode still presents
  the plan to the user for approval.
- Afk mode bypasses permission approval and is non-interactive. In afk mode, do not use
  AskUserQuestion; make the best decision from available context.
- In afk mode, enter_plan_mode / exit_plan_mode are approved automatically because no user
  is present.
- Use enter_plan_mode only when planning itself adds value.

When NOT to use:
- Single-line or few-line fixes (typos, obvious bugs, small tweaks)
- User gave very specific, detailed instructions
- Pure research/exploration tasks

## What Happens in Plan Mode
In plan mode, you will:
1. Identify 2-3 key questions about the codebase that are critical to your plan. If you are not confident about the codebase structure or relevant code paths, use `Task(subagent_type="explore")` to investigate these questions first — this is strongly recommended for non-trivial tasks.
2. Explore the codebase using glob, grep, read (read-only) for any remaining quick lookups
3. Design an implementation approach based on your findings
4. Write your plan to a plan file
5. Present your plan to the user via exit_plan_mode for approval

---
Status: Implemented
Date: 2026-09-11
---

# CLIP-003: Agent Flow — Autonomous Swarms & Dynamic Roles

Architecture of CoderAI's autonomous agent swarms: dynamic role discovery,
DAG-validated task boards, and background worker mailboxes.

## Summary

Two cooperating systems: **roles** (what an agent *is*) and **teams** (how
agents *coordinate*). Roles resolve from Markdown specs at session start;
teams coordinate through a `TeamManager` + `TeamTaskBoard` + per-agent
`ActorChannel` mailboxes, with `wait_agent` barriers for synchronization.

## Motivation

- Single-turn tool batching saturates on multi-file, multi-step work;
  decomposing into teammates with owned task subgraphs scales further.
- Hardcoded personas rot; Markdown specs in the repo let teams evolve roles
  without code changes.
- Background teammates must make progress without blocking the foreground
  turn — hence actor mailboxes, not shared locks.

## Role discovery

`discover_custom_agents()` (`coderai/subagents/registry.py`) scans, first
match wins by name:

```text
<project>/.coderai/agents/*.md
<project>/.agents/agents/*.md
~/.coderai/agents/*.md
~/.agents/agents/*.md
```

over the built-in YAML specs (`coderai/agents/*/*.yaml` via
`coderai/agentspec.py`). Frontmatter (`name`, `description`, `tools`,
`mode`) becomes a `SubagentTypeDefinition`; the body becomes the system
prompt. REPL surface: `/agents roles`, `/agent <name>` hot-switch,
`coderai --agent <name>` at launch.

## Swarm coordination (`coderai/teams/`)

| Component | File | Responsibility |
|---|---|---|
| `TeamManager` | `manager.py` | Spawn (`spawn_teammate`), lifecycle, teardown |
| `TeamTaskBoard` | `models.py` | Task DAG; rejects cyclic dependencies at insert |
| `ActorChannel` / `AsyncMailbox` | `mailbox.py` | Priority per-agent message queues |
| Barriers | `concurrency.py` | `wait_agent` rendezvous points |
| Deadlock guard | `deadlock.py` | Cycle detection across wait edges |
| Team tools | `tools.py` | `team_task_*` verbs exposed to the engine |

Execution model:

1. Foreground turn (or a teammate) decomposes work into board tasks with
   `depends_on` edges. The board validates acyclicity on write.
2. `TeamManager.spawn_teammate(role)` boots a subagent with the role's
   system prompt and a dedicated `AsyncMailbox`.
3. Teammates pull ready tasks (all deps complete), stream `SubagentEvent`
   progress onto the wire, and post results back to the board.
4. `wait_agent` blocks the waiter on a barrier until the target reaches a
   terminal state; `deadlock.py` aborts wait cycles instead of hanging.
5. Session checkpoints capture board state, so `--resume` rehydrates the
   swarm where it stopped.

## Constraints

- Mailboxes are the *only* cross-agent channel; no shared mutable state.
- A teammate's tool allow-list is the intersection of its role `tools` and
  the session approval mode (plan-mode read-only propagates to the swarm).
- Background progress never blocks the foreground turn; barriers are
  opt-in per task edge.

## Non-goals

- No cross-machine swarms; all actors live in one process.
- No preemptive scheduling — cooperation points are task boundaries and
  `wait_agent` barriers.

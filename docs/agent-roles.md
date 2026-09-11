# Agent Roles

CoderAI ships two bundled agent specifications and discovers Markdown-defined
specialist roles from your workspace and home directory.

---

## Bundled roles

| Role | Mode | Description |
|---|---|---|
| `default` | Primary | Full software engineering tool suite. |
| `okabe` | Extended | Experimental mad-scientist persona with advanced toolsets. |

Launch with a bundled role:

```bash
coderai --agent default
coderai --agent-file path/to/custom-spec.yaml   # one-off custom spec file
```

---

## Discovered roles

Markdown specs are discovered (first match wins by role name) from:

- `<project>/.coderai/agents/*.md`
- `<project>/.agents/agents/*.md`
- `~/.coderai/agents/*.md`
- `~/.agents/agents/*.md`

This repo ships: `architect`, `build-error-resolver`, `code-reviewer`,
`planner`, `security-reviewer`, `tdd-guide`.

| Role | Mode | Description |
|---|---|---|
| `architect` | General | Systems architect for components, interfaces, boundary layers. |
| `build-error-resolver` | General | Root-causes compiler, build, and typecheck errors. |
| `code-reviewer` | Read-Only | Security, correctness, and architecture review. |
| `planner` | Read-Only | Actionable implementation plans with phased milestones. |
| `security-reviewer` | Read-Only | OWASP / authorization / sanitization audit. |
| `tdd-guide` | General | Failing-first reproduction tests, then minimal fixes. |

Inspect them in the REPL:

```text
/agents roles    # all bundled + discovered roles
/agents tree     # delegation hierarchy of running subagents
/agents report <id>
```

---

## Frontmatter spec

Each role is a Markdown file with YAML frontmatter:

```markdown
---
name: architect
description: Architecture specialist for large design changes, refactors, and system boundaries.
tools: ["Read", "Grep", "Glob"]
---

You are a software architect focused on clear, maintainable designs.
...
```

Fields:

- `name` — role id used with `--agent <name>` and `/agent`.
- `description` — one-line summary shown in `/agents roles`.
- `tools` — tool allow-list for the role (e.g. read-only roles omit
  `Edit`/`Bash`). Omit to inherit the full suite.
- Optional: `mode` (`general` vs read-only family), `supports_background`.

The body after the frontmatter is the system prompt. Keep it task-focused:
mission, workflow steps, output expectations. See
`.coderai/agents/architect.md` for the house style.

---

## Hot-switching via `/agent`

Inside a session, switch roles without restarting:

```text
/agent architect          # continue this session as the architect
/agent code-reviewer      # read-only review of the working tree
```

Switching changes the active system prompt and tool allow-list; conversation
history is preserved so the new role sees full context. Use `/agents roles`
any time to confirm which roles are available in the current project.

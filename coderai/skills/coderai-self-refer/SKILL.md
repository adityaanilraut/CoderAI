---
name: coderai-self-refer
description: Answer questions about the CoderAI CLI, including settings, commands, skills, MCP, permissions, notifications, and session storage. Use when a user asks how CoderAI works or how to configure or troubleshoot it.
---

# CoderAI Self-Refer

Use the focused English references in `references/`. For commands and flags, prefer live help because it reflects the installed version.

## Topic map

| Topic | Reference |
| --- | --- |
| Settings, precedence, environment variables, skills | `references/configuration.md` |
| MCP server configuration and tool names | `references/mcp.md` |
| Permission scopes and presets | `references/permission.md` |
| Completion notification scripts | `references/notify.md` |
| Session files, migration, resume, and undo | `references/session-persistence.md` |

## Workflow

1. Read every reference relevant to the question.
2. For CLI flags, run `coderai --help`. For slash commands, use `/help` or `/help <command>`.
3. Answer with canonical names and distinguish user scope (`~/.coderai/`) from project scope (`<project>/.coderai/`).
4. Provide a minimal copy-ready example when configuration is requested.
5. Do not invent a field, command, provider, default, or compatibility claim. If the references and live help do not establish it, say that it is not documented in this version.

## Skills

`/skills` is the authoritative view of active skills. Discovery order is:

1. `<project>/.coderai/skills/`
2. `<project>/.agents/skills/`
3. `~/.coderai/skills/`
4. `~/.agents/skills/`
5. Bundled skills shipped in the `coderai` package

Additional roots may be configured with `skillScanPaths`. Skills are de-duplicated by frontmatter `name` in discovery order, and `enabledSkills` can disable a resolved name.

Do not confuse skills with slash commands or always-injected prompt text.

## Configuration changes

Before suggesting an edit:

- Ask whether the setting should apply globally or only to the current project if the scope is unclear.
- Preserve unrelated JSON fields.
- Keep secrets out of chat and committed project files. Prefer process environment variables or a protected user-level file.
- After MCP changes, verify with `/mcp`.
- After permission changes, verify with `/permission`.

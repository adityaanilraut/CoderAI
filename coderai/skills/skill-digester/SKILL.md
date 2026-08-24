---
name: skill-digester
description: Reviews and improves another CoderAI skill's SKILL.md description field, and guides Agent Skill installation into user or project .agents/skills roots. Use when the user asks to digest a skill, install an Agent Skill, install a skill to user/project scope, or says "消化技能" or "安装 agent skill".
---

# Skill Digester

Use this skill to:

- Review and optionally rewrite the `description` field of another CoderAI skill.
- Install a complete Agent Skill into an interoperable `.agents/skills` root.

Use the available user-question tool when a required choice is missing. Never edit a description or overwrite a destination without explicit approval.

## Find a skill

Run the bundled Python helper from this skill directory:

```bash
python3 scripts/find_skill.py "<skill-name-or-path>" "<project-root>"
```

The helper searches, in order:

1. `<project>/.coderai/skills`
2. `<project>/.agents/skills`
3. `~/.coderai/skills`
4. `~/.agents/skills`

It resolves frontmatter `name`, reports active and shadowed matches, and returns `digestTarget.path` in the equivalent native `.coderai/skills` scope. Ask before selecting a shadowed match.

## Digest a description

1. Confirm the target and preferred language if either is unclear.
2. Read the complete source `SKILL.md`.
3. Validate that the description is non-empty, at most 1024 characters, describes what the skill does, and says when it should activate.
4. Compare it with the body. Flag inaccurate, vague, overly broad, or missing trigger language. Do not rewrite only for stylistic preference.
5. Show the current description, concise findings, proposed replacement, source path, and `digestTarget.path`.
6. Ask whether to apply, abandon, or refine the proposal.

After approval, write only to `digestTarget.path`:

- If it is the source, change only `description`.
- If the target does not exist, copy the complete source directory there, then change only the target description.
- If a distinct target exists, change only its description; do not replace its body or resources.

Preserve every other frontmatter field and the Markdown body.

## Install a skill

1. Resolve the source directory from an explicit path or the helper. It must contain `SKILL.md`.
2. Resolve the folder name from frontmatter `name`, falling back to the source folder with underscores converted to hyphens.
3. Ask exactly one scope question:
   - user: `~/.agents/skills/<name>/`
   - project: `<project>/.agents/skills/<name>/`
4. If the destination exists, stop and report the conflict.
5. Otherwise copy the whole directory, including scripts, references, templates, and assets.
6. Report source and destination. Mention that the client may need to reload.

Installation writes only to `.agents/skills`; digestion writes only to `.coderai/skills`. Do not combine installation with a description rewrite unless the user separately requests both.

## Description pattern

```text
<What the skill does>. Use when <task types, file types, tools, domains, or user phrases that should trigger it>.
```

Prefer concrete operations and trigger terms. Avoid marketing copy and implementation details.

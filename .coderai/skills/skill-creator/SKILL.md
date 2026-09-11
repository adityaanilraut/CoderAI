---
name: skill-creator
description: Guide for creating new CoderAI skills. Use when the user wants to create, update, or package a skill that extends CoderAI with specialized workflows, tool integrations, or domain knowledge.
---

# Skill Creator

## Skill Structure

Every skill lives in `.coderai/skills/<skill-name>/` and requires:

```
skill-name/
├── SKILL.md          (required)
├── scripts/          (optional - executable helpers)
├── references/       (optional - loaded into context as needed)
└── assets/           (optional - templates, images, output files)
```

## SKILL.md Format

```yaml
---
name: skill-name
description: What this skill does and when to use it.
---
```

Follow with Markdown instructions. Keep body under 200 lines. Use imperative form.

## Design Principles

1. **Concise** — Only add context CoderAI does not already have. Challenge every paragraph: "Does this justify its token cost?"
2. **Progressive disclosure** — Metadata always loaded (~50 words), body loaded on trigger (<500 lines), bundled resources loaded as needed.
3. **Match freedom to fragility** — Narrow bridge (fragile ops) = specific scripts. Open field (many valid approaches) = text guidance.

## Creation Steps

1. **Understand** — Gather concrete usage examples. Ask: what triggers this skill? What does success look like?
2. **Plan resources** — For each example, ask: what scripts, references, or assets would help?
3. **Initialize** — Create the skill directory with SKILL.md and needed subdirs.
4. **Edit** — Write SKILL.md body and implement bundled resources. Test scripts by running them.
5. **Package** — Validate frontmatter, naming, and structure. No extraneous files (no README.md, CHANGELOG, etc.).

## Naming Rules

- Lowercase letters, digits, and hyphens only.
- Under 64 characters. Prefer short, verb-led phrases (e.g., `rotate-pdf`, `address-comments`).
- Folder name must match the `name` field in frontmatter.

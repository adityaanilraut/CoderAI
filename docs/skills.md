# Skills

Skills are progressive-disclosure instruction packs: a `SKILL.md` with YAML
frontmatter plus optional `scripts/`, `references/`, and resources. CoderAI
loads skill bodies on demand instead of stuffing every workflow into the
system prompt.

---

## Folder structure

```text
<skills-root>/<skill-name>/
  SKILL.md            # required: frontmatter + instructions
  scripts/            # optional helper scripts (python/shell)
  references/         # optional deep-dive docs loaded on request
```

Example (`coderai/skills/skill-writer/SKILL.md`):

```markdown
---
name: skill-writer
description: Guide users through creating, updating, debugging, and validating Agent Skills ...
---

# Skill Writer
...
```

## Discovery & precedence

`get_skill_scan_roots()` (`coderai/skill/__init__.py`) scans, first match
wins by skill name:

1. `./.coderai/skills` (project)
2. `./.agents/skills` (project)
3. `./.claude/skills` (project compat)
4. `~/.coderai/skills` (user-global)
5. `~/.agents/skills` (user-global)
6. `~/.claude/skills` (user-global compat)
7. `--skills-dir <dir>` custom paths (repeatable)
8. `bundled:` — skills shipped in `coderai/skills/`

Bundled skills in this repo: `coderai-self-refer`, `image-generator`,
`skill-digester`, `skill-writer`.

```bash
coderai --skills-dir ~/my-skills -p "..."   # add a custom root this run
```

## YAML frontmatter

```yaml
---
name: my-skill
description: When to use this skill — the trigger the agent matches on.
---
```

- `name` — directory name and invocation id (`/skill <name>`).
- `description` — the routing signal. Write it as a *when-to-use* sentence,
  not a feature blurb: `"Use when the user wants to ..."` beats
  `"Helpers for ..."`.
- Extra keys are preserved and exposed to the loader; keep them scalar.

## Execution permissions

Skills inherit the session's approval policy — they grant *knowledge*, not
*privilege*. A skill's scripts run through the same sandbox as any tool
call: default confirm, `--yolo` auto-approve, `--plan` read-only gating.
Skill scan roots are read-exempt (`get_skill_read_exempt_paths()`) so
loading instructions never triggers an approval loop.

## Scripts

`scripts/` holds executables the skill instructs the agent to run, e.g.
`coderai/skills/image-generator/scripts/image_generator.py`
(`--prompt`, `--output`, ...). Keep scripts dependency-light and
argparse-driven so they work from any checkout.

## Progressive disclosure

1. **Metadata** — `name` + `description` always in context (cheap routing).
2. **`SKILL.md` body** — loaded on `/skill <name>` or natural-language
   trigger (`/skills` lists them).
3. **`references/`** — deep docs the skill tells the agent to read *only
   when needed* (e.g. `coderai/skills/coderai-self-refer/references/`).

Write the body for step 2: self-contained procedure, then pointers into
`references/` for edge cases. If everything must be read every time, it
belongs in the body — references are for the long tail.

Invoke in-session:

```text
/skills              # browse discovered skills
/skill skill-writer  # load a skill into this session
```

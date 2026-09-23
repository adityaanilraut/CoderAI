<!-- Source: review agent 37433cbd-08bb-4d95-b87b-8e34d85c7b62 · CoderAI working tree @ 2026-09-22 (incl. uncommitted changes) · read-only review -->

# Prompts, agent specs and skills review — CoderAI

The most serious problem is that agent-role tool restrictions are broken almost everywhere. Five markdown roles get zero tools. `code-reviewer`, which the docs call read-only, gets all 47 tools. `--agent` and `/agent` never enforce any tool limits at all. And the read-only `explore` and `plan` subagents run without their read-only prompts.

I checked each finding in code, and most also by running the loaders in-process: I resolved the YAML specs, rendered the templates, built `SubAgentSpec` objects for every subagent type, and parsed test frontmatter through fake path objects. No repo files were changed.

**Context for the findings below:**
- The live runtime (`SessionManager`) builds its system prompt from the hardcoded `SYSTEM_PROMPT_BASE` in `coderai/prompt/__init__.py`.
- `coderai/agents/default/system.md` and `agent.yaml` only reach the model in two ways: through `--agent`/`/agent` (`render_system_prompt`), or through the new `coderai/app.py` → `load_agent` → `KimiSoul` path. Nothing imports `coderai/app.py`; only `tests/test_coderaisoul_lifecycle.py` uses it.

---

## (A) Prompt ↔ code inconsistencies

**A1. High — Markdown roles with capitalized tool names get zero tools as subagents.**
- **Where:** `coderai/subagents/runner.py:1045`, together with `.coderai/agents/{architect,planner,security-reviewer,tdd-guide,build-error-resolver}.md:4`.
- **Problem:** The runner filters with `if spec.allowed_tools and name not in spec.allowed_tools`, which is exact and case-sensitive. The frontmatter declares `["Read","Grep","Glob"]`, but the real tool names are `read`, `grep`, `glob`. The alias table and `is_tool_allowed()` in `registry.py:11-33,235` exist but are never called.
- **Evidence:** In a simulated run, all five roles ended up with `0 tools`.
- **Impact:** Each of these roles either wastes its turns or makes up findings because it cannot read anything.
- **Fix:** Use `is_tool_allowed(name, "allowlist", spec.allowed_tools)` in both the filter and the check at `runner.py:958`, or normalize names when parsing.

**A2. High — `code-reviewer` (`tools: []`) and unknown subagent types get the full toolset, the opposite of "deny by default".**
- **Where:** `.coderai/agents/code-reviewer.md:4`, `builder.py:223-228`, `runner.py:1045`, `registry.py:84-88`.
- **Problem:** An empty allowlist `()` is falsy in both the builder and the runner, so nothing gets filtered out. The mode check also needs a truthy `allowed_tools`, so `code-reviewer` is classified as `general`.
- **Evidence:** `code-reviewer` ended up with 47 tools in `general` mode, including `bash`, `write`, `edit`, `Task` and team tools. `subagent_type="bogus"` got 45 tools, even though the comment at `builder.py:216` promises deny-by-default.
- **Fix:** Use `is not None` checks, treat `()` as "deny all", and apply the read-only mode when the tool list is empty.

**A3. High — `allowedTools` is written but never read, so `--agent planner` and `/agent security-reviewer` run with every tool.**
- **Where:** Written at `coderai/cli/session_factory.py:67-68` and `coderai/soul/session/manager.py:310-311`.
- **Evidence:** A repo-wide `rg allowedTools` finds only those two writes.
- **Problem:** On top of that, `resolve_agent_spec` (`agentspec.py:212-213, 238-239`) sets `allowed_tools=None` whenever `defn.tools` is empty. So `--agent code-reviewer` gets `tools=["read","bash","edit"]` with no restriction at all.
- **Impact:** The read-only roles advertised in `AGENTS.md:29-33` can write files and run shell commands.
- **Fix:** Pass `allowedTools` into `get_tools()`/`to_openai_schemas` and into the executor's permission check, using alias-aware matching.

**A4. High — Builtin `explore` and `plan` subagents never receive their YAML role prompt and run in "GENERAL" mode.**
- **Where:** `registry.py:163-170`, `builder.py:237-238`, `explore.yaml:5-15`, `plan.yaml:5-11`.
- **Problem:** `_load_definitions` builds builtin definitions without `system_prompt` (so `ROLE_ADDITIONAL` is dropped) and with the default `mode="general"`. The builder then replaces `read_only` with `general`.
- **Evidence:** The simulation shows `explore mode=general sys_prompt=N` with `bash` available.
- **Impact:** The model gets "You are operating in GENERAL mode with workspace execution capabilities". This contradicts `agent.yaml:37` ("prompt-enforced read-only") and the `Task` schema text ("explore (read-only research)").
- **Fix:** Carry `system_prompt_args["ROLE_ADDITIONAL"]` into `system_prompt`, and set `mode="read_only"` for `explore` and `plan`.

**A5. High — `render_system_prompt` leaves Jinja and `${CODERAI_*}` placeholders in the prompt sent to the model.**
- **Where:** `coderai/agentspec.py:159-185`, used by `session_factory.py:61` and `manager.py:306`.
- **Problem:** It only does a regex substitution of `${VAR}` using the spec args, and never supplies the builtin variables.
- **Evidence:** `resolve_agent_spec("default")` renders with 8 unresolved variables (`CODERAI_OS`, `CODERAI_SHELL`, `CODERAI_NOW`, `CODERAI_WORK_DIR`, `CODERAI_WORK_DIR_LS`, `CODERAI_AGENTS_MD`, `CODERAI_SKILLS`, `CODERAI_ADDITIONAL_DIRS_INFO`) and 4 raw `{% if %}`/`{% endif %}` tags.
- **Impact:** `--agent default`, `--agent okabe` and `/agent default` all replace the working base prompt with this broken text.
- **Fix:** Render through the Jinja path in `soul/agent.py:_load_system_prompt` with a filled-in `BuiltinSystemPromptArgs`, and fail on undefined variables.

**A6. High — `load_agent()` can't load any bundled spec, because `load_tools` expects `module:Class` paths but every YAML lists bare names.**
- **Where:** `coderai/soul/agent.py:282-285` → `soul/toolset.py:692`.
- **Evidence:** `CoderAIToolset._load_tool("bash", {})` raises `ValueError: not enough values to unpack`.
- **Impact:** `CoderAIApp.create()` (`coderai/app.py:156`) crashes on `DEFAULT_AGENT_FILE`.
- **Fix:** Map spec names to tool classes through the registry, or remove this path (see D1).

**A7. High — Compaction in the `KimiSoul` runtime always crashes on an `ImportError`.**
- **Where:** `coderai/soul/compaction.py:532`.
- **Problem:** It runs `from coderai.prompt import prompts_dir`, but no `prompts_dir` exists anywhere. I confirmed the `ImportError` directly.
- **Impact:** `SimpleCompaction.prepare()` is called from `coderaisoul.py:2211`, so every compaction there fails, and `compact.md` is never used. This bug comes straight from the `prompt`/`prompts` naming confusion (see D4).
- **Fix:** Use `Path(coderai.prompts.__file__).parent` or `coderai.prompts.COMPACT`.

**A8. Medium — The system prompt's "Available Tools" section is built from a fixed map, not the live tool set.**
- **Where:** `coderai/prompt/__init__.py:488-503` (`get_preset_tools(None) or frozenset(TOOL_GUIDANCE_MAP)`).
- **What the default prompt advertises that doesn't exist:** `get_goal`, `create_goal` and `update_goal`, none of which are real tools. It also always advertises `UnderstandImage`, which only exists for vision models, and both `bash` and `pwsh`.
- **What exists but gets no guidance:** 21 real tools, including `send_message`, `list_agents`, `terminal_*`, `schedule_*` and `wait_agent`.
- **Mismatched guidance:** The `goal` entry (`:415-419`) documents `list`/`add`/`update`/`done` actions, but the schema only has `title`, `description` and `milestones`.
- **Fix:** Build the docs from the names returned by `get_tools(options)`.

**A9. Medium — The `enter_plan_mode` description uses tool names that don't exist.**
- **Where:** `coderai/tools/plan/enter_description.md:7,15-21,31-35`. This text is loaded live.
- **Problem:** It refers to `EnterPlanMode`, `ExitPlanMode`, `Agent(subagent_type="explore")`, `Glob`, `Grep` and `ReadFile`. The real names are `enter_plan_mode`, `exit_plan_mode`, `Task`/`subagent`, `glob`, `grep` and `read`.
- **Fix:** Rename them in the markdown.

**A10. Medium — `extend:` resolves inherited relative paths against the child file's directory.**
- **Where:** `agentspec.py:122-126, 136-138`.
- **Evidence:** Okabe's inherited subagent paths resolve to `coderai/agents/okabe/{coder,explore,plan}.yaml`, and none of those files exist. `system.md` only works because of a silent fallback to the default directory.
- **Knock-on effects:** Child specs don't set `name`, so `coder`, `explore` and `plan` all resolve to `name="default"`. Also, `subagents:` set to null in a child (`coder.yaml:24`) is meant to clear the inherited subagents, but it's ignored because of `elif value is not None` (`:96`).
- **Fix:** Resolve paths to absolute inside `_resolve_dict` using the directory each value came from, and treat an explicit null as an override.

**A11. Medium — `AGENTS.md` claims don't match what the code does.**
- **Line 28:** Okabe is described as a "mad-scientist persona with advanced toolsets". In reality its prompt is "meticulous senior engineer", and it has *fewer* tools: `okabe/agent.yaml` drops `Think`, the team tools and `wait_agent`.
- **Line 31:** `code-reviewer` is listed as read-only (see A2 and A3).
- **Line 14:** The discovery list leaves out `~/.coderai/agents`, which the code also scans (`registry.py:122`).
- **Tool lists:** The `tools:` list in `agent.yaml` has no effect on the live root agent, which exposes all 47 registry tools.
- **Fix:** Update `AGENTS.md`, or make the specs actually control tools.

**A12. Low — `system.md` refers to parameters and tools that don't match.**
- **Where:** `system.md:14, 52`.
- **Problem:** It says `Task` can resume "by `agent_id`", but the `Task` schema only has `description`, `prompt`, `run_in_background` and `subagent_type`. It also refers to the `Task` tool, but `agent.yaml` lists `subagent` instead.
- **Fix:** Align the wording with the schema.

## (B) Prompt quality issues

**B1. High — The `code-reviewer.md` prompt is overfit to specific benchmark cases and contradicts itself.**
- **Overfitting:** Lines 15-43 mention specific identifiers from outside projects: `isConditionalPasskeysEnabled`, `UpdateCompatibilityCheck`, `ASN1Encoder`, `ClientPermissionsV2`, `santizeAnchors`, `spans.buffer.flusher.*`, and `messages_lt.properties`. This looks like a benchmark answer key.
- **Contradictions:**
  - Line 31 says to flag race conditions, but line 51 says "Do NOT speculate on … race conditions".
  - Line 8 says "Do not inspect the local workspace", but line 37 says "You MUST inspect translation files".
- **Wrong context:** It assumes a diff is "provided directly in the prompt", which isn't true when it's used via `--agent` or as a subagent.
- **Fix:** Rewrite it as a general checklist and move benchmark-specific rules out of the repo.

**B2. High — The live base prompt is tuned for SWE-bench and has none of `system.md`'s safety guardrails.**
- **Where:** `coderai/prompt/__init__.py:45-66`.
- **Problem:** It requires a reproduction test, a `git diff` check and a test-suite run for *every* task ("conclude immediately"). It has no git-mutation ban, no "don't touch files outside the working directory" rule, and no "reply in the user's language" rule. `rg "git push"` finds no such rule in any live prompt; those guardrails exist only in the unused `system.md:54, 65, 73`.
- **Fix:** Move the guardrails into `SYSTEM_PROMPT_BASE` and make the reproduction-test workflow conditional on bug fixes.

**B3. Medium — There are three plan-mode instruction sets that contradict each other.**
- **The system prompt section** (`PLAN_MODE_PROMPT`, `prompt/__init__.py:68-142`) says to put the final plan in a `<proposed_plan>` block and allows running tests and builds.
- **The injection** (`plan_mode.py:28-57`) says "MUST NOT run non-readonly tools", to write `.coderai/plans/<id>.md`, and to end every turn with `AskUserQuestion` or `exit_plan_mode` rather than asking for approval in text.
- **The tool description** (`enter_description.md`) says to present the plan via "ExitPlanMode".
- **Extra conflicts:**
  - In afk mode the injection tells the model *never* to call `AskUserQuestion` (`afk_mode.py:25`), while plan mode requires it.
  - In non-interactive mode `AskUserQuestion` is removed from the docs (`:496`), but the plan reminder still demands it.
  - The `exit_plan_mode` description (`plan/description.md`, "Leave Plan Mode while mutation tools stay active") never mentions user approval or its `summary` parameter.
- **Fix:** Settle on one workflow and generate all three texts from a single source.

**B4. Medium — Project instructions and skills from untrusted repos get system-level authority with no delimiting.**
- **AGENTS.md and rules files:** `load_agent_instructions` (`prompt/__init__.py:784-832`) puts `AGENTS.md`, `CLAUDE.md` and `.coderai/rules/*.md` into the **system** message behind a single `--- Project Instructions ---` header, with no closing delimiter and no "treat as data" framing.
- **Skills:** SKILL.md files from `./.claude/skills` and other roots are appended as `system` messages (`manager.py:1429-1436`). They're also auto-injected based on a raw substring match (`skill/__init__.py:402`, `name_lower in prompt_lower`), so a project skill with a short name like `e` gets injected on nearly every prompt.
- **Reminder spoofing:** `read` and `bash` output isn't scanned for spoofed `<system-reminder>` tags, even though the runtime puts real reminders inside tool results (`toolset.py:101-133`). WebFetch is the only tool with sanitization.
- **Fix:** Wrap instructions and skills in explicit tagged blocks with a "treat as data" preamble, match skill names on word boundaries or tokens only, and escape reminder tags in tool output.

**B5. Medium — The live compaction directive can lose important state.**
- **Where:** `compaction.py:300-318`.
- **Missing:** It has no errors-and-fixes section, no verbatim user messages, and no exact paths or line numbers. It also doesn't carry over plan-mode state or the plan file path, loaded skills, background job IDs, subagent IDs, or todo state.
- **Divergence:** The other two compaction prompts (`COMPACT_PROMPT_BASE` at `prompt/__init__.py:144-157` and `prompts/compact.md`) do include errors, user messages and current work.
- **Fix:** Use one template that includes these items, and re-inject runtime state after compaction.

**B6. Medium — The afk-disabled reminder is exported but never sent.**
- **Where:** `afk_mode.py:31-38, 59-61`.
- **Problem:** `on_afk_changed` only resets `_injected`, and `get_injections` returns nothing when afk is off.
- **Impact:** After afk is turned off, the "Do NOT call AskUserQuestion" reminder stays in history with nothing to counter it.
- **Fix:** Emit `AFK_DISABLED_REMINDER` once, after a switch from on to off.

**B7. Medium — `/agent <role>` says it switched, but the current session doesn't change.**
- **Where:** `manager.py:296-320`.
- **Problem:** The persona only goes into `get_system_prompt` when a session is created (`:1086, :1199`), and the system message is never rebuilt.
- **Fix:** Rebuild the system message on switch, or tell the user it only applies to new sessions.

**B8. Low — `/init` has three different prompts that disagree.**
- **Where:** `prompts/init.md` (shell `/init`, `dispatch.py:1014`), `get_init_command_prompt` (`prompt/__init__.py:854`, used when a literal `/init` user message is rewritten in `message_converter.py:285`), and `soul/slash.py:29`.
- **Conflict:** One asks for a "thorough summary", the other for "Repository Guidelines, 200-400 words".
- **Broken:** The `soul/slash.py` version imports `load_agents_md`, which doesn't exist.
- **Fix:** Keep one.

**B9. Low — Other smaller prompt issues.**
- **Only one instruction file is loaded:** `load_agent_instructions` stops at the first file found (`:810`). So `.coderai/AGENTS.md` hides the root `AGENTS.md`, and `~/.coderai/AGENTS.md` (user-wide preferences) is dropped whenever a project file exists. Files are also cut off at 8000 characters without warning.
- **Duplicated guidance:** `TOOL_GUIDANCE_MAP` repeats the schema descriptions, costing about 4.5k characters per prompt.
- **Overlapping tools:** Both `todo_write` ("wraps UpdatePlan") and `UpdatePlan` are exposed.
- **Wrong tool reference:** `security-reviewer.md` says "Run security-oriented checks", but it has no `bash` tool.

## (C) Loader and templating bugs

**C1. High — Markdown frontmatter parsing fails open.**
- **Where:** `coderai/subagents/registry.py:36-64, 77-96`.
- **Evidence** (parsed through fake path objects):
  - A comma-separated `tools: Read, Grep, Glob` (Claude-Code style) becomes a string, so `allowed_tools=None` and the role is **unrestricted**.
  - Any YAML error, such as a colon in the description, sends parsing to the fallback line parser. That parser drops block lists, so the role is again **unrestricted**.
  - `exclude_tools:` set to null raises `TypeError`. Nothing catches it, so all of discovery fails. Inside `SubAgentSpec.__post_init__` the exception is swallowed (`builder.py:241`), which leaves the subagent **unrestricted**.
  - `exclude_tools: bash` becomes `('b','a','s','h')`.
  - `supports_background: 'false'` becomes `True`.
- **Fix:** Validate the frontmatter with a schema, accept comma-separated strings, log and skip invalid specs, and deny all tools when parsing fails.

**C2. Medium — Several spec fields are parsed but never used.**
- `model:` in markdown specs is dropped (`agentspec.py:210, 235` hardcode `model=None`).
- `exclude_tools` is never applied in the subagent runner (only in the unused `load_agent`).
- `when_to_use` and `list_subagent_types()` are never shown to the model: the `Task`/`subagent` `subagent_type` description only lists `coder`, `explore` and `plan`, so the discovered roles can't be found.
- Builtin definitions override custom ones silently (`registry.py:177`).
- **Fix:** Wire these fields up or remove them, and generate the `subagent_type` description from the registry.

**C3. Medium — The flow decision retry loop has no limit.**
- **Where:** `coderai/skill/flow/runner.py:152-170`.
- **Problem:** An invalid or missing `<choice>` causes an unlimited `while True` loop of full LLM turns, and these don't count against `max_moves`. Matching is also exact and case-sensitive (`:191`), so `<choice>stop</choice>` fails.
- **Also:** The Ralph prompt text "Including it will stop further iterations" (`:84-86`) doesn't make sense in context.
- **Fix:** Cap retries at around 3, count them as moves, and match case-insensitively.

**C4. Low — Silent `except Exception: pass` blocks hide broken specs.**
- **Where:** `registry.py:161, 171` and `builder.py:241`.
- **Problem:** If `default/agent.yaml` fails to load, all builtin types quietly disappear. Unknown types then fall into the full-toolset path described in A2.
- **Fix:** Log at warning level and fail closed.

## (D) Dead or unused prompt content

**D1. Medium — A whole parallel runtime is effectively dead code.**
- **What it covers:** `system.md`, the `tools:` lists in `agent.yaml`, `load_agent`/`BuiltinSystemPromptArgs` (`soul/agent.py:20-30, 229-305`), `coderai/app.py`, `SimpleCompaction` with `prompts/compact.md`, and `soul/slash.py`.
- **Evidence:** Nothing imports `coderai/app.py` (only one test does), and each of these pieces is broken (A5, A6, A7, B8).
- **Fix:** Either delete it, or make `SessionManager` render `system.md` and use it as the single source of truth.

**D2. Low — Nine tool-description markdown files are never loaded.**
- **Files:** `tools/agent/description.md`, `tools/background/{list,output,stop}.md`, `tools/shell/bash.md`, `tools/think/think.md`, `tools/todo/set_todo_list.md`, `tools/web/{fetch,search}.md`.
- **Evidence:** No `load_desc` or `read_text` reference in their packages. The registry repeats `agent/description.md` word for word inline.
- **Fix:** Load them from the registry or delete them.

**D3. Low — Unused exports in `coderai/prompt/__init__.py` and dead injection code.**
- **Unused:** `TOOL_DOCS` (`:505`), `build_cache_stabilized_messages` (`:611`) and `_project_guidance` (`:907`) have no callers in the runtime.
- **Buggy but unused:** The `KimiSoul` injection path passes `Message` objects to providers that expect dicts (`coderaisoul.py:1141`), which would re-send the full plan reminder on every step. That path is also dead.

**D4. Low — Having both a `coderai/prompt/` and a `coderai/prompts/` package is confusing, and has already caused a bug.**
- **Problem:** Inline prompts (`SYSTEM_PROMPT_BASE`, `PLAN_MODE_PROMPT`, `COMPACT_PROMPT_BASE` and the init f-string) live in `prompt/`, while the markdown prompts (`INIT`, `COMPACT`) live in `prompts/`. The `prompts_dir` import bug in A7 is a direct result of this confusion.
- **Fix:** Merge them into one package (for example, `coderai/prompt/` with a `templates/` folder of `.md` files) and a single loader.

**D5. Low — The skill docs disagree with the code.**
- **Where:** `coderai-self-refer/SKILL.md:30-37`.
- **Problem:** Its discovery order leaves out `./.claude/skills`, `~/.claude/skills` and where custom paths are placed, all of which the code scans (`skill/__init__.py:26-49`).
- **Also:** `image-generator/SKILL.md:36` runs `python3 scripts/image_generator.py` without saying it has to be run from the skill directory (the working directory is the project root).

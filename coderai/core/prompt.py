"""System prompts, tool schemas, and runtime context for the agent.

Cache-aware ordering: the system prompt (tools + runtime context) is a stable
prefix that changes rarely; the volatile user content (history, snippets, the
current turn) is appended after it, so provider prompt caches hit on every turn.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import platform
import subprocess
from typing import Any

from coderai.core.prompt_sections import (
    PERSONA_ORDER,
    SANDBOX_POLICY_ORDER,
    SKILLS_CATALOG_ORDER,
    SUBAGENT_DELEGATION_ORDER,
    TOOL_BASH_ORDER,
    TOOL_EDIT_ORDER,
    TOOL_GOAL_ORDER,
    TOOL_GREP_ORDER,
    TOOL_JOBS_ORDER,
    TOOL_PWSH_ORDER,
    TOOL_READ_ORDER,
    TOOL_WEB_FETCH_ORDER,
    TOOL_WRITE_ORDER,
    INSTRUCTIONS_ORDER,
    PLAN_MODE_ORDER,
    PromptSection,
    assemble_sections,
    get_preset_tools,
    is_restricted_tool_preset,
)
from coderai.core.sandbox import sandbox_policy_prompt
from coderai.core.common.shell_utils import resolve_shell_path
from coderai.core.skill import (
    list_skills,
)

SYSTEM_PROMPT_BASE = """You are a helpful software engineer assistant.

## Test Generation & Invariant Reasoning Rules
When writing, modifying, or analyzing reproduction tests and unit tests:
1. **Mental Post-Fix Trace**: Before writing test assertions, mentally trace what every assertion will evaluate to AFTER the bug is fixed. Never include assertions that assume, assert, or validate buggy behavior.
2. **Multi-Assertion Invariant Validation**: Ensure every single assertion in the test method tests a valid expected state under the official specification. Do not assume broken invariants or rely solely on the first failure.
3. **Specification Consistency**: Never include assertions that contradict the intended specification (e.g. asserting len==1 on evicted caches or assertFalse on valid token grants).

## Step Verification Loop Before Task Completion
Before concluding your task or issuing your final response:
1. **Inspect Git Diff**: Inspect `git diff` on modified files to verify you modified only the intended files without collateral changes or syntax errors.
2. **Run Regression Test Suite**: Run the relevant test suite (e.g. `pytest` or test runner) to verify:
   - Your reproduction test fails specifically due to the reported issue (and not an import error or syntax bug).
   - All existing passing tests continue to pass without regression.
3. **Verify Expected States**: Confirm all test assertions specifically target the issue without assuming broken invariants."""

PLAN_MODE_PROMPT = """# Plan Mode

You are in **Plan Mode**. Your goal is to explore the environment, gather facts, clarify intent, and produce a complete, actionable implementation plan before any code is modified.

Separately, `UpdatePlan` is CoderAI's checklist/progress tool. It updates the current task plan with a complete markdown task list, but it does not enter or exit Plan Mode and it is not the final planning artifact. Do not use `UpdatePlan` as a substitute for the `<proposed_plan>` block.

## Execution vs. mutation in Plan Mode

You may explore and execute **non-mutating** actions that improve the plan. You must not perform **mutating** actions.

### Allowed (non-mutating, plan-improving)

Actions that gather truth, reduce ambiguity, or validate feasibility without changing repo-tracked state:
- Reading or searching files, configs, schemas, types, manifests, and docs
- Static analysis, inspection, and repo exploration
- Dry-run style commands when they do not edit repo-tracked files
- Tests, builds, or checks that may write to caches or build artifacts so long as they do not edit repo-tracked files

### Not allowed (mutating, plan-executing)

Actions that implement the plan or change repo-tracked state:
- Editing or writing files
- Running formatters or linters that rewrite files
- Applying patches, migrations, or codegen that updates repo-tracked files
- Side-effectful commands whose purpose is to carry out the plan rather than refine it

When in doubt: if the action would reasonably be described as "doing the work" rather than "planning the work," do not do it.

## PHASE 1 — Ground in the environment (explore first, ask second)

Begin by grounding yourself in the actual environment. Eliminate unknowns in the prompt by discovering facts, not by asking the user. Resolve all questions that can be answered through exploration or inspection. Silent exploration between turns is allowed and encouraged.

Before asking the user any question, perform at least one targeted non-mutating exploration pass (for example: search relevant files, inspect likely entrypoints/configs, confirm current implementation shape).

Do not ask questions that can be answered from the repo or system. Only ask once you have exhausted reasonable non-mutating exploration.

## PHASE 2 — Intent chat (what they actually want)

- Keep asking until you can clearly state: goal + success criteria, audience, in/out of scope, constraints, current state, and the key preferences/tradeoffs.
- Bias toward questions over guessing: if any high-impact ambiguity remains, do NOT plan yet—ask using `AskUserQuestion`.

## PHASE 3 — Implementation chat (what/how we’ll build)

- Once intent is stable, keep asking until the spec is decision complete: approach, interfaces (APIs/schemas/I/O), data flow, edge cases/failure modes, testing + acceptance criteria.

## Finalization rule

Only output the final plan when it is decision complete and leaves no decisions to the implementer.

When you present the official plan, wrap it in a `<proposed_plan>` block so the client can render it specially:

1. The opening tag must be on its own line.
2. Start the plan content on the next line (no text on the same line as the tag).
3. The closing tag must be on its own line.
4. Use Markdown inside the block.
5. Keep the tags exactly as `<proposed_plan>` and `</proposed_plan>`.

Example:

<proposed_plan>
# Plan Title

## Summary
...

## Key Changes
...

## Verification Plan
...

## Assumptions
...
</proposed_plan>
"""

COMPACT_PROMPT_BASE = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should capture technical details, code patterns, and architectural decisions essential for continuing work without losing context.

The summary should include:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections examined, modified, or created
4. Errors and fixes
5. Problem Solving
6. All user messages (non-tool)
7. Pending Tasks
8. Current Work (precisely what was being worked on immediately before this summary)
9. Optional Next Step
"""


def get_plan_mode_prompt() -> str:
    return PLAN_MODE_PROMPT.strip()


MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # DeepSeek
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    # Claude
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-5-haiku": 200_000,
    # Gemini
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o1-preview": 128_000,
    "o3-mini": 200_000,
    "gpt-5.6-luna": 128_000,
}


def get_model_context_limit(model: str | None = None) -> int:
    """Resolve context window token limit for a model name."""
    if not model:
        return 128_000
    m = model.lower()
    for pattern, limit in MODEL_CONTEXT_WINDOWS.items():
        if pattern in m:
            return limit
    if "gemini" in m:
        return 1_000_000
    if "claude" in m:
        return 200_000
    if "deepseek" in m:
        return 128_000
    if "gpt-4" in m or "o1" in m or "o3" in m or "gpt-5" in m:
        return 128_000
    return 128_000


def calculate_context_budget(
    model: str | None = None,
    system_tokens: int = 0,
    tool_tokens: int = 0,
    safety_margin_tokens: int = 2000,
    pressure_ratio: float = 0.75,
    overflow_ratio: float = 0.95,
) -> dict[str, int]:
    """Calculate adaptive context budget based on model context limits and active usage."""
    context_limit = get_model_context_limit(model)
    max_output = min(8192, int(context_limit * 0.1))
    if context_limit >= 1_000_000:
        max_output = 16384

    reserved_system = int(system_tokens + tool_tokens + safety_margin_tokens)
    pressure_threshold = int(context_limit * pressure_ratio)
    overflow_threshold = int(context_limit * overflow_ratio)
    available_history = max(0, context_limit - max_output - reserved_system)
    compaction_target = int(context_limit * 0.40)

    return {
        "context_limit": context_limit,
        "max_output_tokens": max_output,
        "reserved_system_tokens": reserved_system,
        "pressure_threshold": pressure_threshold,
        "overflow_threshold": overflow_threshold,
        "available_history_budget": available_history,
        "compaction_target_tokens": compaction_target,
    }


def get_compact_prompt_token_threshold(model: str | None = None) -> int:
    """Return token threshold after which auto-compaction triggers based on adaptive budgeting."""
    budget = calculate_context_budget(model)
    return budget["pressure_threshold"]


def get_subagent_system_prompt(mode: str = "read_only", description: str = "") -> str:
    """Return specialized static system prompt for isolated sub-agents."""
    del description  # task descriptions are passed in the initial user prompt for cache stability
    mode_text = (
        "You are operating in READ-ONLY mode. You may explore, read, search, and analyze files, "
        "but you must NOT mutate or create repo files. Use read and WebSearch tools freely."
        if mode == "read_only"
        else "You are operating in GENERAL mode with workspace execution capabilities."
    )
    return (
        "You are an expert autonomous sub-agent.\n"
        f"{mode_text}\n"
        "Your task is to thoroughly analyze the objective, use your available tools to gather facts, "
        "and produce a concise, complete, and decision-ready conclusion for the parent agent. "
        "Do not leave ambiguities open; report exact findings, file paths, line numbers, and actionable summaries."
    )


def get_tools(
    options: dict[str, Any] | None = None,
    external_tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve formatted tool definitions using ToolRegistry as the canonical source of truth."""
    from coderai.core.tools.registry import get_tool_registry

    registry = get_tool_registry()
    return registry.to_openai_schemas(
        options=options,
        external_tools=external_tools,
    )


def format_tool_definitions(
    tools: list[dict[str, Any]],
    model: str = "",
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Format tool definitions for specific model families (e.g. strict schemas vs standard function calling)."""
    from coderai.core.prompt_sections import order_tools
    from coderai.core.tools.types import canonicalize_tool_schema

    formatted: list[dict[str, Any]] = []
    is_strict_model = bool(strict)

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and "function" in tool:
            func = dict(tool["function"])
            params = dict(func.get("parameters") or {})
            if is_strict_model:
                func["strict"] = True
                params["additionalProperties"] = False
            func["parameters"] = canonicalize_tool_schema(params)
            formatted.append(
                canonicalize_tool_schema(
                    {
                        "type": "function",
                        "function": func,
                    }
                )
            )
        else:
            formatted.append(canonicalize_tool_schema(tool))
    return order_tools(formatted)


TOOL_GUIDANCE_MAP: dict[str, tuple[str, int, str]] = {
    "bash": (
        "tool:bash",
        TOOL_BASH_ORDER,
        '## bash\nExecute shell commands. Provide `command`, a clear `description`, and the permission `sideEffects` array (or `["unknown"]` when effects cannot be classified). Set `persistent` only when shell state must survive across calls. Use `run_in_background` for long jobs and track them with job_list / job_output / job_kill. Use standard POSIX shell commands (e.g. `sed -n 10,25p file.py` or `python3` instead of non-portable GNU flags like `cat -A`).',
    ),
    "pwsh": (
        "tool:pwsh",
        TOOL_PWSH_ORDER,
        "## pwsh\nExecute PowerShell commands with background job and timeout support.",
    ),
    "job_list": (
        "tool:job_list",
        TOOL_JOBS_ORDER,
        "## job_list\nList background jobs (running and finished) with ids, kinds, and statuses.",
    ),
    "job_output": (
        "tool:job_output",
        TOOL_JOBS_ORDER,
        "## job_output\nRead a background job's output since the previous read. Set `wait: true` only when you are blocked on that job. Every response ends with `[status: ...]`.",
    ),
    "job_kill": (
        "tool:job_kill",
        TOOL_JOBS_ORDER,
        "## job_kill\nCancel a running background job by `job_id`.",
    ),
    "glob": (
        "tool:glob",
        TOOL_READ_ORDER,
        '## glob\nUse the glob tool — not shell find — to discover files by path pattern. A pattern with no "/" matches basenames at any depth, so "*" matches every file in the tree rather than its top level. Results are files only, never directories, and include hidden and ignored files: a result that fits comes back in modification-time order, while a larger one is sampled across top-level entries, so it spans the tree instead of one subtree.',
    ),
    "grep": (
        "tool:grep",
        TOOL_GREP_ORDER,
        "## grep\nUse the grep tool — not shell grep or rg — to search file contents. Use read on a matched file when you need surrounding context.",
    ),
    "read": (
        "tool:read",
        TOOL_READ_ORDER,
        "## read\nRead a file. Returns a numbered preview plus a `snippet_id` or line numbers for edits. Use `offset`/`limit` to read a range.",
    ),
    "edit": (
        "tool:edit",
        TOOL_EDIT_ORDER,
        "## edit\nUse the edit tool for targeted changes to existing UTF-8 text files. It replaces literal `old_string` with `new_string`; by default `old_string` must appear exactly once. If `old_string` appears multiple times, provide a more specific `old_string` or set `replace_all` to true. Always read the target file first to obtain the exact text.",
    ),
    "write": (
        "tool:write",
        TOOL_WRITE_ORDER,
        "## write\nCreate or overwrite a file with a complete string payload. Must read the full file first when overwriting. Prefer `edit` for existing files.",
    ),
    "str_replace_editor": (
        "tool:str_replace_editor",
        TOOL_EDIT_ORDER,
        "## str_replace_editor\nAnthropic-style file editor supporting view, create, str_replace, insert, and undo_edit operations.",
    ),
    "Task": (
        "tool:Task",
        SUBAGENT_DELEGATION_ORDER,
        "## Task\nSpawn a one-shot sub-agent for focused exploration and wait for aggregated findings. Prefer `subagent` when the child should continue across follow-up messages.",
    ),
    "subagent": (
        "tool:subagent",
        SUBAGENT_DELEGATION_ORDER,
        "## subagent\nStart a continuable background sub-agent and get an agent id. Steer it with `send_message`, inspect with `list_agents`, cancel with `interrupt_agent`. One-shot work uses `Task` / `subagent_fork`.",
    ),
    "todo_write": (
        "tool:todo_write",
        TOOL_GOAL_ORDER,
        "## todo_write\nReplace the structured todo list (wraps UpdatePlan). Each item has content and status.",
    ),
    "exit_plan_mode": (
        "tool:exit_plan_mode",
        PERSONA_ORDER + 1,
        "## exit_plan_mode\nLeave Plan Mode after the plan is approved. Mutation tools stay in the schema for KV-cache stability.",
    ),
    "goal": (
        "tool:goal",
        TOOL_GOAL_ORDER,
        "## goal\nTrack session goals (`list` / `add` / `update` / `done`).",
    ),
    "AskUserQuestion": (
        "tool:AskUserQuestion",
        108,
        "## AskUserQuestion\nPause to ask the user a clarifying question when the task is ambiguous.",
    ),
    "UpdatePlan": (
        "tool:UpdatePlan",
        TOOL_GOAL_ORDER,
        "## UpdatePlan\nUpdate the current markdown task plan.",
    ),
    "skill": (
        "tool:skill",
        SKILLS_CATALOG_ORDER,
        "## skill\nLoad the full SKILL.md instructions for a named skill from the session catalog before acting on a matching task.",
    ),
    "WebSearch": (
        "tool:WebSearch",
        TOOL_WEB_FETCH_ORDER,
        "## WebSearch\nSearch the web with a natural-language query.",
    ),
    "WebFetch": (
        "tool:WebFetch",
        TOOL_WEB_FETCH_ORDER,
        "## WebFetch\nFetch a URL and return sanitized Markdown (or raw text). Use after WebSearch when you need the page contents.",
    ),
    "UnderstandImage": (
        "tool:UnderstandImage",
        112,
        "## UnderstandImage\nAnalyze a local image (JPEG/PNG/WebP).",
    ),
    "session_search": (
        "tool:session_search",
        115,
        "## session_search\nSearch past session metadata, titles, and prompt snippets by keyword.",
    ),
    "session_trace": (
        "tool:session_trace",
        115,
        "## session_trace\nInspect session timeline and event history traces.",
    ),
    "session_event_search": (
        "tool:session_event_search",
        115,
        "## session_event_search\nSearch specific session event logs by query.",
    ),
    "session_event_read": (
        "tool:session_event_read",
        115,
        "## session_event_read\nRead event entries from a specific session event log offset.",
    ),
}


def render_tool_docs(preset: str | None = None, non_interactive: bool = False) -> str:
    """Render tool documentation sections scoped to the active preset."""
    active_tools = get_preset_tools(preset) or frozenset(TOOL_GUIDANCE_MAP)

    sections = []
    for tool_name, (_sec_name, sec_order, doc_text) in TOOL_GUIDANCE_MAP.items():
        if tool_name not in active_tools:
            continue
        if non_interactive and tool_name == "AskUserQuestion":
            continue
        sections.append((sec_order, doc_text))

    sections.sort(key=lambda s: s[0])
    docs_body = "\n\n".join(s[1] for s in sections)
    return f"# Available Tools\n\n{docs_body}" if docs_body else ""


TOOL_DOCS = render_tool_docs()


CACHE_BOUNDARY_TOKEN = "<!-- CODERAI_KV_CACHE_PREFIX_BOUNDARY -->"


def render_skill_catalog(
    project_root: str | None = None,
    enabled_skills: dict[str, bool] | None = None,
    custom_scan_paths: list[str] | None = None,
) -> str | None:
    """Render a compact catalog of available skills for the system prompt with deterministic sorting.

    Instead of inlining entire SKILL.md documents into the context window,
    this lists each skill by name and brief description, instructing the model
    to load the full skill instructions on-demand via the `skill` tool.
    """
    raw_skills = list_skills(
        project_root=project_root,
        enabled_skills=enabled_skills,
        custom_scan_paths=custom_scan_paths,
    )
    if not raw_skills:
        return None

    # Deterministic alphabetical ordering by skill name
    skills = sorted(raw_skills, key=lambda s: str(s.get("name", "")).lower())

    entries = []
    for s in skills:
        name = s.get("name", "")
        desc = (s.get("description") or "").replace("\n", " ").strip()
        if name and desc:
            entries.append(f"- `{name}`: {desc}")
        elif name:
            entries.append(f"- `{name}`")

    if not entries:
        return None

    catalog_text = "\n".join(entries)
    return (
        "# Available Skills\n\n"
        "The following specialized skills are available in this environment. "
        "To load full instructions for any skill, call the `skill` tool with the exact name before proceeding with matching tasks:\n\n"
        f"{catalog_text}"
    )


def get_system_prompt(options: dict[str, Any] | None = None) -> str:
    options = options or {}
    preset = options.get("preset") or options.get("toolsPreset")
    non_interactive = bool(options.get("nonInteractive", False))
    docs = render_tool_docs(preset=preset, non_interactive=non_interactive)

    persona_text = str(options.get("persona") or SYSTEM_PROMPT_BASE)
    complete = bool(options.get("complete", False))

    sections = [
        PromptSection("deployment:persona", PERSONA_ORDER, persona_text, complete=complete),
    ]
    if docs:
        sections.append(PromptSection("tools", TOOL_READ_ORDER, docs))

    sandbox_mode = options.get("sandboxMode")
    if sandbox_mode:
        sections.append(
            PromptSection(
                "sandbox:policy",
                SANDBOX_POLICY_ORDER,
                sandbox_policy_prompt(str(sandbox_mode), str(options.get("workspaceRoot") or "")),
            )
        )

    workspace_root = str(options.get("workspaceRoot") or "")
    allow_skills = options.get("enabledSkills") is not False and not is_restricted_tool_preset(
        preset
    )
    if workspace_root and allow_skills:
        skill_catalog = render_skill_catalog(
            project_root=workspace_root,
            enabled_skills=options.get("enabledSkills"),
            custom_scan_paths=options.get("skillScanPaths"),
        )
        if skill_catalog:
            sections.append(PromptSection("skills:catalog", SKILLS_CATALOG_ORDER, skill_catalog))

    instructions = options.get("instructions")
    if instructions and isinstance(instructions, str) and instructions.strip():
        sections.append(
            PromptSection("project:instructions", INSTRUCTIONS_ORDER, instructions.strip())
        )
    elif workspace_root and options.get("loadInstructions") is True:
        loaded_inst = load_agent_instructions(workspace_root)
        if loaded_inst:
            sections.append(
                PromptSection("project:instructions", INSTRUCTIONS_ORDER, loaded_inst.strip())
            )

    plan_mode = options.get("planMode") or options.get("plan_mode")
    if plan_mode:
        sections.append(PromptSection("mode:plan", PLAN_MODE_ORDER, get_plan_mode_prompt()))

    return assemble_sections(sections)


def build_cache_stabilized_messages(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[dict[str, Any]] | None = None,
    include_boundary_tag: bool = True,
    enable_cache_control: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Assemble stabilized prompt prefix and canonicalized tools for maximum KV-cache reuse.

    Guarantees:
    1. The leading system message contains frozen prompt text + cache boundary marker.
    2. Anthropic / OpenAI cache_control breakpoints are injected if enabled.
    3. Tool schemas are deterministically canonicalized and sorted.
    """
    stable_system_content = system_prompt.strip()
    if include_boundary_tag and CACHE_BOUNDARY_TOKEN not in stable_system_content:
        stable_system_content = f"{stable_system_content}\n\n{CACHE_BOUNDARY_TOKEN}"

    system_msg: dict[str, Any] = {
        "role": "system",
        "content": stable_system_content,
    }
    if enable_cache_control:
        system_msg["cache_control"] = {"type": "ephemeral"}

    filtered_messages = [m for m in messages if m.get("role") != "system"]
    stabilized_messages = [system_msg, *filtered_messages]

    stabilized_tools = None
    if tools is not None:
        from coderai.core.prompt_sections import order_tools
        from coderai.core.tools.types import canonicalize_tool_schema

        stabilized_tools = order_tools([canonicalize_tool_schema(t) for t in tools])

    return stabilized_messages, stabilized_tools



def get_compact_prompt(session_messages: list[Any]) -> str:
    lines = []
    for m in session_messages:
        lines.append(
            json_dumps(
                {
                    "id": getattr(m, "id", ""),
                    "role": getattr(m, "role", ""),
                    "content": getattr(m, "content", ""),
                }
            )
        )
    return (
        f"{COMPACT_PROMPT_BASE}\n\nconversation below:\n\n```jsonl\n" + "\n".join(lines) + "\n```"
    )


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def get_runtime_context(
    project_root: str,
    model: str | None = None,
    suppress_dynamic_time: bool = False,
) -> str:
    """Stable workspace env prefix (no git status / project docs — those are volatile)."""
    header_parts: list[str] = []
    if model:
        header_parts.append(f"Current LLM model: {model}.")
    if not suppress_dynamic_time:
        today = datetime.date.today().isoformat()
        header_parts.append(f"Today is {today}.")
    header = " ".join(header_parts) if header_parts else ""
    env: dict[str, Any] = {
        "homedir": str(pathlib.Path.home()),
        "pwd": project_root,
        "root path": project_root,
        "shell path": _shell_path(),
        "system info": f"{platform.system()} {platform.release()} {platform.machine()}",
    }
    py = _version("python3", ["--version"])
    if py:
        env["python3 version"] = py
    header_str = f"{header}\n\n" if header else ""
    return f"{header_str}# Local Workspace Environment\n\n```json\n{json.dumps(env, indent=2, sort_keys=True)}\n```"


def load_agent_instructions(project_root: str) -> str | None:
    """Load AGENTS.md / CODERAI.md / CLAUDE.md and modular rules as system instruction context."""
    root = pathlib.Path(project_root)
    home = pathlib.Path.home()
    candidates = [
        root / ".coderai" / "AGENTS.md",
        root / "AGENTS.md",
        root / ".agents" / "AGENTS.md",
        root / ".coderai" / "CODERAI.md",
        root / "CODERAI.md",
        root / "CLAUDE.md",
        home / ".coderai" / "AGENTS.md",
    ]
    parts: list[str] = []
    seen_files: set[pathlib.Path] = set()

    for path in candidates:
        if not path.is_file() or path in seen_files:
            continue
        seen_files.add(path)
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            parts.append(f"--- Project Instructions ({path.name}) ---\n{content[:8000]}")
            break

    # Discover modular rules under .coderai/rules/ and .agents/rules/
    rule_dirs = [root / ".coderai" / "rules", root / ".agents" / "rules"]
    rule_parts: list[str] = []
    for rdir in rule_dirs:
        if rdir.is_dir():
            for rfile in sorted(rdir.glob("*.md")):
                if rfile.is_file() and rfile not in seen_files:
                    seen_files.add(rfile)
                    try:
                        rcontent = rfile.read_text(encoding="utf-8").strip()
                        if rcontent:
                            rule_parts.append(f"--- Rule ({rfile.name}) ---\n{rcontent[:4000]}")
                    except OSError:
                        continue

    if rule_parts:
        parts.extend(rule_parts)

    if parts:
        return "\n\n".join(parts)
    return None



def get_effective_project_agents_md_file(project_root: str) -> str | None:
    """Return the relative display path of existing AGENTS.md / CODERAI.md file if present."""
    root = pathlib.Path(project_root)
    candidate_paths = [
        (root / ".coderai" / "AGENTS.md", "./.coderai/AGENTS.md"),
        (root / "AGENTS.md", "./AGENTS.md"),
        (root / ".coderai" / "CODERAI.md", "./.coderai/CODERAI.md"),
        (root / "CODERAI.md", "./CODERAI.md"),
    ]
    for abs_path, display_path in candidate_paths:
        if abs_path.is_file():
            try:
                if abs_path.read_text(encoding="utf-8").strip():
                    return display_path
            except OSError:
                pass
    return None


def get_init_command_prompt(project_root: str) -> str:
    """Render the /init command prompt template for generating or updating AGENTS.md."""
    agents_file = get_effective_project_agents_md_file(project_root)
    if agents_file is None:
        target_intro = "Generate a file named ./AGENTS.md that serves as a contributor guide for this repository."
    else:
        target_intro = (
            f"Update {agents_file} to align it with repository changes made after the last "
            f"time {agents_file} was modified."
        )

    return f"""{target_intro}
Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.
Follow the outline below, but adapt as needed — add sections if relevant, and omit those that do not apply to this project.

Document Requirements

- Title the document "Repository Guidelines".
- Use Markdown headings (#, ##, etc.) for structure.
- Keep the document concise. 200-400 words is optimal.
- Keep explanations short, direct, and specific to this repository.
- Provide examples where helpful (commands, directory paths, naming patterns).
- Maintain a professional, instructional tone.

Recommended Sections

Project Structure & Module Organization

- Outline the project structure, including where the source code, tests, and assets are located.

Build, Test, and Development Commands

- List key commands for building, testing, and running locally (e.g., npm test, make build, pytest).
- Briefly explain what each command does.

Coding Style & Naming Conventions

- Specify indentation rules, language-specific style preferences, and naming patterns.
- Include any formatting or linting tools used.

Testing Guidelines

- Identify testing frameworks and coverage requirements.
- State test naming conventions and how to run tests.

Commit & Pull Request Guidelines

- Summarize commit message conventions found in the project’s Git history.
- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).

(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions."""


def _project_guidance(project_root: str) -> str | None:
    return load_agent_instructions(project_root)


def _shell_path() -> str:
    try:
        return resolve_shell_path() or "sh"
    except Exception:
        return "sh"


def _version(command: str, args: list[str]) -> str | None:
    try:
        out = subprocess.run([command, *args], capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip().splitlines()[0]
    except Exception:
        return None
    return None


__all__ = [
    "render_skill_catalog",
    "get_tools",
    "format_tool_definitions",
    "get_system_prompt",
    "get_compact_prompt",
    "get_runtime_context",
    "load_agent_instructions",
    "get_init_command_prompt",
    "get_effective_project_agents_md_file",
    "calculate_context_budget",
    "get_compact_prompt_token_threshold",
    "get_subagent_system_prompt",
    "get_plan_mode_prompt",
    "get_model_context_limit",
    "CACHE_BOUNDARY_TOKEN",
    "build_cache_stabilized_messages",
    "TOOL_DOCS",
]


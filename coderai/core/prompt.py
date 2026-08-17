"""Prompts and tool definitions — port of deepcode core/src/prompt.ts.

Cache-aware ordering: the system prompt (tools + runtime context) is a stable
prefix that changes rarely; the volatile user content (history, snippets, the
current turn) is appended after it, so provider prompt caches hit on every turn.
"""

from __future__ import annotations

import pathlib
import platform
import re
import subprocess
from typing import Any

from coderai.core.common.model_capabilities import supports_multimodal
from coderai.core.common.shell_utils import resolve_shell_path

SYSTEM_PROMPT_BASE = "You are a helpful software engineer assistant."

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


def get_extension_root() -> str:
    import coderai

    return coderai.__file__.rsplit("/", 1)[0] if coderai.__file__ else "."


def get_plan_mode_prompt() -> str:
    return PLAN_MODE_PROMPT.strip()


def get_compact_prompt_token_threshold(model: str | None = None) -> int:
    """Return token threshold after which auto-compaction triggers."""
    return 100_000


def get_subagent_system_prompt(mode: str = "read_only", description: str = "") -> str:
    """Return specialized system prompt for isolated sub-agents."""
    mode_text = (
        "You are operating in READ-ONLY mode. You may explore, read, search, and analyze files, "
        "but you must NOT mutate or create repo files. Use read and WebSearch tools freely."
        if mode == "read_only"
        else "You are operating in GENERAL mode with workspace execution capabilities."
    )
    desc_text = f" Sub-task goal: '{description}'." if description else ""
    return (
        f"You are an expert autonomous sub-agent.{desc_text}\n"
        f"{mode_text}\n"
        "Your task is to thoroughly analyze the objective, use your available tools to gather facts, "
        "and produce a concise, complete, and decision-ready conclusion for the parent agent. "
        "Do not leave ambiguities open; report exact findings, file paths, line numbers, and actionable summaries."
    )


def get_tools(
    options: dict[str, Any] | None = None, external_tools: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    options = options or {}
    tools: list[dict[str, Any]] = [
        _fn(
            "bash",
            "Execute shell commands in a persistent bash session.",
            {
                "command": {"type": "string", "description": "The shell command to execute"},
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does in active voice.",
                },
                "sideEffects": {
                    "type": "array",
                    "description": "Permission scopes required by this bash command.",
                    "items": {"type": "string", "enum": sorted(BASH_SCOPE_ENUM)},
                    "uniqueItems": True,
                },
                "run_in_background": {"type": "boolean"},
            },
            ["command", "sideEffects"],
        ),
        _fn(
            "AskUserQuestion",
            "When the task has ambiguities or multiple implementation approaches, use this tool to pause and ask the user for clarification or a decision.",
            {
                "questions": {
                    "type": "array",
                    "description": "Questions to present to the user. Usually only one is needed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "multiSelect": {"type": "boolean"},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["question", "options"],
                    },
                }
            },
            ["questions"],
        ),
        _fn(
            "UpdatePlan",
            "Update the current task plan. The plan argument must be the complete markdown task list to show as the latest progress state.",
            {
                "plan": {"type": "string", "description": "The complete markdown task list."},
                "explanation": {
                    "type": "string",
                    "description": "Optional short reason for changing the plan.",
                },
            },
            ["plan"],
        ),
        _fn(
            "read",
            "Read files from the filesystem (text, images, notebooks).",
            {
                "file_path": {"type": "string", "description": "UNIX-style path to file"},
                "offset": {"type": "number", "description": "Line number to start reading from"},
                "limit": {"type": "number", "description": "Number of lines to read"},
            },
            ["file_path"],
        ),
        _fn(
            "write",
            "Create files or overwrite them with a complete string payload. Prefer edit for existing files.",
            {
                "file_path": {"type": "string", "description": "Absolute path to file"},
                "content": {
                    "type": "string",
                    "description": "Complete file content as a single string.",
                },
            },
            ["file_path", "content"],
        ),
        _fn(
            "edit",
            "Perform scoped string replacements in files.",
            {
                "snippet_id": {"type": "string", "description": "Required Read/Edit snippet_id."},
                "file_path": {
                    "type": "string",
                    "description": "Optional absolute path guard; must match snippet_id's file.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace inside snippet_id's scope",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text (must differ from old_string)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_string (default false)",
                },
                "expected_occurrences": {
                    "type": "number",
                    "description": "Expected number of matches",
                },
            },
            ["snippet_id", "old_string", "new_string"],
        ),
        _fn(
            "Task",
            "Spawn an isolated sub-agent to execute a specific sub-task (codebase exploration, file analysis, verification, or research) in an independent context and return aggregated findings.",
            {
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word summary of the sub-task.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions, objective, and context for the sub-agent.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["read_only", "general"],
                    "description": "Execution mode. 'read_only' allows read/search/bash without file mutation. 'general' allows full capabilities.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Optional timeout in seconds for sub-agent completion (default: 90).",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context snippets or file excerpts.",
                },
            },
            ["description", "prompt"],
        ),
    ]

    if options.get("nonInteractive") is True:
        tools = [t for t in tools if t["function"]["name"] != "AskUserQuestion"]

    tools.append(
        _fn(
            "WebSearch",
            "Perform web searching using a natural language query.",
            {
                "query": {
                    "type": "string",
                    "description": "A clear, specific natural language search query.",
                }
            },
            ["query"],
        )
    )

    if not supports_multimodal(options.get("model", "")):
        tools.append(
            _fn(
                "UnderstandImage",
                "Analyze or extract information from a local JPEG, PNG, or WebP image.",
                {
                    "prompt": {
                        "type": "string",
                        "description": "A clear instruction describing what to analyze.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "The absolute path of the image to analyze.",
                    },
                },
                ["prompt", "image_path"],
            )
        )

    for tool in external_tools or []:
        tools.append(tool)
    return tools


BASH_SCOPE_ENUM = {
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "unknown",
}


def _fn(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_DOCS = """# Available Tools

## bash
Execute shell commands in a persistent bash session. Provide `command`, a clear `description`, and the permission `sideEffects` array (or `["unknown"]` when effects cannot be classified).

## read
Read a file. Returns a numbered preview plus a `snippet_id` you must keep for later edits. Use `offset`/`limit` to read a range.

## edit
Perform a scoped string replacement. Requires the `snippet_id` from a prior `read`. `old_string` must match exactly within the snippet's line range; set `replace_all` to replace every occurrence and `expected_occurrences` to assert a count.

## write
Create or overwrite a file with a complete string payload. Must read the full file first when overwriting. Prefer `edit` for existing files.

## Task
Spawn an isolated sub-agent for focused exploration, research, or testing. Returns a summary without bloating the main conversation context.

## AskUserQuestion
Pause to ask the user a clarifying question when the task is ambiguous.

## UpdatePlan
Update the current markdown task plan.

## WebSearch
Search the web with a natural-language query.

## UnderstandImage
Analyze a local image (JPEG/PNG/WebP)."""


def get_system_prompt(options: dict[str, Any] | None = None) -> str:
    options = options or {}
    docs = TOOL_DOCS
    return f"{SYSTEM_PROMPT_BASE}\n\n{docs}"


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
    import json

    return json.dumps(obj, ensure_ascii=False)


def get_runtime_context(project_root: str, model: str | None = None) -> str:
    parts = [
        f"root path: {project_root}",
        f"pwd: {project_root}",
        f"system info: {platform.system()} {platform.release()} {platform.machine()}",
        f"shell path: {_shell_path()}",
    ]
    py = _version("python3", ["--version"])
    if py:
        parts.append(f"python3 version: {py}")
    git = _git_status(project_root)
    if git:
        parts.append(git)
    if model:
        parts.insert(0, f"Current LLM model: {model}.")

    guidance = _project_guidance(project_root)
    if guidance:
        parts.append(guidance)

    return "\n".join(parts)


def _project_guidance(project_root: str) -> str | None:
    for fname in (
        "AGENTS.md",
        ".coderai/AGENTS.md",
        "CODERAI.md",
        ".coderai/CODERAI.md",
        "CLAUDE.md",
    ):
        p = pathlib.Path(project_root) / fname
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    return f"\n--- Project Instructions ({fname}) ---\n{content[:8000]}"
            except Exception:
                pass
    return None


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


def _git_status(project_root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode != 0:
            return None
        return "git status:\n" + (out.stdout.strip()[:2000] or "clean")
    except Exception:
        return None


# --- skills (lazily-loaded context) ---

DEFAULT_SKILL_RESOURCE_FILE_LIMIT = 50

SKILL_RESOURCE_EXCLUDED_DIRS = {
    ".cache",
    ".git",
    ".next",
    ".turbo",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "__pycache__",
    ".venv",
    "venv",
}


def strip_skill_prompt_metadata(content: str) -> str:
    """Strip YAML frontmatter metadata from SKILL.md content."""
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(content)
    if match:
        return content[match.end() :].lstrip()
    return content


def extract_skill_frontmatter(content: str) -> dict[str, str]:
    """Extract metadata (name, description) from SKILL.md YAML frontmatter."""
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(content)
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip().lower()] = val.strip().strip("'\"")
    return meta


def list_skill_resource_files(
    skill_file_path: str, limit: int = DEFAULT_SKILL_RESOURCE_FILE_LIMIT
) -> tuple[list[str], bool]:
    """Discover helper and resource files located in the skill directory."""
    skill_dir = pathlib.Path(skill_file_path).parent
    if not skill_dir.is_dir():
        return [], False

    files: list[str] = []
    truncated = False

    for item in sorted(skill_dir.rglob("*")):
        if item.is_dir():
            continue
        parts = item.relative_to(skill_dir).parts
        if any(p in SKILL_RESOURCE_EXCLUDED_DIRS or p.startswith(".") for p in parts):
            continue
        rel = "/".join(parts)
        if rel == "SKILL.md":
            continue
        if len(files) >= limit:
            truncated = True
            break
        files.append(rel)

    return files[:limit], truncated


def render_skill_resources(skill_file_path: str | None) -> str:
    if not skill_file_path:
        return ""
    files, truncated = list_skill_resource_files(skill_file_path, DEFAULT_SKILL_RESOURCE_FILE_LIMIT)
    if not files and not truncated:
        return ""
    lines = [f"  <file>{_escape(f)}</file>" for f in files]
    if truncated:
        lines.append(
            f"  <note>Listing capped at {DEFAULT_SKILL_RESOURCE_FILE_LIMIT} files and may be incomplete.</note>"
        )
    return "\n\n<skill_resources>\n" + "\n".join(lines) + "\n</skill_resources>"


def render_skill_document_block(skill: dict[str, Any]) -> str:
    name = skill.get("name", "skill")
    path_attr = f' path="{_escape(skill.get("path", ""))}"' if skill.get("path") else ""
    content = strip_skill_prompt_metadata(skill.get("content", ""))
    skill_file_path = skill.get("skillFilePath") or skill.get("path")
    resources = render_skill_resources(skill_file_path)
    return f"<{name}-skill{path_attr}>\n{content}{resources}\n</{name}-skill>"


def build_skill_documents_prompt(skills: list[dict[str, Any]]) -> str:
    blocks = [render_skill_document_block(skill) for skill in skills]
    if not blocks:
        return ""
    return "Use the skill documents below to assist the user:\n" + "\n\n".join(blocks)


def list_skills(project_root: str | None = None) -> list[dict[str, str]]:
    """List bundled + project skills. Each is {"name", "path", "description"}."""
    roots: list[str] = []
    bundled = pathlib.Path(__file__).parent.parent / "skills"
    if bundled.is_dir():
        roots.append(str(bundled))
    if project_root:
        roots.append(str(pathlib.Path(project_root) / ".coderai" / "skills"))
    skills: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        p = pathlib.Path(root)
        if not p.is_dir():
            continue
        for skill_dir in sorted(p.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_file():
                name = skill_dir.name
                if name not in seen:
                    seen.add(name)
                    raw_content = skill_file.read_text(encoding="utf-8", errors="replace")
                    meta = extract_skill_frontmatter(raw_content)
                    desc = meta.get("description", "")
                    skills.append({"name": name, "path": str(skill_file), "description": desc})
    return skills


def load_skill(name: str, project_root: str | None = None) -> dict[str, Any] | None:
    for skill in list_skills(project_root):
        if skill["name"] == name:
            try:
                content = _read(skill["path"])
                return {
                    "name": name,
                    "content": content,
                    "path": skill["path"],
                    "skillFilePath": skill["path"],
                    "description": skill.get("description", ""),
                }
            except Exception:
                return None
    return None


def match_skills_for_prompt(
    user_prompt: str, project_root: str | None = None
) -> list[dict[str, str]]:
    """Match skills automatically based on user prompt query terms and skill descriptions."""
    if not user_prompt.strip():
        return []
    prompt_tokens = set(re.findall(r"\w+", user_prompt.lower()))
    matched: list[dict[str, str]] = []
    for skill in list_skills(project_root):
        name_tokens = set(re.findall(r"\w+", skill["name"].lower()))
        desc_tokens = set(re.findall(r"\w+", skill.get("description", "").lower()))
        if (name_tokens & prompt_tokens) or (desc_tokens & prompt_tokens):
            matched.append(skill)
    return matched


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )

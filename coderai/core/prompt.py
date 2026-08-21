"""Prompts and tool definitions — port of deepcode core/src/prompt.ts.

Cache-aware ordering: the system prompt (tools + runtime context) is a stable
prefix that changes rarely; the volatile user content (history, snippets, the
current turn) is appended after it, so provider prompt caches hit on every turn.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import platform
import re
import subprocess
from typing import Any

from coderai.core.common.model_capabilities import supports_multimodal
from coderai.core.common.shell_utils import resolve_shell_path
from coderai.core.prompt_sections import assemble_sections, order_tools, PromptSection
from coderai.core.sandbox import sandbox_policy_prompt

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
    return str(pathlib.Path(__file__).resolve().parent.parent)


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
            "job_list",
            "List your background jobs (running and finished) with their ids, kinds, and statuses.",
            {},
            [],
        ),
        _fn(
            "job_output",
            "Read a background job. Returns output since the previous read. Every response ends with `[status: ...]`. Set wait=true to block until settlement.",
            {
                "job_id": {
                    "type": "string",
                    "description": "Job id returned when the background work started.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Block until the job reaches a terminal status or the timeout expires.",
                },
                "timeout_ms": {
                    "type": "number",
                    "description": "Max wait in milliseconds when wait is true (default 30000, cap 600000).",
                },
            },
            ["job_id"],
        ),
        _fn(
            "job_kill",
            "Request cancellation of a running background job by job id.",
            {
                "job_id": {
                    "type": "string",
                    "description": "Job id returned when the background work started.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason recorded with the job.",
                },
            },
            ["job_id"],
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
            "glob",
            "Find files whose paths match a glob pattern. Returns matching file paths — never directories — including hidden and ignored files (VCS metadata directories are excluded). Up to 100 paths come back in modification-time order; a larger result instead returns 100 paths sampled across top-level entries, says so, and reports where the complete sorted list was saved. This tool does not enumerate directory entries.",
            {
                "pattern": {
                    "type": "string",
                    "description": 'Glob pattern to match file paths against (e.g. "**/*.ts", "src/**/*.test.js"). A pattern with no "/" matches the basename at any depth, so "*" and "*.ts" both search the whole tree; include a separator to anchor the depth.',
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it.",
                },
            },
            ["pattern"],
        ),
        _fn(
            "grep",
            "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns the first 250 matches inline; a capped result reports where the complete match list was saved. Use read on a matched file for surrounding context.",
            {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for (ripgrep syntax).",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search. Defaults to the session workspace; a relative path resolves against it.",
                },
                "include": {
                    "type": "string",
                    "description": 'One glob filter for which files to search (e.g. "*.ts", "*.{js,jsx}"). Not a list; negation is not supported.',
                },
            },
            ["pattern"],
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
            "skill",
            "Load the full instructions for an available skill. Call this with the exact skill name from the session skill catalog before acting on a task that names or clearly matches that skill.",
            {
                "name": {
                    "type": "string",
                    "description": "The exact skill name from the available skills list.",
                }
            },
            ["name"],
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

    tools.append(
        _fn(
            "WebFetch",
            "Fetch content from an external web URL, sanitize against prompt injection, and return clean Markdown or JSON.",
            {
                "url": {
                    "type": "string",
                    "description": "The HTTP or HTTPS URL to fetch content from.",
                },
                "raw": {
                    "type": "boolean",
                    "description": "If true, return raw plain text instead of parsed Markdown.",
                },
                "max_length": {
                    "type": "number",
                    "description": "Maximum characters to return (default: 30,000).",
                },
            },
            ["url"],
        )
    )

    if not supports_multimodal(options.get("model", ""), options.get("multimodal", "default")):
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

    extra: list[dict[str, Any]] = [
        _fn(
            "subagent",
            "Start a continuable sub-agent in the background and return an agent id. Use send_message / list_agents / interrupt_agent to steer it. For a one-shot child, use Task or subagent_fork.",
            {
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word summary of the sub-task.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Detailed instructions for the sub-agent.",
                },
                "mode": {"type": "string", "enum": ["read_only", "general"]},
                "timeout_seconds": {"type": "number"},
                "context": {"type": "string"},
            },
            ["description", "prompt"],
        ),
        _fn(
            "subagent_fork",
            "Spawn a one-shot sub-agent and wait for its aggregated findings (alias of Task).",
            {
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["read_only", "general"]},
                "timeout_seconds": {"type": "number"},
                "context": {"type": "string"},
            },
            ["description", "prompt"],
        ),
        _fn(
            "send_message",
            "Send a follow-up message to a running or parked sub-agent by agent id.",
            {
                "agent_id": {"type": "string"},
                "message": {"type": "string"},
            },
            ["agent_id", "message"],
        ),
        _fn(
            "interrupt_agent",
            "Cancel a running sub-agent by agent id.",
            {"agent_id": {"type": "string"}},
            ["agent_id"],
        ),
        _fn(
            "list_agents",
            "List sub-agents spawned from this session with ids and statuses.",
            {},
            [],
        ),
        _fn(
            "todo_write",
            "Replace the structured todo list. Each item has content and status (pending, in_progress, completed, cancelled).",
            {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                        },
                    },
                }
            },
            ["todos"],
        ),
        _fn(
            "exit_plan_mode",
            "Exit Plan Mode after the plan is approved. Mutation tools remain in the schema; this signals the client to lift the plan-mode fence.",
            {"summary": {"type": "string", "description": "Short summary of the approved plan."}},
            [],
        ),
        _fn(
            "goal",
            "List, add, or update session goals.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "update", "done", "cancel", "start"],
                },
                "title": {"type": "string"},
                "goal_id": {"type": "string"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
            },
            [],
        ),
        _fn(
            "str_replace_editor",
            "Anthropic-style custom file editor for viewing, creating, replacing string snippets, inserting lines, and undoing edits.",
            {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                },
                "path": {"type": "string"},
                "file_text": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "insert_line": {"type": "integer"},
                "view_range": {"type": "array", "items": {"type": "integer"}},
            },
            ["command", "path"],
        ),
        _fn(
            "terminal_open",
            "Open a persistent interactive PTY terminal session.",
            {
                "type": {"type": "string"},
                "name": {"type": "string"},
                "cwd": {"type": "string"},
            },
            [],
        ),
        _fn(
            "terminal_send",
            "Send text or a command to an open persistent terminal session.",
            {
                "sessionId": {"type": "string"},
                "text": {"type": "string"},
                "submit": {"type": "boolean"},
                "run_in_background": {"type": "boolean"},
                "timeout_ms": {"type": "number"},
            },
            ["sessionId", "text"],
        ),
        _fn(
            "terminal_read",
            "Read available output from an open persistent terminal session.",
            {
                "sessionId": {"type": "string"},
                "timeout_ms": {"type": "number"},
            },
            ["sessionId"],
        ),
        _fn(
            "terminal_signal",
            "Send a signal (SIGINT, SIGTERM, SIGKILL) to an open terminal session.",
            {
                "sessionId": {"type": "string"},
                "signal": {
                    "type": "string",
                    "enum": ["SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP"],
                },
            },
            ["sessionId", "signal"],
        ),
        _fn(
            "terminal_close",
            "Close and terminate a persistent terminal session.",
            {"sessionId": {"type": "string"}},
            ["sessionId"],
        ),
        _fn(
            "terminal_list",
            "List all active persistent terminal sessions.",
            {},
            [],
        ),
        _fn(
            "lsp",
            "Query Language Server Protocol (LSP) for precise code definitions, references, hover docstrings, and document symbols.",
            {
                "operation": {
                    "type": "string",
                    "enum": [
                        "goToDefinition",
                        "findReferences",
                        "goToImplementation",
                        "hover",
                        "documentSymbol",
                        "workspaceSymbol",
                    ],
                },
                "file_path": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
            },
            ["operation"],
        ),
        _fn(
            "schedule_create",
            "Create a durable reminder or recurring scheduled task.",
            {
                "prompt": {"type": "string"},
                "after_seconds": {"type": "integer"},
                "at": {"type": "string"},
                "every_seconds": {"type": "integer"},
            },
            ["prompt"],
        ),
        _fn(
            "schedule_list",
            "List all active and overdue scheduled reminders.",
            {},
            [],
        ),
        _fn(
            "schedule_delete",
            "Delete a scheduled reminder by schedule_id.",
            {"schedule_id": {"type": "string"}},
            ["schedule_id"],
        ),
        _fn(
            "workflow",
            "Execute an orchestration script that fans out subagents with pipeline, parallel, and schema validation primitives.",
            {
                "script": {"type": "string"},
                "meta": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "phases": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "args": {"type": "object"},
            },
            ["script"],
        ),
        _fn(
            "ralph",
            "Execute multi-round adversarial verification on an immutable objective with fresh child agents.",
            {
                "objective": {"type": "string"},
                "max_rounds": {"type": "integer"},
                "context": {"type": "string"},
                "mode": {"type": "string", "enum": ["general", "read_only"]},
                "timeout_per_round": {"type": "number"},
            },
            ["objective"],
        ),
        _fn(
            "spawn_teammate",
            "Spawn a dedicated role-based teammate in the multi-agent swarm.",
            {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "system_prompt": {"type": "string"},
                "mode": {"type": "string", "enum": ["general", "read_only"]},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
            },
            ["name", "role"],
        ),
        _fn(
            "team_task_create",
            "Create a task on the shared team task board.",
            {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assigned_to": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "dependencies": {"type": "array", "items": {"type": "string"}},
            },
            ["title"],
        ),
        _fn(
            "team_task_get",
            "Retrieve details of a task from the shared team task board.",
            {"task_id": {"type": "string"}},
            ["task_id"],
        ),
        _fn(
            "team_task_list",
            "List tasks on the shared team task board with optional filters.",
            {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                },
                "assigned_to": {"type": "string"},
            },
            [],
        ),
        _fn(
            "team_task_update",
            "Update status, assignee, result, or notes for a task on the shared task board.",
            {
                "task_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                },
                "assigned_to": {"type": "string"},
                "result": {"type": "string"},
                "notes": {"type": "string"},
            },
            ["task_id"],
        ),
        _fn(
            "wait_agent",
            "Wait for completion or message settlement from spawned teammates or subagents.",
            {
                "agent_id": {"type": "string"},
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number"},
                "wait_for": {"type": "string", "enum": ["completion", "message", "any_settlement"]},
            },
            [],
        ),
        _fn(
            "code_mode",
            "Execute Python code in a stateful sandbox with workspace tool helpers (read_file, write_file, edit_file, glob_search, grep_search, run_command).",
            {
                "code": {"type": "string"},
                "reset_state": {"type": "boolean"},
                "timeout_seconds": {"type": "number"},
            },
            ["code"],
        ),
        _fn(
            "session_query",
            "Search historical conversation turns, compacted history, and tool outputs using full-text search.",
            {
                "query": {"type": "string"},
                "session_id": {"type": "string"},
                "role": {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                "limit": {"type": "integer"},
            },
            ["query"],
        ),
        _fn(
            "pwsh",
            "Execute commands in a PowerShell session with background job and timeout support.",
            {
                "command": {"type": "string"},
                "description": {"type": "string"},
                "sideEffects": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(BASH_SCOPE_ENUM)},
                },
                "run_in_background": {"type": "boolean"},
            },
            ["command", "sideEffects"],
        ),
    ]
    if options.get("childAgent") is True:
        extra.append(
            _fn(
                "report",
                "Child-only: submit the final report for the parent agent and finish this sub-agent.",
                {"summary": {"type": "string"}},
                ["summary"],
            )
        )
    else:
        extra = [t for t in extra if t["function"]["name"] != "report"]

    tools.extend(extra)
    return order_tools(tools)


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


def format_tool_definitions(
    tools: list[dict[str, Any]],
    model: str = "",
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Format tool definitions for specific model families (e.g. strict schemas vs standard function calling)."""
    formatted: list[dict[str, Any]] = []
    is_strict_model = strict or any(
        k in model.lower()
        for k in (
            "gpt-5",
            "gpt-4.5",
            "gpt-4o",
            "o1",
            "o3",
            "deepseek-v4",
            "deepseek-r1",
            "deepseek-v3",
        )
    )

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and "function" in tool:
            func = dict(tool["function"])
            params = dict(func.get("parameters") or {})
            if is_strict_model:
                func["strict"] = True
                params["additionalProperties"] = False
            func["parameters"] = params
            formatted.append(
                {
                    "type": "function",
                    "function": func,
                }
            )
        else:
            formatted.append(tool)
    return formatted


TOOL_DOCS = """# Available Tools

## bash
Execute shell commands in a persistent bash session. Provide `command`, a clear `description`, and the permission `sideEffects` array (or `["unknown"]` when effects cannot be classified). Use `run_in_background` for long jobs and track them with job_list / job_output / job_kill.

## job_list
List background jobs (running and finished) with ids, kinds, and statuses.

## job_output
Read a background job's output since the previous read. Set `wait: true` only when you are blocked on that job. Every response ends with `[status: ...]`.

## job_kill
Cancel a running background job by `job_id`.

## glob
Use the glob tool — not shell find — to discover files by path pattern. A pattern with no "/" matches basenames at any depth, so "*" matches every file in the tree rather than its top level. Results are files only, never directories, and include hidden and ignored files: a result that fits comes back in modification-time order, while a larger one is sampled across top-level entries, so it spans the tree instead of one subtree.

## grep
Use the grep tool — not shell grep or rg — to search file contents. Use read on a matched file when you need surrounding context.

## read
Read a file. Returns a numbered preview plus a `snippet_id` you must keep for later edits. Use `offset`/`limit` to read a range.

## edit
Perform a scoped string replacement. Requires the `snippet_id` from a prior `read`. `old_string` must match exactly within the snippet's line range; set `replace_all` to replace every occurrence and `expected_occurrences` to assert a count.

## write
Create or overwrite a file with a complete string payload. Must read the full file first when overwriting. Prefer `edit` for existing files.

## Task
Spawn a one-shot sub-agent for focused exploration and wait for aggregated findings. Prefer `subagent` when the child should continue across follow-up messages.

## subagent
Start a continuable background sub-agent and get an agent id. Steer it with `send_message`, inspect with `list_agents`, cancel with `interrupt_agent`. One-shot work uses `Task` / `subagent_fork`.

## todo_write
Replace the structured todo list (wraps UpdatePlan). Each item has content and status.

## exit_plan_mode
Leave Plan Mode after the plan is approved. Mutation tools stay in the schema for KV-cache stability.

## goal
Track session goals (`list` / `add` / `update` / `done`).

## AskUserQuestion
Pause to ask the user a clarifying question when the task is ambiguous.

## UpdatePlan
Update the current markdown task plan.

## skill
Load the full SKILL.md instructions for a named skill from the session catalog before acting on a matching task.

## WebSearch
Search the web with a natural-language query.

## WebFetch
Fetch a URL and return sanitized Markdown (or raw text). Use after WebSearch when you need the page contents.

## UnderstandImage
Analyze a local image (JPEG/PNG/WebP)."""


def get_system_prompt(options: dict[str, Any] | None = None) -> str:
    options = options or {}
    docs = TOOL_DOCS
    if options.get("nonInteractive") is True:
        docs = re.sub(r"\n## AskUserQuestion\n.*?(?=\n## |\Z)", "", docs, flags=re.S)
    sections = [
        PromptSection("base", 10, SYSTEM_PROMPT_BASE),
        PromptSection("tools", 100, docs),
    ]
    sandbox_mode = options.get("sandboxMode")
    if sandbox_mode:
        sections.append(
            PromptSection(
                "sandbox:policy",
                90,
                sandbox_policy_prompt(str(sandbox_mode), str(options.get("workspaceRoot") or "")),
            )
        )
    return assemble_sections(sections)


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


def get_runtime_context(project_root: str, model: str | None = None) -> str:
    """Stable workspace env prefix (no git status / project docs — those are volatile)."""
    today = datetime.date.today().isoformat()
    header = f"Today is {today}."
    if model:
        header = f"Current LLM model: {model}. {header}"
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
    return f"{header}\n\n# Local Workspace Environment\n\n```json\n{json.dumps(env, indent=2, sort_keys=True)}\n```"


def load_agent_instructions(project_root: str) -> str | None:
    """Load AGENTS.md / CODERAI.md / CLAUDE.md as a separate (still-prefix) system message."""
    root = pathlib.Path(project_root)
    home = pathlib.Path.home()
    candidates = [
        root / ".coderai" / "AGENTS.md",
        root / ".coderAI" / "AGENTS.md",
        root / "AGENTS.md",
        root / ".coderai" / "CODERAI.md",
        root / ".coderAI" / "CODERAI.md",
        root / "CODERAI.md",
        root / "CLAUDE.md",
        home / ".coderai" / "AGENTS.md",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return f"--- Project Instructions ({path.name}) ---\n{content[:8000]}"
    return None


def get_effective_project_agents_md_file(project_root: str) -> str | None:
    """Return the relative display path of existing AGENTS.md / CODERAI.md file if present."""
    root = pathlib.Path(project_root)
    candidate_paths = [
        (root / ".coderai" / "AGENTS.md", "./.coderai/AGENTS.md"),
        (root / ".coderAI" / "AGENTS.md", "./.coderAI/AGENTS.md"),
        (root / ".deepcode" / "AGENTS.md", "./.deepcode/AGENTS.md"),
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


# --- skills (lazily-loaded context — delegated to coderai.core.skill) ---

from coderai.core.skill import (
    DEFAULT_SKILL_RESOURCE_FILE_LIMIT,
    SKILL_RESOURCE_EXCLUDED_DIRS,
    SkillRegistry,
    _implicit_invocation_allowed,
    build_skill_documents_prompt,
    extract_skill_frontmatter,
    get_bundled_skills_root,
    get_skill_read_exempt_paths,
    get_skill_scan_roots,
    list_skill_resource_files,
    list_skills,
    load_skill,
    match_skills_for_prompt,
    parse_skill_match_response,
    render_skill_document_block,
    render_skill_resources,
    strip_skill_prompt_metadata,
)

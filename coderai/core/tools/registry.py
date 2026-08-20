"""Modular, type-safe Tool Registry with strict JSON Schema and type validation."""

from __future__ import annotations

from typing import Any

from coderai.core.tools import ask_user_question as _ask
from coderai.core.tools import agents as _agents
from coderai.core.tools import bash as _bash
from coderai.core.tools import edit as _edit
from coderai.core.tools import jobs as _jobs
from coderai.core.tools import lsp as _lsp
from coderai.core.tools import plan_mode as _plan_mode
from coderai.core.tools import ralph as _ralph
from coderai.core.tools import read as _read
from coderai.core.tools import schedule as _schedule
from coderai.core.tools import search as _search_fs
from coderai.core.tools import skill as _skill
from coderai.core.tools import str_replace_editor as _str_replace
from coderai.core.tools import subagent as _subagent
from coderai.core.tools import terminal as _terminal
from coderai.core.tools import todo_write as _todo
from coderai.core.tools import understand_image as _image
from coderai.core.tools import update_plan as _plan
from coderai.core.tools import web_fetch as _fetch
from coderai.core.tools import web_search as _search
from coderai.core.tools import write as _write
from coderai.core.goals import handle_goal_tool as _goal_handle
from coderai.core.workflow import handle_workflow_tool as _workflow_handle
from coderai.core.code_mode import handle_code_mode_tool as _code_mode_handle
from coderai.core.session_query import handle_session_query_tool as _session_query_handle
from coderai.core.tools import pwsh as _pwsh
from coderai.core.teams import (
    handle_spawn_teammate_tool as _spawn_teammate_handle,
    handle_team_task_create_tool as _task_create_handle,
    handle_team_task_get_tool as _task_get_handle,
    handle_team_task_list_tool as _task_list_handle,
    handle_team_task_update_tool as _task_update_handle,
    handle_wait_agent_tool as _wait_agent_handle,
)
from coderai.core.tools.types import ToolDefinition, ValidationError

BASH_SCOPE_ENUM = [
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
]


class ToolRegistry:
    """Type-safe registry for built-in and dynamic agent tools with strict validation."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._register_builtins()

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a tool definition and its aliases."""
        self._tools[tool_def.name] = tool_def
        for alias in tool_def.aliases:
            self._aliases[alias] = tool_def.name

    def get(self, name: str) -> ToolDefinition | None:
        """Resolve a tool definition by name or alias."""
        canonical = self._aliases.get(name, name)
        return self._tools.get(canonical)

    def has_tool(self, name: str) -> bool:
        """Check if a tool exists by name or alias."""
        return self.get(name) is not None

    def list_tools(self) -> list[ToolDefinition]:
        """Return all registered tool definitions."""
        return list(self._tools.values())

    def get_openai_tool_definitions(
        self,
        options: dict[str, Any] | None = None,
        external_tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate OpenAI function definitions for all registered tools."""
        options = options or {}
        definitions: list[dict[str, Any]] = []

        for tool in self._tools.values():
            if options.get("nonInteractive") is True and tool.name == "AskUserQuestion":
                continue
            definitions.append(tool.to_openai_schema())

        for ext in external_tools or []:
            definitions.append(ext)

        return definitions

    def validate_arguments(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Strictly validate input arguments against the tool's parameter schema.

        Raises:
            ValidationError: If required fields are missing or types mismatch.
        """
        tool_def = self.get(name)
        if not tool_def:
            raise ValidationError(f"Tool '{name}' is not registered in the ToolRegistry.")

        if not isinstance(args, dict):
            raise ValidationError(f"Tool arguments for '{name}' must be a dictionary/object.")

        # 1. Check required fields
        for req in tool_def.required:
            if req not in args or args[req] is None:
                raise ValidationError(f"Tool '{name}' is missing required argument '{req}'.")

        # 2. Check parameter types and enum constraints
        for param_name, value in args.items():
            if value is None:
                continue

            param_spec = tool_def.parameters.get(param_name)
            if not param_spec:
                # Extra unknown argument - allow for forward compatibility
                continue

            expected_type = param_spec.get("type")
            if expected_type == "string":
                if not isinstance(value, str):
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be a string, got {type(value).__name__}."
                    )
            elif expected_type in ("number", "integer"):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be a number, got {type(value).__name__}."
                    )
            elif expected_type == "boolean":
                if not isinstance(value, bool):
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be a boolean, got {type(value).__name__}."
                    )
            elif expected_type == "array":
                if not isinstance(value, list):
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be an array/list, got {type(value).__name__}."
                    )
            elif expected_type == "object":
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be an object/dict, got {type(value).__name__}."
                    )

            # Enum constraints
            enum_vals = param_spec.get("enum")
            if enum_vals and value not in enum_vals:
                raise ValidationError(
                    f"Argument '{param_name}' for tool '{name}' has invalid value '{value}'. Allowed: {enum_vals}"
                )

        return args

    def _register_builtins(self) -> None:
        """Register the core standard built-in tools."""
        # 1. bash
        self.register(
            ToolDefinition(
                name="bash",
                aliases=["Bash"],
                description="Execute shell commands in a persistent bash session.",
                parameters={
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
                required=["command", "sideEffects"],
                handler=_bash.handle_bash_tool,
                category="shell",
                is_mutating=True,
            )
        )

        self.register(
            ToolDefinition(
                name="job_list",
                aliases=["JobList"],
                description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
                parameters={},
                required=[],
                handler=_jobs.handle_job_list_tool,
                category="shell",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="job_output",
                aliases=["JobOutput"],
                description=(
                    "Read a background job. Returns output since the previous read. "
                    "Every response ends with `[status: ...]`. Set wait=true to block until settlement."
                ),
                parameters={
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
                required=["job_id"],
                handler=_jobs.handle_job_output_tool,
                category="shell",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="job_kill",
                aliases=["JobKill"],
                description="Request cancellation of a running background job by job id.",
                parameters={
                    "job_id": {
                        "type": "string",
                        "description": "Job id returned when the background work started.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional short reason recorded with the job.",
                    },
                },
                required=["job_id"],
                handler=_jobs.handle_job_kill_tool,
                category="shell",
                is_mutating=True,
            )
        )

        # 2. glob / grep (workspace discovery — not shell find/rg)
        self.register(
            ToolDefinition(
                name="glob",
                aliases=["Glob"],
                description=_search_fs.GLOB_DESCRIPTION,
                parameters={
                    "pattern": {
                        "type": "string",
                        "description": (
                            'Glob pattern to match file paths against (e.g. "**/*.ts", "src/**/*.test.js"). '
                            'A pattern with no "/" matches the basename at any depth, so "*" and "*.ts" both search the whole tree; include a separator to anchor the depth.'
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to the session workspace; a relative path resolves against it.",
                    },
                },
                required=["pattern"],
                handler=_search_fs.handle_glob_tool,
                category="filesystem",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="grep",
                aliases=["Grep"],
                description=_search_fs.GREP_DESCRIPTION,
                parameters={
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
                required=["pattern"],
                handler=_search_fs.handle_grep_tool,
                category="filesystem",
                is_mutating=False,
            )
        )

        # 3. read
        self.register(
            ToolDefinition(
                name="read",
                aliases=["Read"],
                description="Read files from the filesystem (text, images, notebooks).",
                parameters={
                    "file_path": {"type": "string", "description": "UNIX-style path to file"},
                    "offset": {
                        "type": "number",
                        "description": "Line number to start reading from",
                    },
                    "limit": {"type": "number", "description": "Number of lines to read"},
                },
                required=["file_path"],
                handler=_read.handle_read_tool,
                category="filesystem",
                is_mutating=False,
            )
        )

        # 3. write
        self.register(
            ToolDefinition(
                name="write",
                aliases=["Write"],
                description="Create files or overwrite them with a complete string payload. Prefer edit for existing files.",
                parameters={
                    "file_path": {"type": "string", "description": "Absolute path to file"},
                    "content": {
                        "type": "string",
                        "description": "Complete file content as a single string.",
                    },
                },
                required=["file_path", "content"],
                handler=_write.handle_write_tool,
                category="filesystem",
                is_mutating=True,
            )
        )

        # 4. edit
        self.register(
            ToolDefinition(
                name="edit",
                aliases=["Edit"],
                description="Perform scoped string replacements in files.",
                parameters={
                    "snippet_id": {
                        "type": "string",
                        "description": "Required Read/Edit snippet_id.",
                    },
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
                required=["snippet_id", "old_string", "new_string"],
                handler=_edit.handle_edit_tool,
                category="filesystem",
                is_mutating=True,
            )
        )

        # 5. AskUserQuestion
        self.register(
            ToolDefinition(
                name="AskUserQuestion",
                aliases=["ask_user_question"],
                description="When the task has ambiguities or multiple implementation approaches, use this tool to pause and ask the user for clarification or a decision.",
                parameters={
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
                required=["questions"],
                handler=_ask.handle_ask_user_question_tool,
                category="interactive",
                is_mutating=False,
            )
        )

        # 6. UpdatePlan
        self.register(
            ToolDefinition(
                name="UpdatePlan",
                aliases=["update_plan"],
                description="Update the current task plan. The plan argument must be the complete markdown task list to show as the latest progress state.",
                parameters={
                    "plan": {"type": "string", "description": "The complete markdown task list."},
                    "explanation": {
                        "type": "string",
                        "description": "Optional short reason for changing the plan.",
                    },
                },
                required=["plan"],
                handler=_plan.handle_update_plan_tool,
                category="meta",
                is_mutating=False,
            )
        )

        # 7. UnderstandImage
        self.register(
            ToolDefinition(
                name="UnderstandImage",
                aliases=["understand_image"],
                description="Analyze or extract information from a local JPEG, PNG, or WebP image.",
                parameters={
                    "prompt": {
                        "type": "string",
                        "description": "A clear instruction describing what to analyze.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "The absolute path of the image to analyze.",
                    },
                },
                required=["prompt", "image_path"],
                handler=_image.handle_understand_image_tool,
                category="meta",
                rate_limited_id="UnderstandImage",
                is_mutating=False,
            )
        )

        # 8. WebSearch
        self.register(
            ToolDefinition(
                name="WebSearch",
                aliases=["web_search"],
                description="Perform web searching using a natural language query.",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "A clear, specific natural language search query.",
                    }
                },
                required=["query"],
                handler=_search.handle_web_search_tool,
                category="web",
                rate_limited_id="WebSearch",
                is_mutating=False,
            )
        )

        # 9. WebFetch
        self.register(
            ToolDefinition(
                name="WebFetch",
                aliases=["web_fetch"],
                description="Fetch content from an external web URL, sanitize against prompt injection, and return clean Markdown or JSON.",
                parameters={
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
                required=["url"],
                handler=_fetch.handle_web_fetch_tool,
                category="web",
                rate_limited_id="WebFetch",
                is_mutating=False,
            )
        )

        # 10. Task / subagent_fork (one-shot)
        self.register(
            ToolDefinition(
                name="Task",
                aliases=["task", "subagent_fork", "SubAgentFork"],
                description="Spawn a one-shot sub-agent and wait for aggregated findings.",
                parameters={
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
                        "description": "Execution mode ('read_only' or 'general').",
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
                required=["description", "prompt"],
                handler=_subagent.handle_subagent_tool,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="subagent",
                aliases=["SubAgent"],
                description="Start a continuable background sub-agent and return an agent id.",
                parameters={
                    "description": {"type": "string", "description": "Short 3-5 word summary."},
                    "prompt": {"type": "string", "description": "Detailed instructions."},
                    "mode": {"type": "string", "enum": ["read_only", "general"]},
                    "timeout_seconds": {"type": "number"},
                    "context": {"type": "string"},
                },
                required=["description", "prompt"],
                handler=_agents.handle_continuable_subagent_tool,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="send_message",
                aliases=["SendMessage"],
                description="Send a follow-up message to a sub-agent by agent id.",
                parameters={
                    "agent_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                required=["agent_id", "message"],
                handler=_agents.handle_send_message_tool,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="interrupt_agent",
                aliases=["InterruptAgent"],
                description="Cancel a running sub-agent by agent id.",
                parameters={"agent_id": {"type": "string"}},
                required=["agent_id"],
                handler=_agents.handle_interrupt_agent_tool,
                category="subagent",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="list_agents",
                aliases=["ListAgents"],
                description="List sub-agents spawned from this session.",
                parameters={},
                required=[],
                handler=_agents.handle_list_agents_tool,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="report",
                aliases=["Report"],
                description="Child-only: submit the final report for the parent agent.",
                parameters={"summary": {"type": "string"}},
                required=["summary"],
                handler=_agents.handle_report_tool,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="todo_write",
                aliases=["TodoWrite", "todoWrite"],
                description="Replace the structured todo list. Wraps UpdatePlan.",
                parameters={
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
                required=["todos"],
                handler=_todo.handle_todo_write_tool,
                category="meta",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="exit_plan_mode",
                aliases=["ExitPlanMode"],
                description="Exit Plan Mode after the plan is approved. Mutation tools remain in the schema.",
                parameters={"summary": {"type": "string"}},
                required=[],
                handler=_plan_mode.handle_exit_plan_mode_tool,
                category="meta",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="goal",
                aliases=["Goal"],
                description="List, add, or update session goals.",
                parameters={
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "update", "done", "cancel", "start"],
                    },
                    "title": {"type": "string"},
                    "goal_id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                required=[],
                handler=_goal_handle,
                category="meta",
                is_mutating=False,
            )
        )

        # 11. skill (Dynamic skill loader)
        self.register(
            ToolDefinition(
                name="skill",
                aliases=["Skill", "load_skill"],
                description="Load the full instructions for an available skill. Call this with the exact skill name from the session skill catalog before acting on a task that names or clearly matches that skill.",
                parameters={
                    "name": {
                        "type": "string",
                        "description": "The exact skill name from the available skills list.",
                    }
                },
                required=["name"],
                handler=_skill.handle_skill_tool,
                category="meta",
                is_mutating=False,
            )
        )

        # 12. str_replace_editor (Anthropic-style file editor)
        self.register(
            ToolDefinition(
                name="str_replace_editor",
                aliases=["StrReplaceEditor"],
                description="Anthropic-style custom file editor for viewing, creating, replacing string snippets, inserting lines, and undoing edits.",
                parameters={
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                        "description": "The command to run: `view`, `create`, `str_replace`, `insert`, `undo_edit`.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to the file or directory.",
                    },
                    "file_text": {
                        "type": "string",
                        "description": "Content of the file to create (for `create`).",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact unique string to replace (for `str_replace`).",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement string (for `str_replace`) or string to insert (for `insert`).",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "Line number after which to insert `new_str` (for `insert`).",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Line range `[start, end]` to view (for `view`).",
                    },
                },
                required=["command", "path"],
                handler=_str_replace.handle_str_replace_editor_tool,
                category="filesystem",
                is_mutating=True,
            )
        )

        # 13. Persistent PTY Terminals
        self.register(
            ToolDefinition(
                name="terminal_open",
                aliases=["TerminalOpen"],
                description="Open a persistent interactive PTY terminal session.",
                parameters={
                    "type": {
                        "type": "string",
                        "description": "Shell command/type (default 'bash')",
                    },
                    "name": {"type": "string", "description": "Optional name for terminal session"},
                    "cwd": {"type": "string", "description": "Optional initial working directory"},
                },
                required=[],
                handler=_terminal.handle_terminal_open_tool,
                category="shell",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="terminal_send",
                aliases=["TerminalSend"],
                description="Send text or a command to an open persistent terminal session.",
                parameters={
                    "sessionId": {"type": "string", "description": "Target terminal session ID"},
                    "text": {"type": "string", "description": "Text/command to send to stdin"},
                    "submit": {"type": "boolean", "description": "Append newline (default true)"},
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Run in background and return immediately",
                    },
                    "timeout_ms": {
                        "type": "number",
                        "description": "Max wait time in milliseconds",
                    },
                },
                required=["sessionId", "text"],
                handler=_terminal.handle_terminal_send_tool,
                category="shell",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="terminal_read",
                aliases=["TerminalRead"],
                description="Read available output from an open persistent terminal session.",
                parameters={
                    "sessionId": {"type": "string", "description": "Target terminal session ID"},
                    "timeout_ms": {
                        "type": "number",
                        "description": "Max wait time in milliseconds",
                    },
                },
                required=["sessionId"],
                handler=_terminal.handle_terminal_read_tool,
                category="shell",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="terminal_signal",
                aliases=["TerminalSignal"],
                description="Send a signal (SIGINT, SIGTERM, SIGKILL) to an open terminal session.",
                parameters={
                    "sessionId": {"type": "string", "description": "Target terminal session ID"},
                    "signal": {
                        "type": "string",
                        "enum": ["SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP"],
                        "description": "Signal name",
                    },
                },
                required=["sessionId", "signal"],
                handler=_terminal.handle_terminal_signal_tool,
                category="shell",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="terminal_close",
                aliases=["TerminalClose"],
                description="Close and terminate a persistent terminal session.",
                parameters={
                    "sessionId": {"type": "string", "description": "Target terminal session ID"}
                },
                required=["sessionId"],
                handler=_terminal.handle_terminal_close_tool,
                category="shell",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="terminal_list",
                aliases=["TerminalList"],
                description="List all active persistent terminal sessions.",
                parameters={},
                required=[],
                handler=_terminal.handle_terminal_list_tool,
                category="shell",
                is_mutating=False,
            )
        )

        # 14. Language Server Protocol (LSP)
        self.register(
            ToolDefinition(
                name="lsp",
                aliases=["Lsp"],
                description="Query Language Server Protocol (LSP) for precise code definitions, references, hover docstrings, and document symbols.",
                parameters={
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
                        "description": "LSP query operation",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to the target file",
                    },
                    "line": {
                        "type": "integer",
                        "description": "1-based line number",
                    },
                    "character": {
                        "type": "integer",
                        "description": "1-based character position",
                    },
                },
                required=["operation"],
                handler=_lsp.handle_lsp_tool,
                category="filesystem",
                is_mutating=False,
            )
        )

        # 15. Schedule Subsystem
        self.register(
            ToolDefinition(
                name="schedule_create",
                aliases=["ScheduleCreate"],
                description="Create a durable reminder or recurring scheduled task. Provide prompt and exactly one of after_seconds, at, or every_seconds.",
                parameters={
                    "prompt": {
                        "type": "string",
                        "description": "Notification message or task instruction",
                    },
                    "after_seconds": {
                        "type": "integer",
                        "description": "Relative delay in seconds (e.g. 60)",
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO 8601 / RFC 3339 UTC target instant",
                    },
                    "every_seconds": {
                        "type": "integer",
                        "description": "Fixed interval in seconds (minimum 300 / 5 minutes)",
                    },
                },
                required=["prompt"],
                handler=_schedule.handle_schedule_create_tool,
                category="meta",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="schedule_list",
                aliases=["ScheduleList"],
                description="List all active and overdue scheduled reminders.",
                parameters={},
                required=[],
                handler=_schedule.handle_schedule_list_tool,
                category="meta",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="schedule_delete",
                aliases=["ScheduleDelete"],
                description="Delete a scheduled reminder by schedule_id.",
                parameters={
                    "schedule_id": {
                        "type": "string",
                        "description": "ID of the schedule to delete",
                    }
                },
                required=["schedule_id"],
                handler=_schedule.handle_schedule_delete_tool,
                category="meta",
                is_mutating=True,
            )
        )

        # 16. Workflow Scripting Engine
        self.register(
            ToolDefinition(
                name="workflow",
                aliases=["Workflow"],
                description="Execute an orchestration script that fans out subagents with pipeline, parallel, and schema validation primitives.",
                parameters={
                    "script": {
                        "type": "string",
                        "description": "Python orchestration script using workflow primitives (agent, pipeline, parallel, phase, log).",
                    },
                    "meta": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "phases": {"type": "array", "items": {"type": "string"}},
                        },
                        "description": "Metadata for the workflow execution (name, description, phases).",
                    },
                    "args": {
                        "type": "object",
                        "description": "Optional dictionary of arguments passed into the script scope.",
                    },
                },
                required=["script"],
                handler=_workflow_handle,
                category="subagent",
                is_mutating=True,
            )
        )

        # 17. Ralph Automated Verification Engine
        self.register(
            ToolDefinition(
                name="ralph",
                aliases=["Ralph"],
                description="Execute multi-round adversarial verification on an immutable objective with fresh child agents.",
                parameters={
                    "objective": {
                        "type": "string",
                        "description": "The immutable specification or goal to verify.",
                    },
                    "max_rounds": {
                        "type": "integer",
                        "description": "Maximum number of verification rounds (default: 5).",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional initial context or guidelines.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["general", "read_only"],
                        "description": "Execution mode for child agents ('general' or 'read_only').",
                    },
                    "timeout_per_round": {
                        "type": "number",
                        "description": "Max timeout in seconds per round (default: 90).",
                    },
                },
                required=["objective"],
                handler=_ralph.handle_ralph_tool,
                category="subagent",
                is_mutating=True,
            )
        )

        # 18. Agent Teams & Swarm Coordination Tools
        self.register(
            ToolDefinition(
                name="spawn_teammate",
                aliases=["SpawnTeammate"],
                description="Spawn a dedicated role-based teammate in the multi-agent swarm.",
                parameters={
                    "name": {
                        "type": "string",
                        "description": "Name of the teammate agent.",
                    },
                    "role": {
                        "type": "string",
                        "description": "Role of the teammate (e.g. 'architect', 'coder', 'reviewer', 'tester', 'researcher').",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional custom system instructions for this teammate.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["general", "read_only"],
                        "description": "Execution mode ('general' or 'read_only').",
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of allowed tool names for this teammate.",
                    },
                },
                required=["name", "role"],
                handler=_spawn_teammate_handle,
                category="subagent",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="team_task_create",
                aliases=["TeamTaskCreate"],
                description="Create a task on the shared team task board.",
                parameters={
                    "title": {
                        "type": "string",
                        "description": "Short title of the task.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed task description and requirements.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Optional teammate ID or role assigned to this task.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Priority level of the task.",
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of task IDs that must complete before this task.",
                    },
                },
                required=["title"],
                handler=_task_create_handle,
                category="subagent",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="team_task_get",
                aliases=["TeamTaskGet"],
                description="Retrieve details of a task from the shared team task board.",
                parameters={
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to retrieve.",
                    },
                },
                required=["task_id"],
                handler=_task_get_handle,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="team_task_list",
                aliases=["TeamTaskList"],
                description="List tasks on the shared team task board with optional filters.",
                parameters={
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                        "description": "Optional status filter.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "Optional assignee filter.",
                    },
                },
                required=[],
                handler=_task_list_handle,
                category="subagent",
                is_mutating=False,
            )
        )
        self.register(
            ToolDefinition(
                name="team_task_update",
                aliases=["TeamTaskUpdate"],
                description="Update status, assignee, result, or notes for a task on the shared task board.",
                parameters={
                    "task_id": {
                        "type": "string",
                        "description": "ID of the task to update.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                        "description": "New status for the task.",
                    },
                    "assigned_to": {
                        "type": "string",
                        "description": "New assignee for the task.",
                    },
                    "result": {
                        "type": "string",
                        "description": "Completion result or summary.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes or blockers.",
                    },
                },
                required=["task_id"],
                handler=_task_update_handle,
                category="subagent",
                is_mutating=True,
            )
        )
        self.register(
            ToolDefinition(
                name="wait_agent",
                aliases=["WaitAgent"],
                description="Wait for completion or message settlement from spawned teammates or subagents.",
                parameters={
                    "agent_id": {
                        "type": "string",
                        "description": "ID of the teammate or subagent to await.",
                    },
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of teammate or subagent IDs to await.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Maximum seconds to wait (default: 60).",
                    },
                    "wait_for": {
                        "type": "string",
                        "enum": ["completion", "message", "any_settlement"],
                        "description": "Settlement condition to wait for (default: 'completion').",
                    },
                },
                required=[],
                handler=_wait_agent_handle,
                category="subagent",
                is_mutating=False,
            )
        )

        # 19. Code Mode & Interactive Execution
        self.register(
            ToolDefinition(
                name="code_mode",
                aliases=["CodeMode", "python_exec"],
                description="Execute Python code in a stateful sandbox with workspace tool helpers (read_file, write_file, edit_file, glob_search, grep_search, run_command).",
                parameters={
                    "code": {
                        "type": "string",
                        "description": "The Python code snippet or script to execute.",
                    },
                    "reset_state": {
                        "type": "boolean",
                        "description": "Optional flag to reset the variable environment before running.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Maximum execution time in seconds (default: 30).",
                    },
                },
                required=["code"],
                handler=_code_mode_handle,
                category="meta",
                is_mutating=True,
            )
        )

        # 20. Session Query & Full-Text Search (FTS)
        self.register(
            ToolDefinition(
                name="session_query",
                aliases=["SessionQuery", "session_search", "SessionSearch"],
                description="Search historical conversation turns, compacted history, and tool outputs using full-text search.",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Natural language or keyword search query.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session ID to limit search to a specific session.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["user", "assistant", "tool", "system"],
                        "description": "Optional role filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 10).",
                    },
                },
                required=["query"],
                handler=_session_query_handle,
                category="meta",
                is_mutating=False,
            )
        )

        # 21. Cross-Platform PowerShell Execution
        self.register(
            ToolDefinition(
                name="pwsh",
                aliases=["PowerShell", "powershell"],
                description="Execute commands in a PowerShell session with background job and timeout support.",
                parameters={
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command or script to execute.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Clear description of what this command does.",
                    },
                    "sideEffects": {
                        "type": "array",
                        "description": "Permission scopes required by this command.",
                        "items": {"type": "string", "enum": sorted(BASH_SCOPE_ENUM)},
                        "uniqueItems": True,
                    },
                    "run_in_background": {"type": "boolean"},
                },
                required=["command", "sideEffects"],
                handler=_pwsh.handle_pwsh_tool,
                category="shell",
                is_mutating=True,
            )
        )


# Global default tool registry
_default_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _default_tool_registry

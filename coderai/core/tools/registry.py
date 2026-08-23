"""Modular, type-safe Tool Registry with scoped layers, strict JSON Schema, and DeepSeek Harness parity."""

from __future__ import annotations

import copy
from typing import Any
from collections.abc import Callable, Sequence

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
from coderai.core.tools.types import (
    ToolDefinition,
    ToolPresentationMode,
    ValidationError,
    canonicalize_tool_schema,
)
from coderai.core.tools.schema import define_tool

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


class ToolLayer:
    """Scoped tool registration layer supporting dynamic overrides, filters, and guards."""

    def __init__(self, scope: str | None = None) -> None:
        self.scope = scope
        self.tools: dict[str, ToolDefinition] = {}
        self.aliases: dict[str, str] = {}
        self.restrictions: list[dict[str, set[str]]] = []
        self.suppressions: set[str] = set()
        self.guards: list[Callable[[ToolDefinition, dict[str, Any], Any], str | None]] = []
        self.mode: ToolPresentationMode | None = None

    def insert(self, tool_def: ToolDefinition) -> None:
        self.tools[tool_def.name] = tool_def
        for alias in tool_def.aliases:
            self.aliases[alias] = tool_def.name

    def remove(self, name: str) -> bool:
        canonical = self.aliases.get(name, name)
        removed = self.tools.pop(canonical, None)
        if removed:
            # Clean up aliases
            self.aliases = {k: v for k, v in self.aliases.items() if v != canonical}
            self.suppressions.discard(canonical)
            return True
        return False

    def suppress(self, name: str) -> None:
        canonical = self.aliases.get(name, name)
        self.suppressions.add(canonical)

    def restore(self, name: str) -> bool:
        canonical = self.aliases.get(name, name)
        if canonical in self.suppressions:
            self.suppressions.remove(canonical)
            return True
        return False

    def is_suppressed(self, name: str) -> bool:
        canonical = self.aliases.get(name, name)
        return canonical in self.suppressions

    def admits(self, name: str) -> bool:
        """Check if a tool name passes all compiled restrictions in this layer."""
        if self.is_suppressed(name):
            return False
        for r in self.restrictions:
            allow_set = r.get("allow")
            deny_set = r.get("deny")
            if allow_set is not None and name not in allow_set:
                return False
            if deny_set is not None and name in deny_set:
                return False
        return True


class ToolRegistry:
    """Type-safe registry for built-in and dynamic agent tools with scoping, restrictions, and validation."""

    def __init__(self) -> None:
        self._global_layer = ToolLayer(scope=None)
        self._scoped_layers: dict[str, ToolLayer] = {}
        self._change_listeners: list[Callable[[], None]] = []
        self.default_mode: ToolPresentationMode = "native"
        self._register_builtins()

    def on_change(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe a listener to tool registration/restriction changes."""
        self._change_listeners.append(listener)

        def disposer() -> None:
            if listener in self._change_listeners:
                self._change_listeners.remove(listener)

        return disposer

    def _emit_change(self) -> None:
        for listener in list(self._change_listeners):
            try:
                listener()
            except Exception:
                pass

    def _get_layer(self, scope: str | None, create: bool = False) -> ToolLayer:
        if scope is None:
            return self._global_layer
        if scope not in self._scoped_layers:
            if create:
                self._scoped_layers[scope] = ToolLayer(scope=scope)
            else:
                return ToolLayer(scope=scope)
        return self._scoped_layers[scope]

    def register(self, tool_def: ToolDefinition, scope: str | None = None) -> Callable[[], None]:
        """Register a tool definition and return an unregister disposer function."""
        layer = self._get_layer(scope, create=True)
        layer.insert(tool_def)
        self._emit_change()

        def unregister_disposer() -> None:
            self.unregister(tool_def.name, scope=scope)

        return unregister_disposer

    def unregister(self, name: str, scope: str | None = None) -> bool:
        """Unregister a tool by name or alias from the specified scope or global layer."""
        layer = self._get_layer(scope, create=False)
        removed = layer.remove(name)
        if removed:
            self._emit_change()
        return removed

    def suppress_tool(self, name: str, scope: str | None = None) -> Callable[[], None]:
        """Temporarily suppress a tool in the specified scope (or globally)."""
        layer = self._get_layer(scope, create=True)
        layer.suppress(name)
        self._emit_change()

        def disposer() -> None:
            self.restore_tool(name, scope=scope)

        return disposer

    def restore_tool(self, name: str, scope: str | None = None) -> bool:
        """Restore a previously suppressed tool in the specified scope (or globally)."""
        layer = self._get_layer(scope, create=False)
        restored = layer.restore(name)
        if restored:
            self._emit_change()
        return restored

    def is_tool_suppressed(self, name: str, scope: str | None = None) -> bool:
        """Check if a tool is suppressed in the given scope or globally."""
        if scope and scope in self._scoped_layers:
            if self._scoped_layers[scope].is_suppressed(name):
                return True
        return self._global_layer.is_suppressed(name)

    def restrict(
        self,
        filter_spec: dict[str, Sequence[str]],
        scope: str | None = None,
    ) -> Callable[[], None]:
        """Restrict visible tools for a scope (e.g. allow only read tools for a read_only subagent)."""
        layer = self._get_layer(scope, create=True)
        compiled: dict[str, set[str]] = {}
        if "allow" in filter_spec:
            compiled["allow"] = set(filter_spec["allow"])
        if "deny" in filter_spec:
            compiled["deny"] = set(filter_spec["deny"])

        layer.restrictions.append(compiled)
        self._emit_change()

        def disposer() -> None:
            if compiled in layer.restrictions:
                layer.restrictions.remove(compiled)
                self._emit_change()

        return disposer

    def set_session_mask(
        self,
        session_id: str,
        allow: Sequence[str] | None = None,
        deny: Sequence[str] | None = None,
    ) -> Callable[[], None]:
        """Convenience method to set an allow/deny tool mask for a session."""
        filter_spec: dict[str, Sequence[str]] = {}
        if allow is not None:
            filter_spec["allow"] = list(allow)
        if deny is not None:
            filter_spec["deny"] = list(deny)
        return self.restrict(filter_spec, scope=session_id)

    def clear_session_mask(self, session_id: str) -> None:
        """Clear all restrictions and suppressions from a session's scoped layer."""
        if session_id in self._scoped_layers:
            layer = self._scoped_layers[session_id]
            layer.restrictions.clear()
            layer.suppressions.clear()
            self._emit_change()

    def guard(
        self,
        guard_fn: Callable[[ToolDefinition, dict[str, Any], Any], str | None],
        scope: str | None = None,
    ) -> Callable[[], None]:
        """Register a monotonic execution guard for a scope or globally."""
        layer = self._get_layer(scope, create=True)
        layer.guards.append(guard_fn)

        def disposer() -> None:
            if guard_fn in layer.guards:
                layer.guards.remove(guard_fn)

        return disposer

    def get(self, name: str, scope: str | None = None) -> ToolDefinition | None:
        """Resolve a tool definition by name or alias, applying scoping and active restrictions."""
        # 1. Check scoped layer first
        if scope and scope in self._scoped_layers:
            scoped_layer = self._scoped_layers[scope]
            canonical = scoped_layer.aliases.get(name, name)
            if scoped_layer.is_suppressed(canonical):
                return None
            if canonical in scoped_layer.tools:
                if not scoped_layer.admits(canonical):
                    return None
                return scoped_layer.tools[canonical]

        # 2. Check global layer
        canonical = self._global_layer.aliases.get(name, name)
        if self._global_layer.is_suppressed(canonical):
            return None
        tool_def = self._global_layer.tools.get(canonical)
        if tool_def is None:
            return None

        # 3. Check restrictions on inherited tool
        if scope and scope in self._scoped_layers:
            if not self._scoped_layers[scope].admits(tool_def.name):
                return None

        return tool_def

    def has_tool(self, name: str, scope: str | None = None) -> bool:
        """Check if a tool exists and is permitted in the given scope."""
        return self.get(name, scope=scope) is not None

    def list_tools(self, scope: str | None = None) -> list[ToolDefinition]:
        """Return all registered and permitted tool definitions for the given scope."""
        tools_map: dict[str, ToolDefinition] = {}

        # 1. Global tools that pass scope restrictions and aren't suppressed
        for name, tool_def in self._global_layer.tools.items():
            if self._global_layer.is_suppressed(name):
                continue
            if scope and scope in self._scoped_layers:
                if not self._scoped_layers[scope].admits(name):
                    continue
            tools_map[name] = tool_def

        # 2. Scoped tool additions / overrides
        if scope and scope in self._scoped_layers:
            for name, tool_def in self._scoped_layers[scope].tools.items():
                if not self._scoped_layers[scope].admits(name):
                    continue
                tools_map[name] = tool_def

        return list(tools_map.values())

    def get_openai_tool_definitions(
        self,
        options: dict[str, Any] | None = None,
        external_tools: list[dict[str, Any]] | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generate OpenAI function definitions for all registered and visible tools."""
        options = options or {}
        definitions: list[dict[str, Any]] = []

        for tool in self.list_tools(scope=scope):
            if options.get("nonInteractive") is True and tool.name in (
                "AskUserQuestion",
                "ask_user_question",
            ):
                continue
            definitions.append(tool.to_openai_schema())

        for ext in external_tools or []:
            definitions.append(ext)

        return definitions

    def validate_arguments(
        self,
        name: str,
        args: dict[str, Any],
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Strictly validate input arguments against the tool's parameter schema.

        Raises:
            ValidationError: If required fields are missing, types mismatch, or constraints are violated.
        """
        tool_def = self.get(name, scope=scope)
        if not tool_def:
            raise ValidationError(
                f"Tool '{name}' is not registered or is restricted in the ToolRegistry."
            )

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
                if expected_type == "integer" and int(value) != value:
                    raise ValidationError(
                        f"Argument '{param_name}' for tool '{name}' must be an integer, got {value}."
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

    def to_openai_schemas(
        self,
        scope: str | None = None,
        options: dict[str, Any] | None = None,
        external_tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Project registered tools onto formatted OpenAI Function tool call schemas."""
        options = options or {}
        from coderai.core.prompt_sections import order_tools
        from coderai.core.common.model_capabilities import supports_multimodal

        tools_list: list[dict[str, Any]] = []
        is_non_interactive = options.get("nonInteractive") is True
        is_child_agent = options.get("childAgent") is True
        model = str(options.get("model", ""))
        multimodal_mode = str(options.get("multimodal", "default"))
        model_supports_vision = supports_multimodal(model, multimodal_mode)

        preset = options.get("preset") or options.get("toolsPreset") or options.get("tools_preset")

        for tool_def in self.list_tools(scope=scope):
            name = tool_def.name
            # Filter non-interactive tools
            if is_non_interactive and name in ("AskUserQuestion", "ask_user_question"):
                continue
            # Filter child agent specific tools
            if not is_child_agent and name == "report":
                continue
            # Filter multimodal tool if model has native vision
            if model_supports_vision and name in ("UnderstandImage", "understand_image"):
                continue

            schema = tool_def.to_openai_schema()
            tools_list.append(schema)

        if external_tools:
            tools_list.extend(canonicalize_tool_schema(external_tools))

        if preset in ("minimal", "benchmark", "coding", "dsh_minimal"):
            core_names = {"bash", "str_replace_editor", "edit", "read", "write", "glob", "grep"}
            if preset == "dsh_minimal":
                core_names = {"bash", "str_replace_editor"}
            tools_list = [
                t for t in tools_list if (t.get("function") or {}).get("name") in core_names
            ]

        ordered = order_tools(tools_list)
        return [canonicalize_tool_schema(t) for t in ordered]

    def to_sdk_schemas(
        self, scope: str | None = None, language: str = "python"
    ) -> list[dict[str, Any]]:
        """Project registered tools onto Code Mode SDK signatures."""
        del language
        schemas: list[dict[str, Any]] = []
        for tool in self.list_tools(scope=scope):
            if tool.name in ("code_mode", "run_code"):
                continue
            item = copy.deepcopy(tool.to_openai_schema())
            if tool.output:
                item["output"] = copy.deepcopy(tool.output.schema)
            schemas.append(item)
        return schemas

    def _register_builtins(self) -> None:
        """Register the core standard built-in tools with DeepSeek Harness parity."""
        # 1. bash & pwsh
        self.register(
            define_tool(
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
                    },
                    "run_in_background": {"type": "boolean"},
                    "persistent": {
                        "type": "boolean",
                        "description": "Run inside a persistent PTY bash shell retaining variables and working directory across calls.",
                    },
                    "timeout_ms": {
                        "type": "number",
                        "description": "Command execution timeout in milliseconds.",
                    },
                    "sandbox_permissions": {
                        "type": "string",
                        "description": "Escalated sandbox permissions mode if required.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Justification for requested sandbox escalation.",
                    },
                },
                required=["command", "sideEffects"],
                handler=_bash.handle_bash_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )

        self.register(
            define_tool(
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
                    },
                    "run_in_background": {"type": "boolean"},
                },
                required=["command", "sideEffects"],
                handler=_pwsh.handle_pwsh_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )

        # 2. Background jobs
        self.register(
            define_tool(
                name="job_list",
                aliases=["JobList"],
                description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
                parameters={},
                required=[],
                handler=_jobs.handle_job_list_tool,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
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
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
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
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )

        # 3. Filesystem Discovery (glob / grep)
        self.register(
            define_tool(
                name="glob",
                aliases=["Glob", "glob_search"],
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
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="grep",
                aliases=["Grep", "grep_search"],
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
                is_concurrency_safe=True,
            )
        )

        # 4. Filesystem Core (read, write, edit, str_replace_editor)
        self.register(
            define_tool(
                name="read",
                aliases=["Read"],
                description="Read a text file, notebook, image, or directory listing with line numbering and observation tracking.",
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative path to read.",
                    },
                    "offset": {
                        "type": "number",
                        "description": "1-based starting line number to read from.",
                    },
                    "limit": {
                        "type": "number",
                        "description": "Maximum number of lines to return (default: 2000).",
                    },
                },
                required=["file_path"],
                handler=_read.handle_read_tool,
                category="filesystem",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="write",
                aliases=["Write"],
                description="Create or completely overwrite a UTF-8 text file.",
                parameters={
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file to create or overwrite.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The exact full text content to write to the file.",
                    },
                },
                required=["file_path", "content"],
                handler=_write.handle_write_tool,
                category="filesystem",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="edit",
                aliases=["Edit"],
                description="Edit an existing UTF-8 text file with snippet-scoped or path replacement.",
                parameters={
                    "snippet_id": {
                        "type": "string",
                        "description": "Snippet ID returned from a prior read call for scoped editing.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file being edited.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact literal text to replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Exact literal replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences when true.",
                    },
                    "expected_occurrences": {
                        "type": "number",
                        "description": "Expected number of occurrences to replace.",
                    },
                },
                required=["old_string", "new_string"],
                handler=_edit.handle_edit_tool,
                category="filesystem",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="str_replace_editor",
                aliases=["StrReplaceEditor"],
                description="Custom editing tool for viewing, creating, str_replace, insert, and undo commands on files.",
                parameters={
                    "command": {
                        "type": "string",
                        "enum": ["view", "create", "str_replace", "insert", "undo_edit", "undo_command"],
                        "description": "The editing command to execute.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the target file or directory.",
                    },
                    "file_text": {
                        "type": "string",
                        "description": "Required for `create` command: initial file content.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Required for `str_replace`: unique text to replace.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text for `str_replace` or `insert`.",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "Required for `insert`: 0-based or 1-based line number after which to insert.",
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [start_line, end_line] for `view` command.",
                    },
                },
                required=["command", "path"],
                handler=_str_replace.handle_str_replace_editor_tool,
                category="filesystem",
                is_mutating=True,
                is_concurrency_safe=lambda args: args.get("command") == "view",
            )
        )

        # 5. Interactive & Questions
        self.register(
            define_tool(
                name="AskUserQuestion",
                aliases=["ask_user_question", "ask_user"],
                description="Prompt the user with structured questions, choices, or clarifications.",
                parameters={
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
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
                                "multiSelect": {"type": "boolean"},
                            },
                            "required": ["question", "options"],
                        },
                        "description": "List of structured questions to present to the user.",
                    }
                },
                required=["questions"],
                handler=_ask.handle_ask_user_question_tool,
                category="interactive",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )

        # 6. Web & Network
        self.register(
            define_tool(
                name="WebSearch",
                aliases=["web_search"],
                description="Search the web for up-to-date documentation, issues, and references.",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Search query string.",
                    }
                },
                required=["query"],
                handler=_search.handle_web_search_tool,
                category="web",
                rate_limited_id="WebSearch",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="WebFetch",
                aliases=["web_fetch"],
                description="Fetch and extract readable Markdown content from a public URL.",
                parameters={
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch and convert to Markdown.",
                    }
                },
                required=["url"],
                handler=_fetch.handle_web_fetch_tool,
                category="web",
                rate_limited_id="WebFetch",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )

        # 7. Subagents & Delegation
        self.register(
            define_tool(
                name="Task",
                aliases=["task"],
                description="Spawn an isolated sub-agent session for complex, modular, or exploratory tasks.",
                parameters={
                    "description": {
                        "type": "string",
                        "description": "Short 3-5 word summary of the task.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Detailed instructions and context for the sub-agent.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["read_only", "general"],
                        "description": "Execution permissions mode for the sub-agent (default: 'read_only').",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Maximum execution time in seconds (default: 90).",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional additional context or code snippets.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Current subagent nesting depth.",
                    },
                },
                required=["description", "prompt"],
                handler=_subagent.handle_subagent_tool,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="subagent",
                aliases=["SubAgent"],
                description="Start a continuable sub-agent in the background and return an agent id. Use send_message / list_agents / interrupt_agent to steer it. For a one-shot child, use Task or subagent_fork.",
                parameters={
                    "description": {
                        "type": "string",
                        "description": "Short 3-5 word summary of the sub-task.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Detailed instructions for the sub-agent.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["read_only", "general"],
                        "description": "Execution permissions mode for the sub-agent (default: 'read_only').",
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
                handler=_agents.handle_continuable_subagent_tool,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="subagent_fork",
                aliases=["SubagentFork"],
                description="Spawn a one-shot sub-agent and wait for its aggregated findings (alias of Task).",
                parameters={
                    "description": {
                        "type": "string",
                        "description": "Short 3-5 word summary of the sub-task.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Detailed instructions for the sub-agent.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["read_only", "general"],
                        "description": "Execution permissions mode for the sub-agent (default: 'read_only').",
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
                handler=_agents.handle_subagent_fork_tool,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="send_message",
                aliases=["SendMessage"],
                description="Send a follow-up message to a running or parked sub-agent by agent id.",
                parameters={
                    "agent_id": {
                        "type": "string",
                        "description": "Target agent id.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Follow-up message or instructions.",
                    },
                },
                required=["agent_id", "message"],
                handler=_agents.handle_send_message_tool,
                category="subagent",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="interrupt_agent",
                aliases=["InterruptAgent"],
                description="Cancel a running sub-agent by agent id.",
                parameters={
                    "agent_id": {
                        "type": "string",
                        "description": "Target agent id to cancel.",
                    }
                },
                required=["agent_id"],
                handler=_agents.handle_interrupt_agent_tool,
                category="subagent",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="list_agents",
                aliases=["ListAgents"],
                description="List sub-agents spawned from this session with ids and statuses.",
                parameters={},
                required=[],
                handler=_agents.handle_list_agents_tool,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="report",
                aliases=["Report"],
                description="Child-only: submit the final report for the parent agent and finish this sub-agent.",
                parameters={
                    "summary": {
                        "type": "string",
                        "description": "Final report summary for parent.",
                    }
                },
                required=["summary"],
                handler=_agents.handle_report_tool,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )

        # 8. Interactive Terminal PTY Sessions
        self.register(
            define_tool(
                name="terminal_open",
                aliases=["TerminalOpen"],
                description="Open a persistent interactive terminal (PTY) session.",
                parameters={
                    "type": {"type": "string", "enum": ["bash", "sh", "zsh", "pwsh"]},
                    "name": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                required=[],
                handler=_terminal.handle_terminal_open_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="terminal_send",
                aliases=["TerminalSend"],
                description="Send input text to an active interactive terminal session.",
                parameters={
                    "sessionId": {"type": "string"},
                    "text": {"type": "string"},
                    "submit": {"type": "boolean"},
                    "run_in_background": {"type": "boolean"},
                    "timeout_ms": {"type": "number"},
                },
                required=["sessionId", "text"],
                handler=_terminal.handle_terminal_send_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="terminal_read",
                aliases=["TerminalRead"],
                description="Read pending output from an active interactive terminal session.",
                parameters={
                    "sessionId": {"type": "string"},
                    "timeout_ms": {"type": "number"},
                },
                required=["sessionId"],
                handler=_terminal.handle_terminal_read_tool,
                category="shell",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="terminal_signal",
                aliases=["TerminalSignal"],
                description="Send a POSIX signal (e.g. SIGINT, SIGTERM, SIGKILL) to an active terminal.",
                parameters={
                    "sessionId": {"type": "string"},
                    "signal": {
                        "type": "string",
                        "enum": ["SIGINT", "SIGTERM", "SIGKILL", "SIGHUP"],
                    },
                },
                required=["sessionId"],
                handler=_terminal.handle_terminal_signal_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="terminal_close",
                aliases=["TerminalClose"],
                description="Close and terminate an active persistent terminal session.",
                parameters={"sessionId": {"type": "string"}},
                required=["sessionId"],
                handler=_terminal.handle_terminal_close_tool,
                category="shell",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="terminal_list",
                aliases=["TerminalList"],
                description="List all active persistent interactive terminal sessions.",
                parameters={},
                required=[],
                handler=_terminal.handle_terminal_list_tool,
                category="shell",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )

        # 9. Language Server Protocol (LSP)
        self.register(
            define_tool(
                name="lsp",
                aliases=["Lsp"],
                description="Query Language Server Protocol features: definitions, references, hover docs, document symbols.",
                parameters={
                    "operation": {
                        "type": "string",
                        "enum": ["goToDefinition", "findReferences", "hover", "documentSymbol"],
                        "description": "The LSP query operation to perform.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to the target source file.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "1-based line number for position queries.",
                    },
                    "character": {
                        "type": "integer",
                        "description": "1-based character column number for position queries.",
                    },
                },
                required=["operation", "file_path"],
                handler=_lsp.handle_lsp_tool,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )

        # 10. Skills
        self.register(
            define_tool(
                name="skill",
                aliases=["Skill"],
                description="Load full instructions and examples for a specialized skill into active context.",
                parameters={
                    "name": {
                        "type": "string",
                        "description": "Name of the skill to load (e.g. 'accidental-data-loss-prevention').",
                    }
                },
                required=["name"],
                handler=_skill.handle_skill_tool,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )

        # 11. Image understanding
        self.register(
            define_tool(
                name="UnderstandImage",
                aliases=["understand_image"],
                description="Analyze and extract visual insights from a local image file.",
                parameters={
                    "image_path": {
                        "type": "string",
                        "description": "Path to the image file.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Question or prompt regarding the image contents.",
                    },
                },
                required=["image_path"],
                handler=_image.handle_understand_image_tool,
                category="meta",
                rate_limited_id="UnderstandImage",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )

        # 12. Plan & Todo tools
        self.register(
            define_tool(
                name="UpdatePlan",
                aliases=["update_plan"],
                description="Update the task plan and milestones.",
                parameters={
                    "plan": {
                        "type": "string",
                        "description": "The updated markdown plan.",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Brief explanation of plan changes.",
                    },
                },
                required=["plan"],
                handler=_plan.handle_update_plan_tool,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="todo_write",
                aliases=["TodoWrite"],
                description="Update the structured todo checklist for this session.",
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
                            "required": ["content", "status"],
                        },
                    },
                    "merge": {"type": "boolean"},
                },
                required=["todos"],
                handler=_todo.handle_todo_write_tool,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="exit_plan_mode",
                aliases=["ExitPlanMode"],
                description="Leave Plan Mode while mutation tools stay active.",
                parameters={
                    "summary": {
                        "type": "string",
                        "description": "Summary of plan conclusions before exiting plan mode.",
                    }
                },
                required=[],
                handler=_plan_mode.handle_exit_plan_mode_tool,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )

        # 13. Schedule
        self.register(
            define_tool(
                name="schedule_create",
                aliases=["ScheduleCreate"],
                description="Schedule a reminder or background instruction (one-shot or cron).",
                parameters={
                    "prompt": {
                        "type": "string",
                        "description": "The instruction prompt to execute when triggered.",
                    },
                    "after_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for one-shot timer.",
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": "Cron expression for recurring schedule.",
                    },
                },
                required=["prompt"],
                handler=_schedule.handle_schedule_create_tool,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="schedule_list",
                aliases=["ScheduleList"],
                description="List all scheduled timers and cron jobs.",
                parameters={},
                required=[],
                handler=_schedule.handle_schedule_list_tool,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="schedule_delete",
                aliases=["ScheduleDelete"],
                description="Delete an active timer or cron schedule by ID.",
                parameters={"schedule_id": {"type": "string"}},
                required=["schedule_id"],
                handler=_schedule.handle_schedule_delete_tool,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )

        # 14. Ralph / Workflow / Goal
        self.register(
            define_tool(
                name="ralph",
                aliases=["Ralph", "workflow_run"],
                description="Run an automated task loop with verification until completion criteria are met.",
                parameters={
                    "objective": {"type": "string", "description": "Immutable verification objective instructions."},
                    "prompt": {"type": "string", "description": "Alias for objective."},
                    "max_rounds": {
                        "type": "integer",
                        "description": "Maximum verification rounds (default: 5).",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Alias for max_rounds.",
                    },
                    "timeout_per_round": {
                        "type": "number",
                        "description": "Timeout in seconds per round (default: 90s).",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional initial context or requirements for round 1.",
                    },
                },
                required=[],
                handler=_ralph.handle_ralph_tool,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="workflow",
                aliases=["Workflow"],
                description="Execute declarative multi-step workflows with dependencies and verification.",
                parameters={
                    "workflow": {"type": "object", "description": "Workflow definition schema."},
                    "action": {"type": "string", "enum": ["run", "status", "cancel", "list"]},
                    "workflow_id": {"type": "string"},
                },
                required=[],
                handler=_workflow_handle,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="goal",
                aliases=["Goal"],
                description="Declare a high-level overnight or long-running goal with progress milestones.",
                parameters={
                    "title": {"type": "string", "description": "Goal title."},
                    "description": {
                        "type": "string",
                        "description": "Detailed goal specification.",
                    },
                    "milestones": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key milestone checkpoints.",
                    },
                },
                required=["title", "description"],
                handler=_goal_handle,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )

        # 15. Team & Multi-Agent Coordination
        self.register(
            define_tool(
                name="spawn_teammate",
                aliases=["team_spawn", "TeamSpawn", "SpawnTeammate"],
                description="Spawn a specialized teammate agent for concurrent collaboration.",
                parameters={
                    "name": {"type": "string", "description": "Teammate identifier/name."},
                    "role": {"type": "string", "description": "Role/persona for the teammate."},
                    "prompt": {"type": "string", "description": "Task instructions for teammate."},
                },
                required=["name", "role", "prompt"],
                handler=_spawn_teammate_handle,
                category="subagent",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="team_task_create",
                aliases=["TeamTaskCreate"],
                description="Create a task on the shared team task board.",
                parameters={
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "assigned_to": {"type": "string"},
                },
                required=["title"],
                handler=_task_create_handle,
                category="subagent",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="team_task_get",
                aliases=["TeamTaskGet"],
                description="Retrieve details of a task from the shared team task board.",
                parameters={"task_id": {"type": "string"}},
                required=["task_id"],
                handler=_task_get_handle,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="team_task_list",
                aliases=["TeamTaskList"],
                description="List tasks on the shared team task board.",
                parameters={
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                    },
                    "assigned_to": {"type": "string"},
                },
                required=[],
                handler=_task_list_handle,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )
        self.register(
            define_tool(
                name="team_task_update",
                aliases=["TeamTaskUpdate"],
                description="Update status or assignee for a task on the shared team task board.",
                parameters={
                    "task_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                    },
                    "assigned_to": {"type": "string"},
                    "result": {"type": "string"},
                    "notes": {"type": "string"},
                },
                required=["task_id"],
                handler=_task_update_handle,
                category="subagent",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="wait_agent",
                aliases=["WaitAgent"],
                description="Wait for completion or message settlement from spawned teammates or subagents.",
                parameters={
                    "agent_id": {"type": "string"},
                    "agent_ids": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "number"},
                },
                required=[],
                handler=_wait_agent_handle,
                category="subagent",
                is_mutating=False,
                is_concurrency_safe=False,
            )
        )

        # 16. Code Mode & Session Query
        self.register(
            define_tool(
                name="code_mode",
                aliases=["CodeMode", "python_exec"],
                description="Execute Python code in a stateful sandbox with workspace tool helpers.",
                parameters={
                    "code": {
                        "type": "string",
                        "description": "The Python code snippet to execute.",
                    },
                    "reset_state": {"type": "boolean"},
                    "timeout_seconds": {"type": "number"},
                },
                required=["code"],
                handler=_code_mode_handle,
                category="meta",
                is_mutating=True,
                is_concurrency_safe=False,
            )
        )
        self.register(
            define_tool(
                name="session_query",
                aliases=["SessionQuery", "session_search", "SessionSearch"],
                description="Search historical conversation turns and tool outputs using full-text search.",
                parameters={
                    "query": {
                        "type": "string",
                        "description": "Natural language or keyword query.",
                    },
                    "session_id": {"type": "string"},
                    "role": {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                    "limit": {"type": "integer"},
                },
                required=["query"],
                handler=_session_query_handle,
                category="meta",
                is_mutating=False,
                is_concurrency_safe=True,
            )
        )


# Global default tool registry
_default_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _default_tool_registry

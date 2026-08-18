"""Modular, type-safe Tool Registry with strict JSON Schema and type validation."""

from __future__ import annotations

from typing import Any

from coderai.core.tools import ask_user_question as _ask
from coderai.core.tools import bash as _bash
from coderai.core.tools import edit as _edit
from coderai.core.tools import read as _read
from coderai.core.tools import subagent as _subagent
from coderai.core.tools import understand_image as _image
from coderai.core.tools import update_plan as _plan
from coderai.core.tools import web_fetch as _fetch
from coderai.core.tools import web_search as _search
from coderai.core.tools import write as _write
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

        # 2. read
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

        # 10. Task (Subagent)
        self.register(
            ToolDefinition(
                name="Task",
                aliases=["task", "subagent", "SubAgent"],
                description="Spawn an isolated sub-agent to execute a specific sub-task in an independent context and return aggregated findings.",
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


# Global default tool registry
_default_tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _default_tool_registry

"""Built-in tool definitions owned by this subsystem."""

from __future__ import annotations
from typing import Any
from coderai.tools.legacy.policy import builtin_effect_policy
from coderai.tools.file import replace as _edit
from coderai.tools.file import read as _read
from coderai.tools.file.glob import glob_tool_definition as _glob_tool_definition
from coderai.tools.file.grep import grep_tool_definition as _grep_tool_definition
from coderai.tools.file import replace as _str_replace
from coderai.tools.file import read_media as _image
from coderai.tools.file import write as _write
from coderai.tools.legacy.schema import define_tool


def register_tools(registry: Any) -> None:
    registry.register(_glob_tool_definition())

    registry.register(_grep_tool_definition())

    registry.register(
        define_tool(
            effects=builtin_effect_policy("read", False),
            name="read",
            description="Read a text file, notebook, image, or directory listing with line numbering and observation tracking.",
            parameters={
                "file_path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based starting line number to read from.",
                },
                "limit": {
                    "type": "integer",
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

    registry.register(
        define_tool(
            effects=builtin_effect_policy("write", True),
            name="write",
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
            execution_mode="parallel",
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("edit", True),
            name="edit",
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
            execution_mode="parallel",
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("str_replace_editor", True),
            name="str_replace_editor",
            description="Custom editing tool for viewing, creating, str_replace, insert, and undo commands on files.",
            parameters={
                "command": {
                    "type": "string",
                    "enum": [
                        "view",
                        "create",
                        "str_replace",
                        "insert",
                        "undo_edit",
                        "undo_command",
                    ],
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
            execution_mode="parallel",
        )
    )

    registry.register(
        define_tool(
            effects=builtin_effect_policy("UnderstandImage", False),
            name="UnderstandImage",
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

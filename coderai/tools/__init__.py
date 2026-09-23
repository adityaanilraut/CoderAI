from __future__ import annotations

import json
import os
from typing import Any, cast

try:
    import streamingjson
except ImportError:  # optional display-time dependency
    streamingjson = None  # type: ignore[assignment]

try:
    from kaos.path import KaosPath
except ImportError:  # optional display-time dependency
    KaosPath = None  # type: ignore[assignment]

try:
    from kosong.utils.typing import JsonType
except ImportError:  # optional display-time dependency
    JsonType = Any  # type: ignore[assignment]

try:
    from coderai.utils.string import shorten_middle
except ImportError:  # minimal fallback when utils are unavailable

    def shorten_middle(text: str, width: int = 50) -> str:
        text = str(text)
        if len(text) <= width or width <= 3:
            return text
        half = (width - 3) // 2
        return text[:half] + "..." + text[-half:]


class SkipThisTool(Exception):
    """Raised when a tool decides to skip itself from the loading process."""

    pass


def extract_key_argument(json_content: Any, tool_name: str) -> str | None:
    _lexer_type = getattr(streamingjson, "Lexer", None) if streamingjson else None
    if _lexer_type is not None and isinstance(json_content, _lexer_type):
        json_str = json_content.complete_json()
    elif isinstance(json_content, str):
        json_str = json_content
    else:
        # Non-string input (e.g. int/None): no key argument instead of a crash.
        return None
    try:
        curr_args: JsonType = json.loads(json_str, strict=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not curr_args:
        return None
    key_argument: str = ""
    match tool_name:
        case "Agent" | "Task" | "subagent" | "subagent_fork":
            if not isinstance(curr_args, dict) or not curr_args.get("description"):
                return None
            key_argument = str(curr_args["description"])
        case "SendDMail":
            return None
        case "Think":
            if not isinstance(curr_args, dict) or not curr_args.get("thought"):
                return None
            key_argument = str(curr_args["thought"])
        case "SetTodoList":
            return None
        case "Shell" | "bash" | "pwsh" | "terminal_send":
            if not isinstance(curr_args, dict) or not curr_args.get("command"):
                return None
            key_argument = str(curr_args["command"])
        case "TaskOutput" | "job_output" | "job_kill":
            if not isinstance(curr_args, dict):
                return None
            val = curr_args.get("job_id") or curr_args.get("task_id")
            if not val:
                return None
            key_argument = str(val)
        case "TaskList" | "job_list":
            if not isinstance(curr_args, dict):
                return None
            key_argument = "active" if curr_args.get("active_only", True) else "all"
        case "TaskStop":
            if not isinstance(curr_args, dict) or not curr_args.get("task_id"):
                return None
            key_argument = str(curr_args["task_id"])
        case "ReadFile" | "read" | "WriteFile" | "write" | "edit":
            if not isinstance(curr_args, dict):
                return None
            val = curr_args.get("file_path") or curr_args.get("path")
            if not val:
                return None
            key_argument = _normalize_path(str(val))
        case "ReadMediaFile" | "UnderstandImage":
            if not isinstance(curr_args, dict):
                return None
            val = curr_args.get("file_path") or curr_args.get("path") or curr_args.get("image_path")
            if not val:
                return None
            key_argument = _normalize_path(str(val))
        case "StrReplaceFile" | "str_replace_editor":
            if not isinstance(curr_args, dict):
                return None
            val = curr_args.get("path") or curr_args.get("file_path")
            if not val:
                return None
            key_argument = _normalize_path(str(val))
        case "Glob" | "glob" | "Grep" | "grep":
            if not isinstance(curr_args, dict) or not curr_args.get("pattern"):
                return None
            key_argument = str(curr_args["pattern"])
        case "SearchWeb" | "WebSearch":
            if not isinstance(curr_args, dict) or not curr_args.get("query"):
                return None
            key_argument = str(curr_args["query"])
        case "FetchURL" | "WebFetch":
            if not isinstance(curr_args, dict) or not curr_args.get("url"):
                return None
            key_argument = str(curr_args["url"])
        case _:
            if _lexer_type is not None and isinstance(json_content, _lexer_type):
                content: list[str] = cast(list[str], json_content.json_content)
                key_argument = "".join(content)
            elif isinstance(json_content, str):
                key_argument = json_content
            else:
                return None
    key_argument = shorten_middle(key_argument, width=50)
    return key_argument


def _normalize_path(path: str) -> str:
    try:
        cwd = str(KaosPath.cwd().canonical()) if KaosPath is not None else os.getcwd()
    except Exception:
        cwd = os.getcwd()
    if path.startswith(cwd):
        path = path[len(cwd) :].lstrip("/\\")
    return path

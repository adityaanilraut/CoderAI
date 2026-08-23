"""str_replace_editor tool — Anthropic-style custom file editor (view, create, str_replace, insert, undo_edit)."""

from __future__ import annotations

import os
import pathlib
import time
from typing import Any

from coderai.core.common.file_utils import (
    build_diff_preview,
    ensure_parent_directory,
    read_text_file_with_metadata,
    write_text_file,
)
from coderai.core.common.string_matcher import match_multistage
from coderai.core.state import (
    FileState,
    normalize_file_path,
    record_file_state,
)
from coderai.core.tools.types import ToolResult, as_str

DEFAULT_MAX_OUTPUT_CHARS = 32_000
TRUNCATED_MESSAGE = "\n<response clipped>"

# History stack for undo_edit: path -> list of previous file contents
_UNDO_HISTORY: dict[str, list[str]] = {}


def _record_state(session_id: str, file_path: str, content: str) -> None:
    try:
        record_file_state(
            session_id,
            FileState(
                file_path=file_path,
                content=content,
                timestamp=int(time.time() * 1000),
            ),
            increment_version=True,
        )
    except Exception:
        pass


def _push_undo(path: str, content: str) -> None:
    norm = normalize_file_path(path)
    if norm not in _UNDO_HISTORY:
        _UNDO_HISTORY[norm] = []
    _UNDO_HISTORY[norm].append(content)
    # Keep last 20 revisions
    if len(_UNDO_HISTORY[norm]) > 20:
        _UNDO_HISTORY[norm].pop(0)


def _pop_undo(path: str) -> str | None:
    norm = normalize_file_path(path)
    if norm in _UNDO_HISTORY and _UNDO_HISTORY[norm]:
        return _UNDO_HISTORY[norm].pop()
    return None


def _resolve_path(path_str: str, project_root: str) -> str:
    p = pathlib.Path(path_str)
    if not p.is_absolute():
        p = pathlib.Path(project_root) / p
    return str(p.resolve())


def _format_lines(content: str, start_line: int = 1, end_line: int = -1) -> str:
    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return "1\t"

    if start_line < 1:
        start_line = 1
    if end_line == -1 or end_line > total_lines:
        end_line = total_lines

    formatted = []
    for idx in range(start_line - 1, end_line):
        if 0 <= idx < total_lines:
            line_num = idx + 1
            formatted.append(f"{line_num:6d}\t{lines[idx]}")
    return "\n".join(formatted)


def _find_line_numbers(content: str, search: str) -> list[int]:
    offsets: list[int] = []
    pos = 0
    while True:
        idx = content.find(search, pos)
        if idx == -1:
            break
        offsets.append(idx)
        pos = idx + len(search)
        if not search:
            break

    line_nums: list[int] = []
    for off in offsets:
        line_num = content[:off].count("\n") + 1
        line_nums.append(line_num)
    return line_nums


def handle_str_replace_editor_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Execute str_replace_editor command."""
    command = as_str(args.get("command", "")).strip()
    path_arg = as_str(args.get("path", "")).strip()

    if not command:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Missing required parameter `command`.",
        )

    if not path_arg:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Missing required parameter `path`.",
        )

    project_root = getattr(context, "project_root", ".") if context else "."
    target_path = _resolve_path(path_arg, project_root)

    if command == "view":
        return _handle_view(target_path, args.get("view_range"), project_root, context)
    elif command == "create":
        return _handle_create(target_path, args.get("file_text"), context)
    elif command == "str_replace":
        return _handle_str_replace(target_path, args.get("old_str"), args.get("new_str"), context)
    elif command == "insert":
        return _handle_insert(target_path, args.get("insert_line"), args.get("new_str"), context)
    elif command in ("undo_edit", "undo_command"):
        return _handle_undo(target_path, context)
    else:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Unrecognized command `{command}`. Allowed commands are: `view`, `create`, `str_replace`, `insert`, `undo_edit` (or `undo_command`).",
        )


def _handle_view(
    target_path: str, view_range: Any, project_root: str, context: Any = None
) -> ToolResult:
    p = pathlib.Path(target_path)
    if not p.exists():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"The path `{target_path}` does not exist.",
        )

    if p.is_dir():
        # Directory view
        try:
            entries = sorted(os.listdir(target_path))
            listing = "\n".join(f"- {e}" for e in entries[:200])
            if len(entries) > 200:
                listing += f"\n... and {len(entries) - 200} more items"
            return ToolResult(
                ok=True,
                name="str_replace_editor",
                output=f"Directory listing for `{target_path}`:\n{listing}",
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                name="str_replace_editor",
                error=f"Failed to list directory `{target_path}`: {exc}",
            )

    # File view
    try:
        meta = read_text_file_with_metadata(target_path)
        content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    from coderai.core.tools.observation import get_observation_tracker

    get_observation_tracker().record_observation(session_id, target_path, content=content)

    start_line = 1
    end_line = -1
    if isinstance(view_range, list) and len(view_range) >= 2:
        try:
            start_line = int(view_range[0])
            end_line = int(view_range[1])
        except (ValueError, TypeError):
            pass

    formatted = _format_lines(content, start_line, end_line)
    if len(formatted) > DEFAULT_MAX_OUTPUT_CHARS:
        formatted = formatted[:DEFAULT_MAX_OUTPUT_CHARS] + TRUNCATED_MESSAGE

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"Here's the result of running `cat -n` on {target_path}:\n{formatted}",
    )


def _handle_create(target_path: str, file_text: Any, context: Any) -> ToolResult:
    if file_text is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `file_text` is required for command `create`.",
        )

    text = str(file_text)
    p = pathlib.Path(target_path)
    if p.exists():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File already exists at `{target_path}`. Cannot use `create` on existing files. Use `str_replace` or `insert`.",
        )

    sandbox_mode = getattr(context, "sandbox_mode", None)
    project_root = getattr(context, "project_root", None)
    if isinstance(context, dict):
        sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)
    from coderai.core.sandbox import check_sandbox_path_access

    sb_allowed, sb_err = check_sandbox_path_access(
        target_path, op="write", mode=sandbox_mode, workspace_root=project_root
    )
    if not sb_allowed and sb_err:
        return ToolResult(ok=False, name="str_replace_editor", error=sb_err)

    if context and hasattr(context, "on_before_file_mutation") and context.on_before_file_mutation:
        context.on_before_file_mutation(target_path)

    ensure_parent_directory(target_path)
    try:
        write_text_file(target_path, text)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    _record_state(session_id, target_path, text)

    if context and hasattr(context, "on_after_file_mutation") and context.on_after_file_mutation:
        context.on_after_file_mutation(target_path)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"File created successfully at: {target_path}",
    )


def _handle_str_replace(target_path: str, old_str: Any, new_str: Any, context: Any) -> ToolResult:
    if old_str is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `old_str` is required for command `str_replace`.",
        )

    old_s = str(old_str)
    new_s = str(new_str) if new_str is not None else ""

    p = pathlib.Path(target_path)
    if not p.is_file():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File does not exist at `{target_path}`.",
        )

    try:
        meta = read_text_file_with_metadata(target_path)
        current_content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    sandbox_mode = getattr(context, "sandbox_mode", None)
    project_root = getattr(context, "project_root", None)
    if isinstance(context, dict):
        sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)
    from coderai.core.sandbox import check_sandbox_path_access

    sb_allowed, sb_err = check_sandbox_path_access(
        target_path, op="write", mode=sandbox_mode, workspace_root=project_root
    )
    if not sb_allowed and sb_err:
        return ToolResult(ok=False, name="str_replace_editor", error=sb_err)

    from coderai.core.tools.observation import get_observation_tracker

    allowed, obs_err = get_observation_tracker().check_mutation_allowed(
        session_id, target_path, require_observed=True
    )
    if not allowed and obs_err:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=obs_err,
        )

    match_res = match_multistage(current_content, old_s, new_s)
    matches = match_res.matches

    if len(matches) == 0:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No replacement was performed, old_str did not appear in {target_path}.",
        )

    if len(matches) > 1:
        line_nums = [current_content[:s].count("\n") + 1 for s, _ in matches]
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No replacement was performed. Multiple occurrences of old_str in lines [{', '.join(map(str, line_nums))}]. Please include more surrounding context in `old_str` to make it unique.",
        )

    # Save for undo
    _push_undo(target_path, current_content)

    if context and hasattr(context, "on_before_file_mutation") and context.on_before_file_mutation:
        context.on_before_file_mutation(target_path)

    start_off, end_off = matches[0]
    new_content = current_content[:start_off] + match_res.replaced_new + current_content[end_off:]
    try:
        write_text_file(target_path, new_content)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    _record_state(session_id, target_path, new_content)
    get_observation_tracker().record_observation(session_id, target_path, content=new_content)

    if context and hasattr(context, "on_after_file_mutation") and context.on_after_file_mutation:
        context.on_after_file_mutation(target_path)

    diff = build_diff_preview(target_path, current_content, new_content)
    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"The file {target_path} has been edited successfully.\n{diff}",
    )


def _handle_insert(target_path: str, insert_line: Any, new_str: Any, context: Any) -> ToolResult:
    if insert_line is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `insert_line` is required for command `insert`.",
        )
    if new_str is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error="Parameter `new_str` is required for command `insert`.",
        )

    try:
        ins_line = int(insert_line)
    except (ValueError, TypeError):
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Invalid `insert_line` parameter: {insert_line}. Must be an integer.",
        )

    new_s = str(new_str)
    p = pathlib.Path(target_path)
    if not p.is_file():
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"File does not exist at `{target_path}`.",
        )

    try:
        meta = read_text_file_with_metadata(target_path)
        current_content = meta["content"]
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to read file `{target_path}`: {exc}",
        )

    lines = current_content.splitlines(keepends=True)
    total_lines = len(lines)

    if ins_line < 0 or ins_line > total_lines:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Invalid `insert_line` parameter: {ins_line}. It should be within the range of lines of the file: [0, {total_lines}].",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    sandbox_mode = getattr(context, "sandbox_mode", None)
    project_root = getattr(context, "project_root", None)
    if isinstance(context, dict):
        sandbox_mode = context.get("sandbox_mode", sandbox_mode)
        project_root = context.get("project_root", project_root)
    from coderai.core.sandbox import check_sandbox_path_access

    sb_allowed, sb_err = check_sandbox_path_access(
        target_path, op="write", mode=sandbox_mode, workspace_root=project_root
    )
    if not sb_allowed and sb_err:
        return ToolResult(ok=False, name="str_replace_editor", error=sb_err)

    from coderai.core.tools.observation import get_observation_tracker

    allowed, obs_err = get_observation_tracker().check_mutation_allowed(
        session_id, target_path, require_observed=True
    )
    if not allowed and obs_err:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=obs_err,
        )

    _push_undo(target_path, current_content)

    if context and hasattr(context, "on_before_file_mutation") and context.on_before_file_mutation:
        context.on_before_file_mutation(target_path)

    insert_content = new_s if new_s.endswith("\n") else new_s + "\n"
    if ins_line == 0:
        new_content = insert_content + "".join(lines)
    else:
        new_content = "".join(lines[:ins_line]) + insert_content + "".join(lines[ins_line:])

    try:
        write_text_file(target_path, new_content)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to write file `{target_path}`: {exc}",
        )

    _record_state(session_id, target_path, new_content)
    get_observation_tracker().record_observation(session_id, target_path, content=new_content)

    if context and hasattr(context, "on_after_file_mutation") and context.on_after_file_mutation:
        context.on_after_file_mutation(target_path)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"The file {target_path} has been edited successfully (inserted text after line {ins_line}).",
    )


def _handle_undo(target_path: str, context: Any) -> ToolResult:
    prev = _pop_undo(target_path)
    if prev is None:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"No previous edit found in history for `{target_path}` to undo.",
        )

    if context and hasattr(context, "on_before_file_mutation") and context.on_before_file_mutation:
        context.on_before_file_mutation(target_path)

    try:
        write_text_file(target_path, prev)
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="str_replace_editor",
            error=f"Failed to restore file `{target_path}`: {exc}",
        )

    session_id = str(getattr(context, "session_id", "default") or "default")
    _record_state(session_id, target_path, prev)

    if context and hasattr(context, "on_after_file_mutation") and context.on_after_file_mutation:
        context.on_after_file_mutation(target_path)

    return ToolResult(
        ok=True,
        name="str_replace_editor",
        output=f"Successfully undid previous edit on {target_path}.",
    )

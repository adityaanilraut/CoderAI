"""Shared context, sandbox, and callback plumbing for file mutations."""

from __future__ import annotations

import difflib
import pathlib
from typing import Any

from coderai.core.common.file_utils import write_text_file
from coderai.core.sandbox import check_sandbox_path_access, validate_sandboxed_path


def context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def is_dry_run(context: Any, args: dict[str, Any] | None = None) -> bool:
    """Return True if the execution context or tool arguments specify dry-run mode."""
    dry_val = context_value(context, "dry_run", False)
    if isinstance(dry_val, bool) and dry_val is True:
        return True
    if args and isinstance(args, dict):
        arg_val = args.get("dry_run")
        if isinstance(arg_val, bool) and arg_val is True:
            return True
    return False


def generate_virtual_patch(
    file_path: str,
    old_content: str | None,
    new_content: str,
) -> dict[str, Any]:
    """Generate structured virtual patch and diff metadata without touching disk."""
    old_text = old_content if old_content is not None else ""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    lines_added = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    lines_removed = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )

    return {
        "file_path": file_path,
        "diff": diff_text,
        "is_creation": old_content is None,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "old_bytes": len(old_text.encode("utf-8")),
        "new_bytes": len(new_content.encode("utf-8")),
    }


def check_file_write_access(context: Any, file_path: str) -> str | None:
    """Validate whether write access is permitted under current sandbox and isolated_cwd."""
    isolated_cwd = context_value(context, "isolated_cwd")
    if (
        isinstance(isolated_cwd, (str, pathlib.Path))
        and str(isolated_cwd).strip()
        and "MagicMock" not in str(type(isolated_cwd))
    ):
        valid, resolved, err = validate_sandboxed_path(file_path, root=str(isolated_cwd))
        if not valid:
            return err

    sb_mode = context_value(context, "sandbox_mode")
    mode_str = sb_mode if isinstance(sb_mode, str) else None
    ws_root = context_value(context, "project_root")
    ws_str = ws_root if isinstance(ws_root, (str, pathlib.Path)) else "."

    allowed, error = check_sandbox_path_access(
        file_path,
        op="write",
        mode=mode_str,
        workspace_root=ws_str,
    )
    return None if allowed else error


def write_file_with_callbacks(
    context: Any,
    file_path: str,
    content: str,
    encoding: str = "utf8",
    line_endings: str = "LF",
) -> int:
    before = context_value(context, "on_before_file_mutation")
    after = context_value(context, "on_after_file_mutation")
    if callable(before):
        before(file_path)
    bytes_written = write_text_file(file_path, content, encoding, line_endings)
    if callable(after):
        after(file_path)
    return bytes_written


async def async_write_file_with_locks(
    context: Any,
    file_path: str,
    content: str,
    encoding: str = "utf8",
    line_endings: str = "LF",
) -> int:
    """Acquire fine-grained path write lock and write file with lifecycle callbacks."""
    from coderai.core.tools.path_lock import get_path_lock_manager

    ws_root = context_value(context, "project_root")
    path_lock = get_path_lock_manager()
    async with path_lock.acquire_write_lock(file_path, project_root=ws_root):
        return write_file_with_callbacks(
            context=context,
            file_path=file_path,
            content=content,
            encoding=encoding,
            line_endings=line_endings,
        )

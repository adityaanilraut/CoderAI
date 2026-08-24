"""Shared context, sandbox, and callback plumbing for file mutations."""

from __future__ import annotations

from typing import Any

from coderai.core.common.file_utils import write_text_file
from coderai.core.sandbox import check_sandbox_path_access


def context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def check_file_write_access(context: Any, file_path: str) -> str | None:
    allowed, error = check_sandbox_path_access(
        file_path,
        op="write",
        mode=context_value(context, "sandbox_mode"),
        workspace_root=context_value(context, "project_root"),
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

"""Shared context, sandbox, and callback plumbing for file mutations."""

from __future__ import annotations

import difflib
import os
import pathlib
from typing import Any

from coderai.utils.path import write_text_file
from coderai.sandbox import check_sandbox_path_access, validate_sandboxed_path


def context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def get_effective_workdir(context: Any) -> str:
    """Return the effective working directory: isolated_cwd if set, else project_root, else cwd."""
    if context is None:
        return os.getcwd()
    iso = context_value(context, "isolated_cwd")
    if iso and isinstance(iso, (str, pathlib.Path)):
        return str(pathlib.Path(iso).resolve())
    pr = context_value(context, "project_root")
    if pr and isinstance(pr, (str, pathlib.Path)):
        return str(pathlib.Path(pr).resolve())
    return os.getcwd()


def _coerce_dry_run(value: Any) -> bool:
    """Coerce boolean-like dry-run inputs ("true"/1/yes) instead of `is True` checks."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


def is_dry_run(context: Any, args: dict[str, Any] | None = None) -> bool:
    """Return True if the execution context or tool arguments specify dry-run mode."""
    if _coerce_dry_run(context_value(context, "dry_run", False)):
        return True
    if args and isinstance(args, dict):
        if _coerce_dry_run(args.get("dry_run")):
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
    raw = file_path if isinstance(file_path, (str, pathlib.Path)) else ""
    if not str(raw).strip():
        return "SANDBOX_VIOLATION: empty file path."
    isolated_cwd = context_value(context, "isolated_cwd")
    isolated_root: str | None = None
    if isinstance(isolated_cwd, (str, pathlib.Path)) and str(isolated_cwd).strip():
        isolated_root = str(isolated_cwd)

    sb_mode = context_value(context, "sandbox_mode")
    mode_str = sb_mode if isinstance(sb_mode, str) else None
    ws_root = context_value(context, "project_root")
    ws_str = ws_root if isinstance(ws_root, (str, pathlib.Path)) else "."

    if context_value(context, "plan_mode", False):
        from coderai.tools.legacy.policy import is_owned_plan_file

        if not is_owned_plan_file(context, {"file_path": str(raw)}):
            return "PermissionDenied: only this session's plan file may be edited in plan mode."

    # Clamp relative paths against the effective root before any check so
    # `../` escapes cannot slip past either gate as a raw string.
    candidate = str(raw)
    if not pathlib.Path(candidate).is_absolute():
        candidate = str(pathlib.Path(str(isolated_root or ws_str)) / candidate)

    if isolated_root:
        if context_value(context, "plan_mode", False):
            from coderai.tools.legacy.policy import is_owned_plan_file

            if is_owned_plan_file(context, {"file_path": candidate}):
                return None
        valid, resolved, err = validate_sandboxed_path(candidate, root=isolated_root)
        if not valid:
            return err
        candidate = str(resolved)

    if context_value(context, "plan_mode", False):
        from coderai.tools.legacy.policy import is_owned_plan_file

        if is_owned_plan_file(context, {"file_path": candidate}):
            return None

    allowed, error = check_sandbox_path_access(
        candidate,
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
    line_endings: str | list[str] = "LF",
) -> int:
    before = context_value(context, "on_before_file_mutation")
    after = context_value(context, "on_after_file_mutation")
    if callable(before):
        before(file_path)
    from coderai.tools.legacy.observation import get_observation_tracker

    tracker = get_observation_tracker()
    sid = context_value(context, "session_id") or "default"
    observed = tracker.get(sid, file_path)
    if observed:
        allowed, error = tracker.check_mutation_allowed(sid, file_path)
        if not allowed:
            raise OSError(error)
    cancellation = context_value(context, "cancellation_event")
    if cancellation is not None and cancellation.is_set():
        raise OSError("TOOL_CANCELLED: file mutation cancelled before commit")
    bytes_written = write_text_file(
        file_path,
        content,
        encoding,
        line_endings,
        expected_identity=observed.identity if observed else None,
        expected_digest=observed.digest if observed else None,
    )
    if callable(after):
        after(file_path)
    return bytes_written

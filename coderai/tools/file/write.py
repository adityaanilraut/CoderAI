# Ported from coderai/core/tools/write.py - kimi structure (kimi_cli/tools/file/write.py).
"""write tool — create or overwrite files."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from coderai.utils.path import (
    build_diff_preview,
    ensure_parent_directory,
    has_file_changed_since_state,
    normalize_content,
    read_text_file_with_metadata,
)
from coderai.utils.common.validate import ValidationResult, execute_validated_tool
from coderai.state import (
    FileState,
    get_file_state,
    is_absolute_file_path,
    is_full_file_view,
    normalize_file_path,
    record_file_state,
)
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.tools.file.utils import (
    check_file_write_access,
    context_value,
    write_file_with_callbacks,
)


def _validate_write_schema(args: dict[str, Any]) -> tuple[bool, dict[str, Any], str | None]:
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return False, {}, "file_path is required."

    content = args.get("content")
    if not isinstance(content, str):
        return (
            False,
            {},
            "content must be a string. If you are writing JSON, serialize the full document to text before calling write.",
        )

    return True, args, None


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_write_tool(args, context)


def handle_write_tool(args: dict[str, Any], context: Any) -> ToolResult:
    repair_metadata: dict[str, Any] = {}

    def preprocessor(raw_input: dict[str, Any]) -> ValidationResult:
        nonlocal repair_metadata
        raw_fp = raw_input.get("file_path")
        file_path = normalize_file_path(as_str(raw_fp)) if raw_fp else ""
        content = raw_input.get("content")

        if (
            file_path.lower().endswith(".json")
            and content is not None
            and isinstance(content, (dict, list))
        ):
            repair_metadata = {
                "input_repaired": True,
                "repair_kind": "json-stringify-content",
            }
            new_input = dict(raw_input)
            new_input["file_path"] = file_path
            new_input["content"] = json.dumps(content, indent=2)
            return ValidationResult(ok=True, input=new_input)

        repair_metadata = {}
        new_input = dict(raw_input)
        if isinstance(raw_fp, str):
            new_input["file_path"] = file_path
        return ValidationResult(ok=True, input=new_input)

    def run(validated_args: dict[str, Any], ctx: Any) -> ToolResult:
        raw_fp = as_str(validated_args.get("file_path"))
        iso_val = context_value(ctx, "isolated_cwd")
        isolated_cwd = (
            str(iso_val)
            if isinstance(iso_val, (str, pathlib.Path)) and "MagicMock" not in str(type(iso_val))
            else None
        )
        if isolated_cwd and not is_absolute_file_path(raw_fp):
            file_path = normalize_file_path(str(pathlib.Path(isolated_cwd) / raw_fp))
        else:
            file_path = normalize_file_path(raw_fp)

        if not is_absolute_file_path(file_path):
            return ToolResult(
                ok=False,
                name="write",
                error="file_path must be an absolute path.",
            )

        session_id = context_value(ctx, "session_id") or "default"
        sandbox_error = check_file_write_access(ctx, file_path)
        if sandbox_error:
            return ToolResult(ok=False, name="write", error=sandbox_error)

        p = pathlib.Path(file_path)
        existing_file = p.exists()

        if existing_file:
            try:
                st = p.stat()
            except Exception as e:
                return ToolResult(ok=False, name="write", error=f"Failed to stat file: {e}")

            if p.is_dir():
                return ToolResult(ok=False, name="write", error="file_path points to a directory.")

            if st.st_size > 0:
                file_state = get_file_state(session_id, file_path)
                if not file_state or not is_full_file_view(file_state):
                    return ToolResult(
                        ok=False,
                        name="write",
                        error="Must read the full existing file before writing.",
                    )

                if has_file_changed_since_state(file_path, file_state):
                    return ToolResult(
                        ok=False,
                        name="write",
                        error="File has been modified since read. Read it again before writing.",
                    )

        raw_content = as_str(validated_args.get("content"))
        normalized_content = normalize_content(raw_content)

        try:
            existing_metadata = read_text_file_with_metadata(file_path) if existing_file else None
            diff_preview = build_diff_preview(
                file_path,
                existing_metadata["content"] if existing_metadata else None,
                normalized_content,
            )

            from coderai.tools.file.utils import generate_virtual_patch, is_dry_run

            if is_dry_run(ctx, validated_args):
                patch = generate_virtual_patch(
                    file_path,
                    existing_metadata["content"] if existing_metadata else None,
                    normalized_content,
                )
                meta = {
                    "type": "update" if existing_metadata else "create",
                    "file_path": file_path,
                    "dry_run": True,
                    "diff_preview": diff_preview or patch["diff"],
                    "virtual_patch": patch,
                    "lines_added": patch["lines_added"],
                    "lines_removed": patch["lines_removed"],
                }
                if repair_metadata:
                    meta.update(repair_metadata)
                return ToolResult(
                    ok=True,
                    name="write",
                    output=f"[dry-run] Virtual write preview for {file_path}:\n{patch['diff']}"
                    if patch["diff"]
                    else f"[dry-run] Verified write for {file_path}.",
                    metadata=meta,
                )

            ensure_parent_directory(file_path)

            encoding = existing_metadata["encoding"] if existing_metadata else "utf8"
            line_endings = (
                existing_metadata["lineEndings"]
                if existing_metadata
                else ("CRLF" if "\r\n" in raw_content else "LF")
            )

            bytes_written = write_file_with_callbacks(
                ctx, file_path, normalized_content, encoding, line_endings
            )

            fresh_metadata = read_text_file_with_metadata(file_path)

            record_file_state(
                session_id,
                FileState(
                    file_path=file_path,
                    content=fresh_metadata["content"],
                    timestamp=fresh_metadata["timestamp"],
                    encoding=fresh_metadata["encoding"],
                    line_endings=fresh_metadata["lineEndings"],
                ),
                increment_version=True,
            )

            from coderai.tools.legacy.observation import get_observation_tracker

            get_observation_tracker().record_observation(
                session_id, file_path, content=fresh_metadata["content"]
            )

            meta = {
                "type": "update" if existing_metadata else "create",
                "file_path": file_path,
                "bytes": bytes_written,
                "encoding": fresh_metadata["encoding"],
                "line_endings": fresh_metadata["lineEndings"],
                "cache_refreshed": True,
                "diff_preview": diff_preview,
            }
            if repair_metadata:
                meta.update(repair_metadata)

            return ToolResult(
                ok=True,
                name="write",
                output="Updated file." if existing_metadata else "Created file.",
                metadata=meta,
            )
        except Exception as e:
            return ToolResult(ok=False, name="write", error=str(e))

    return execute_validated_tool(
        "write",
        args,
        context,
        run,
        validator=_validate_write_schema,
        preprocessor=preprocessor,
    )


# --- Kimi CallableTool2 Parity ---

from collections.abc import Callable as _Callable
from pathlib import Path as _Path
from typing import Literal as _Literal
from kaos.path import KaosPath as _KaosPath
from kosong.tooling import CallableTool2 as _CallableTool2, ToolError as _ToolError, ToolReturnValue as _ToolReturnValue
from pydantic import BaseModel as _BaseModel, Field as _Field

from coderai.soul.agent import Runtime as _Runtime
from coderai.soul.approval import Approval as _Approval
from coderai.tools.display import DisplayBlock as _DisplayBlock
from coderai.tools.file.plan_mode import inspect_plan_edit_target as _inspect_plan_edit_target
from coderai.tools.utils import load_desc as _load_desc
from coderai.utils.diff import build_diff_blocks as _build_diff_blocks
from coderai.utils.logging import logger as _logger
from coderai.utils.path import is_within_workspace as _is_within_workspace, kaos_path_from_user_input as _kaos_path_from_user_input

_BASE_WRITE_DESCRIPTION = _load_desc(_Path(__file__).parent / "write.md")


class WriteParams(_BaseModel):
    path: str = _Field(
        description=(
            "The path to the file to write. Absolute paths are required when writing files "
            "outside the working directory."
        )
    )
    content: str = _Field(description="The content to write to the file")
    mode: _Literal["overwrite", "append"] = _Field(
        description=(
            "The mode to use to write to the file. "
            "Two modes are supported: `overwrite` for overwriting the whole file and "
            "`append` for appending to the end of an existing file."
        ),
        default="overwrite",
    )


class WriteFile(_CallableTool2[WriteParams]):
    name: str = "WriteFile"
    description: str = _BASE_WRITE_DESCRIPTION
    params: type[WriteParams] = WriteParams

    def __init__(self, runtime: _Runtime, approval: _Approval):
        super().__init__()
        builtin = getattr(runtime, "builtin_args", None)
        self._work_dir = getattr(builtin, "CODERAI_WORK_DIR", getattr(builtin, "KIMI_WORK_DIR", _KaosPath.cwd()))
        self._additional_dirs = getattr(runtime, "additional_dirs", [])
        self._approval = approval
        self._plan_mode_checker: _Callable[[], bool] | None = None
        self._plan_file_path_getter: _Callable[[], _Path | None] | None = None

    def bind_plan_mode(
        self, checker: _Callable[[], bool], path_getter: _Callable[[], _Path | None]
    ) -> None:
        self._plan_mode_checker = checker
        self._plan_file_path_getter = path_getter

    async def _validate_path(self, path: _KaosPath) -> _ToolError | None:
        resolved_path = path.canonical()
        if (
            not _is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            return _ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to write a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    async def __call__(self, params: WriteParams) -> _ToolReturnValue:
        if not params.path:
            return _ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        try:
            p = _kaos_path_from_user_input(params.path)
            if err := await self._validate_path(p):
                return err
            p = p.canonical()

            plan_target = _inspect_plan_edit_target(
                p,
                plan_mode_checker=self._plan_mode_checker,
                plan_file_path_getter=self._plan_file_path_getter,
            )
            if isinstance(plan_target, _ToolError):
                return plan_target

            is_plan_file_write = plan_target.is_plan_target
            if is_plan_file_write and plan_target.plan_path is not None:
                plan_target.plan_path.parent.mkdir(parents=True, exist_ok=True)

            if not await p.parent.exists():
                return _ToolError(
                    message=f"`{params.path}` parent directory does not exist.",
                    brief="Parent directory not found",
                )

            if params.mode not in ["overwrite", "append"]:
                return _ToolError(
                    message=(
                        f"Invalid write mode: `{params.mode}`. "
                        "Mode must be either `overwrite` or `append`."
                    ),
                    brief="Invalid write mode",
                )

            file_existed = await p.exists()
            old_text = None
            if file_existed:
                old_text = await p.read_text(errors="replace")

            new_text = (
                params.content if params.mode == "overwrite" else (old_text or "") + params.content
            )
            diff_blocks = await _build_diff_blocks(
                str(p),
                old_text or "",
                new_text,
            )

            if not is_plan_file_write:
                from coderai.tools.file import FileActions
                action = (
                    FileActions.EDIT
                    if _is_within_workspace(p, self._work_dir, self._additional_dirs)
                    else FileActions.EDIT_OUTSIDE
                )
                result = await self._approval.request(
                    self.name,
                    action,
                    f"Write file `{p}`",
                    display=diff_blocks,
                )
                if not result:
                    return result.rejection_error()

            match params.mode:
                case "overwrite":
                    await p.write_text(params.content)
                case "append":
                    await p.append_text(params.content)

            file_size = (await p.stat()).st_size
            action_desc = "overwritten" if params.mode == "overwrite" else "appended to"
            return _ToolReturnValue(
                is_error=False,
                output="",
                message=(f"File successfully {action_desc}. Current size: {file_size} bytes."),
                display=diff_blocks,
            )
        except Exception as e:
            _logger.warning("WriteFile failed: {path}: {error}", path=params.path, error=e)
            return _ToolError(
                message=f"Failed to write to {params.path}. Error: {e}",
                brief="Failed to write file",
            )


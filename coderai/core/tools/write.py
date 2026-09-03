"""write tool — create or overwrite files."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from coderai.core.common.file_utils import (
    build_diff_preview,
    ensure_parent_directory,
    has_file_changed_since_state,
    normalize_content,
    read_text_file_with_metadata,
)
from coderai.core.common.validate import ValidationResult, execute_validated_tool
from coderai.core.state import (
    FileState,
    get_file_state,
    is_absolute_file_path,
    is_full_file_view,
    normalize_file_path,
    record_file_state,
)
from coderai.core.tools.types import ToolResult, as_str
from coderai.core.tools.file_mutation import (
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

            from coderai.core.tools.file_mutation import generate_virtual_patch, is_dry_run

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

            from coderai.core.tools.observation import get_observation_tracker

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

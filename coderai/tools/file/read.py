"""read tool — returns snippet_id for scoped edits."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import stat
from typing import Any

from pathspec import GitIgnoreSpec

from coderai.utils.path import detect_encoding, is_binary_buffer, open_regular_binary
from coderai.file_snippets import (
    create_full_file_snippet,
    create_snippet,
    is_absolute_file_path,
    mark_file_read,
    normalize_file_path,
)
from coderai.tools.file.utils import get_effective_workdir
from coderai.tools.legacy.types import (
    ToolExecutionFollowUpMessage,
    ToolResult,
    as_str,
)

DEFAULT_LINE_LIMIT = 2000
MAX_LINE_LENGTH = 2000
LINE_NUMBER_WIDTH = 6
READ_MAX_BYTES = 50 * 1024  # READ_MAX_BYTES cap

DEFAULT_GITIGNORE = [
    "node_modules/",
    ".git/",
    "dist/",
    "build/",
    "out/",
    ".next/",
    ".nuxt/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".gradle/",
    ".idea/",
    ".vscode/",
    "*.class",
    "*.jar",
    "*.war",
    "target/",
]

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".svg",
    ".ico",
    ".avif",
}


def _get_image_mime_type(ext: str) -> str:
    mimes = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".avif": "image/avif",
    }
    return mimes.get(ext.lower(), "image/png")


def _format_with_line_numbers(lines: list[str], start_line_number: int) -> str:
    formatted: list[str] = []
    for index, line in enumerate(lines):
        line_num = start_line_number + index
        if len(line) > MAX_LINE_LENGTH:
            trimmed = line[:MAX_LINE_LENGTH] + f"... (line truncated to {MAX_LINE_LENGTH} chars)"
        else:
            trimmed = line
        formatted.append(f"{str(line_num).rjust(LINE_NUMBER_WIDTH)}\t{trimmed}")
    return "\n".join(formatted)


def _read_notebook(file_path: str) -> str:
    try:
        raw = pathlib.Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "WARNING: File is empty."
    if not raw.strip():
        return "WARNING: File is empty."

    try:
        parsed = json.loads(raw)
    except Exception:
        return "WARNING: File is empty."

    cells = parsed.get("cells")
    if not isinstance(cells, list) or not cells:
        return "WARNING: Notebook has no cells."

    lines: list[str] = []
    for idx, cell in enumerate(cells, 1):
        cell_type = cell.get("cell_type", "unknown")
        lines.append(f"# Cell {idx} ({cell_type})")

        source = cell.get("source", [])
        if isinstance(source, list):
            lines.extend(s.rstrip("\r\n") for s in source)
        elif isinstance(source, str):
            lines.extend(source.splitlines())

        outputs = cell.get("outputs", [])
        if isinstance(outputs, list):
            for out_idx, output in enumerate(outputs, 1):
                if not isinstance(output, dict):
                    continue
                out_type = output.get("output_type", "output")
                lines.append(f"# Output {out_idx} ({out_type})")
                lines.extend(_format_notebook_output(output))

    if not lines:
        return "WARNING: Notebook has no cells."

    return _format_with_line_numbers(lines, 1)


def _format_notebook_output(output: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    text = output.get("text")
    if isinstance(text, list):
        lines.extend(s.rstrip("\r\n") for s in text)
    elif isinstance(text, str):
        lines.extend(text.splitlines())

    data = output.get("data")
    if isinstance(data, dict):
        text_plain = data.get("text/plain")
        if isinstance(text_plain, list):
            lines.extend(s.rstrip("\r\n") for s in text_plain)
        elif isinstance(text_plain, str):
            lines.extend(text_plain.splitlines())
        if isinstance(data.get("image/png"), str):
            lines.append(f"[image/png {len(data['image/png'])} chars]")
        if isinstance(data.get("image/jpeg"), str):
            lines.append(f"[image/jpeg {len(data['image/jpeg'])} chars]")

    traceback = output.get("traceback")
    if isinstance(traceback, list):
        lines.extend(s.rstrip("\r\n") for s in traceback)

    if not lines:
        lines.append("[output omitted]")
    return lines


def _load_ignore_spec(project_root: str) -> GitIgnoreSpec:
    """Compile default exclusions and the project root gitignore rules."""
    patterns = list(DEFAULT_GITIGNORE)
    try:
        patterns.extend(
            (pathlib.Path(project_root) / ".gitignore")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        pass
    return GitIgnoreSpec.from_lines(patterns)


def _read_directory(dir_path: str, project_root: str, max_entries: int = 150) -> str:
    """Read directory contents respecting .gitignore and format as a structured tree."""
    p = pathlib.Path(dir_path)
    matcher = _load_ignore_spec(project_root)
    lines: list[str] = [f"Directory listing for `{dir_path}`:\n"]

    entries: list[tuple[str, bool, int]] = []
    try:
        for root, dirs, files in os.walk(dir_path):
            rel_root = os.path.relpath(root, project_root).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""

            filtered_dirs = []
            for d in dirs:
                rel_dir = f"{rel_root}/{d}".lstrip("/")
                if not matcher.match_file(rel_dir + "/"):
                    filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for d in sorted(dirs):
                full_dir = os.path.join(root, d)
                rel_to_target = os.path.relpath(full_dir, dir_path).replace("\\", "/")
                entries.append((rel_to_target, True, 0))

            for f in sorted(files):
                rel_file = f"{rel_root}/{f}".lstrip("/")
                if matcher.match_file(rel_file):
                    continue
                full_file = os.path.join(root, f)
                try:
                    size = os.path.getsize(full_file)
                except Exception:
                    size = 0
                rel_to_target = os.path.relpath(full_file, dir_path).replace("\\", "/")
                entries.append((rel_to_target, False, size))

            # Limit deep nested search
            depth = len(pathlib.Path(root).relative_to(p).parts)
            if depth >= 3:
                dirs[:] = []
    except Exception as e:
        return f"Error reading directory: {e}"

    if not entries:
        return f"Directory `{dir_path}` is empty (or all contents are ignored)."

    total_count = len(entries)
    showing = entries[:max_entries]
    for rel_path, is_directory, size in showing:
        if is_directory:
            lines.append(f"  [DIR]  {rel_path}/")
        else:
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            lines.append(f"  [FILE] {rel_path} ({size_str})")

    if total_count > max_entries:
        lines.append(f"\n...and {total_count - max_entries} more items.")

    return "\n".join(lines)


def _normalize_relative_suffix(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").strip().lstrip("./")
    return normalized


def _find_suffix_matches(project_root: str, suffix: str) -> list[str]:
    matches: list[str] = []
    normalized_suffix = suffix.replace("\\", "/").lower()
    matcher = _load_ignore_spec(project_root)

    for root, dirs, files in os.walk(project_root):
        rel_root = os.path.relpath(root, project_root).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""

        # Filter directories in-place respecting gitignore
        filtered_dirs = []
        for d in dirs:
            rel_dir = f"{rel_root}/{d}".lstrip("/")
            if not matcher.match_file(rel_dir + "/"):
                filtered_dirs.append(d)
        dirs[:] = filtered_dirs

        for f in files:
            rel_file = f"{rel_root}/{f}".lstrip("/")
            if matcher.match_file(rel_file):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/").lower()
            if rel_path == normalized_suffix or rel_path.endswith(f"/{normalized_suffix}"):
                matches.append(full_path)
                if len(matches) > 10:
                    return matches
    return matches


def _parse_line_number(value: Any, label: str) -> tuple[bool, int | None, str | None]:
    if value is None or value == "":
        return True, None, None
    # Must be integer, not float string. Reject "2.0", "1.5" like Number.isInteger.
    if isinstance(value, float):
        return False, None, f"{label} must be a positive integer"
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return True, None, None
        # reject float strings
        if "." in s or "e" in s.lower():
            return False, None, f"{label} must be a positive integer"
        try:
            integer = int(s, 10)
        except (ValueError, TypeError):
            return False, None, f"{label} must be a positive integer"
        if integer < 1:
            return False, None, f"{label} must be a positive integer"
        return True, integer, None
    try:
        integer = int(value)  # type: ignore[arg-type]
        # reject booleans (int(True)==1)
        if isinstance(value, bool) or float(value) != integer:
            return False, None, f"{label} must be a positive integer"
    except (ValueError, TypeError):
        return False, None, f"{label} must be a positive integer"
    if integer < 1:
        return False, None, f"{label} must be a positive integer"
    return True, integer, None


def _parse_line_limit(value: Any) -> tuple[bool, int, str | None]:
    if value is None or value == "":
        return True, DEFAULT_LINE_LIMIT, None
    if isinstance(value, float):
        return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return True, DEFAULT_LINE_LIMIT, None
        if "." in s or "e" in s.lower():
            return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
        try:
            integer = int(s, 10)
        except (ValueError, TypeError):
            return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
        if integer <= 0:
            return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
        if integer > DEFAULT_LINE_LIMIT:
            return (
                False,
                DEFAULT_LINE_LIMIT,
                f"limit must be less than or equal to {DEFAULT_LINE_LIMIT}",
            )
        return True, integer, None
    try:
        integer = int(value)  # type: ignore[arg-type]
        if isinstance(value, bool) or float(value) != integer:
            return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
    except (ValueError, TypeError):
        return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
    if integer <= 0:
        return False, DEFAULT_LINE_LIMIT, "limit must be a positive integer"
    if integer > DEFAULT_LINE_LIMIT:
        return (
            False,
            DEFAULT_LINE_LIMIT,
            f"limit must be less than or equal to {DEFAULT_LINE_LIMIT}",
        )
    return True, integer, None


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_read_tool(args, context)


def handle_read_tool(args: dict[str, Any], context: Any) -> ToolResult:
    raw_path = as_str(args.get("file_path"))
    file_path = normalize_file_path(raw_path) if raw_path else ""

    if not file_path.strip():
        return ToolResult(
            ok=False,
            name="read",
            error='Missing required "file_path" string.',
        )

    if isinstance(context, dict):
        session_id = context.get("session_id") or "default"
    else:
        session_id = getattr(context, "session_id", None) or "default"
    project_root = get_effective_workdir(context)

    if not is_absolute_file_path(file_path):
        # Clamp joined paths inside the project root; a literal "../" prefix
        # check alone misses "subdir/../../" escapes.
        joined = os.path.normpath(os.path.join(project_root, file_path))
        root = os.path.normpath(project_root)
        if joined != root and not joined.startswith(root + os.sep):
            return ToolResult(
                ok=False,
                name="read",
                error="file_path escapes the project root.",
            )

        suffix = _normalize_relative_suffix(file_path)
        matches = _find_suffix_matches(project_root, suffix) if suffix else []
        if len(matches) > 1:
            more_str = f"\n...and {len(matches) - 3} more." if len(matches) > 3 else ""
            return ToolResult(
                ok=False,
                name="read",
                error=(
                    "file_path must be an absolute path. "
                    "The file_path is ambiguous and may refer to multiple files:\n"
                    + "\n".join(matches[:3])
                    + more_str
                ),
            )

        resolved_path = os.path.normpath(os.path.join(project_root, file_path))
        _root = os.path.normpath(project_root)
        if resolved_path != _root and not resolved_path.startswith(_root + os.sep):
            return ToolResult(
                ok=False,
                name="read",
                error="file_path escapes the project root.",
            )
        if not os.path.exists(resolved_path):
            if len(matches) > 0:
                return ToolResult(
                    ok=False,
                    name="read",
                    error=f'file_path must be an absolute path. The file_path "{file_path}" is ambiguous.',
                )
            return ToolResult(
                ok=False,
                name="read",
                error=f"File not found: {file_path}",
            )
        file_path = resolved_path

    p = pathlib.Path(file_path)
    if not p.exists():
        return ToolResult(ok=False, name="read", error=f"File not found: {file_path}")

    try:
        st = p.stat()
    except Exception as e:
        return ToolResult(ok=False, name="read", error=f"Failed to stat file: {e}")

    if p.is_dir():
        listing = _read_directory(file_path, project_root)
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        return ToolResult(
            ok=True,
            name="read",
            output=listing,
            metadata={"isDirectory": True, "filePath": file_path},
        )

    if not stat.S_ISREG(st.st_mode):
        return ToolResult(ok=False, name="read", error="read requires a regular file or directory.")

    ext = p.suffix.lower()
    if ext in IMAGE_EXTENSIONS and st.st_size > 10 * 1024 * 1024:
        return ToolResult(ok=False, name="read", error="Image file exceeds the 10 MiB limit.")
    if ext == ".ipynb" and st.st_size > 10 * 1024 * 1024:
        return ToolResult(ok=False, name="read", error="Notebook exceeds the 10 MiB limit.")

    if ext == ".ipynb":
        output = _read_notebook(file_path)
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        return ToolResult(ok=True, name="read", output=output)

    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            if st.st_size > 50 * 1024 * 1024:
                raise ValueError("PDF exceeds the 50 MiB metadata inspection limit.")
            with open_regular_binary(str(p)) as pdf_stream:
                page_count = len(PdfReader(pdf_stream).pages)
        except Exception as e:
            return ToolResult(ok=False, name="read", error=f"Failed to inspect PDF: {e}")
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        return ToolResult(
            ok=True,
            name="read",
            output="WARNING: File is binary.",
            metadata={
                "mime": "application/pdf",
                "encoding": "base64",
                "bytes": st.st_size,
                "pageCount": page_count if page_count is not None else 0,
            },
        )

    if ext in IMAGE_EXTENSIONS:
        try:
            with open_regular_binary(str(p)) as image_stream:
                img_bytes = image_stream.read(10 * 1024 * 1024 + 1)
            if len(img_bytes) > 10 * 1024 * 1024:
                return ToolResult(ok=False, name="read", error="Image exceeds the 10 MiB limit.")
        except Exception as e:
            return ToolResult(ok=False, name="read", error=f"Failed to read image: {e}")
        mime = _get_image_mime_type(ext)
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        b64_str = base64.b64encode(img_bytes).decode("ascii")
        follow_up = ToolExecutionFollowUpMessage(
            role="system",
            content=f"The read tool has loaded `{p.name}`. Use the attached image content to answer the original request.",
            content_params=[
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64_str}"},
                }
            ],
        )
        return ToolResult(
            ok=True,
            name="read",
            output="File loaded.",
            metadata={"mime": mime, "bytes": len(img_bytes)},
            follow_up_messages=[follow_up],
        )

    try:
        with open_regular_binary(str(p)) as sample_stream:
            raw_bytes = sample_stream.read(8192)
    except Exception as e:
        return ToolResult(ok=False, name="read", error=f"Failed to read file: {e}")

    if detect_encoding(raw_bytes) == "utf8" and is_binary_buffer(raw_bytes):
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        return ToolResult(
            ok=True,
            name="read",
            output="WARNING: File is binary.",
            metadata={"isBinary": True, "bytes": st.st_size},
        )

    offset_ok, offset, offset_err = _parse_line_number(args.get("offset"), "offset")
    if not offset_ok:
        return ToolResult(
            ok=False, name="read", error=offset_err or "offset must be a number >= 1."
        )

    limit_ok, limit, limit_err = _parse_line_limit(args.get("limit"))
    if not limit_ok:
        return ToolResult(ok=False, name="read", error=limit_err or "limit must be a number > 0.")

    from coderai.tools.file.window import read_text_window

    try:
        metadata = read_text_window(file_path, offset or 1, limit)
    except (OSError, UnicodeError) as e:
        return ToolResult(ok=False, name="read", error=str(e))
    total_lines = metadata["total_lines"]
    if offset is not None and offset > total_lines:
        return ToolResult(
            ok=False,
            name="read",
            error=f'offset {offset} is out of range for "{file_path}" ({total_lines} lines)',
            metadata={"code": "FS_NOT_FOUND"},
        )
    selected = metadata["lines"]
    start_line = offset or 1
    end_line = start_line + len(selected) - 1
    truncated_by_bytes = metadata["capped"]
    is_partial_view = (
        start_line != 1 or end_line < total_lines or truncated_by_bytes or metadata["omitted"]
    )

    mark_file_read(
        session_id,
        file_path,
        {
            "content": "\n".join(selected),
            "timestamp": metadata["timestamp"],
            "offset": start_line if is_partial_view else None,
            "limit": len(selected) if is_partial_view else None,
            "is_partial_view": is_partial_view,
            "encoding": metadata["encoding"],
            "line_endings": metadata["lineEndings"],
            "raw_digest": metadata["digest"],
            "file_identity": metadata["identity"],
        },
    )

    from coderai.tools.legacy.observation import get_observation_tracker

    get_observation_tracker().record_observation(
        session_id,
        file_path,
        digest=metadata["digest"],
        identity=metadata["identity"],
        encoding=metadata["encoding"],
        line_endings=metadata["lineEndings"],
        complete=not is_partial_view,
    )

    formatted_output = _format_with_line_numbers(selected, start_line)
    if st.st_size == 0:
        formatted_output = "WARNING: File is empty."

    # Output footers: capped > paged > eof
    if truncated_by_bytes:
        formatted_output = f"{formatted_output}\n\n(Output capped. Showing lines {start_line}-{end_line}. Use offset={end_line + 1} to continue.)"
    elif end_line < total_lines:
        formatted_output = (
            f"{formatted_output}\n\n(Showing lines {start_line}-{end_line} of {total_lines}. "
            f"Use offset={end_line + 1} to continue.)"
        )

    if is_partial_view:
        snippet = create_snippet(session_id, file_path, start_line, end_line, formatted_output)
    else:
        snippet = create_full_file_snippet(
            session_id, file_path, start_line, end_line, formatted_output
        )

    snippet_meta = None
    if snippet:
        snippet_meta = {
            "id": snippet.id,
            "filePath": snippet.file_path,
            "startLine": snippet.start_line,
            "endLine": snippet.end_line,
        }

    return ToolResult(
        ok=True,
        name="read",
        output=formatted_output,
        metadata={"snippet": snippet_meta} if snippet_meta else None,
    )

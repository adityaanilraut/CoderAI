# Ported from coderai/core/tools/read.py - kimi structure (kimi_cli/tools/file/read.py).
"""read tool — returns snippet_id for scoped edits."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
from typing import Any

from coderai.utils.path import is_binary_buffer, read_text_file_with_metadata
from coderai.state import (
    create_full_file_snippet,
    create_snippet,
    is_absolute_file_path,
    mark_file_read,
    normalize_file_path,
)
from coderai.tools.legacy.types import (
    ToolExecutionFollowUpMessage,
    ToolResult,
    as_str,
)

DEFAULT_LINE_LIMIT = 2000
MAX_LINE_LENGTH = 2000
LINE_NUMBER_WIDTH = 6
READ_MAX_BYTES = 50 * 1024  # DSH READ_MAX_BYTES cap
STREAM_MIN_SIZE = (
    10 * 1024 * 1024
)  # DSH streaming threshold; Python reads whole but documents ceiling
# ponytail: whole-file read; streaming via chunked scan if large files cause OOM

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


def _count_pdf_pages(data: bytes) -> int | None:
    try:
        content = data.decode("latin1", errors="replace")
        matches = re.findall(r"/Type\s*/Page\b(?!s)", content)
        return len(matches) if matches else 0
    except Exception:
        return None


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


class GitignoreMatcher:
    """Evaluates relative file and directory paths against gitignore rules."""

    def __init__(self, patterns: list[str]) -> None:
        self.rules: list[tuple[bool, bool, re.Pattern]] = []  # (is_negation, is_dir_only, regex)
        for pat in patterns:
            self._add_pattern(pat)

    def _add_pattern(self, pattern: str) -> None:
        pat = pattern.strip()
        if not pat or pat.startswith("#"):
            return
        is_negation = pat.startswith("!")
        if is_negation:
            pat = pat[1:].strip()
        is_dir_only = pat.endswith("/")
        if is_dir_only:
            pat = pat[:-1]

        rooted = pat.startswith("/")
        if rooted:
            pat = pat[1:]

        regex_parts: list[str] = []
        i = 0
        while i < len(pat):
            c = pat[i]
            if c == "*":
                if i + 1 < len(pat) and pat[i + 1] == "*":
                    if i + 2 < len(pat) and pat[i + 2] == "/":
                        regex_parts.append("(?:.+/)?")
                        i += 3
                        continue
                    else:
                        regex_parts.append(".*")
                        i += 2
                        continue
                else:
                    regex_parts.append("[^/]*")
                    i += 1
                    continue
            elif c == "?":
                regex_parts.append("[^/]")
            elif c in r"\.+^${}()|[]":
                regex_parts.append(re.escape(c))
            else:
                regex_parts.append(c)
            i += 1

        pattern_str = "".join(regex_parts)
        if rooted:
            regex = re.compile(rf"^{pattern_str}(?:/.*)?$", re.IGNORECASE)
        else:
            regex = re.compile(rf"(?:^|/){pattern_str}(?:/.*)?$", re.IGNORECASE)

        self.rules.append((is_negation, is_dir_only, regex))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        normalized = rel_path.replace("\\", "/").strip("/")
        if not normalized:
            return False

        parts = normalized.split("/")
        ancestor_dirs = ["/".join(parts[:i]) for i in range(1, len(parts))]

        ignored = False
        for is_negation, is_dir_only, regex in self.rules:
            if not is_dir_only or is_dir:
                if regex.search(normalized):
                    ignored = not is_negation
                    continue
            if is_dir_only and not is_dir:
                if any(regex.search(ancestor) for ancestor in ancestor_dirs):
                    ignored = not is_negation
                    continue
        return ignored


def load_gitignore_matcher(project_root: str) -> GitignoreMatcher:
    """Load GitignoreMatcher using default ignore rules and project .gitignore if present."""
    patterns = list(DEFAULT_GITIGNORE)
    gitignore_path = pathlib.Path(project_root) / ".gitignore"
    if gitignore_path.is_file():
        try:
            content = gitignore_path.read_text(encoding="utf-8", errors="replace")
            patterns.extend(content.splitlines())
        except Exception:
            pass
    return GitignoreMatcher(patterns)


def _read_directory(dir_path: str, project_root: str, max_entries: int = 150) -> str:
    """Read directory contents respecting .gitignore and format as a structured tree."""
    p = pathlib.Path(dir_path)
    matcher = load_gitignore_matcher(project_root)
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
                if not matcher.is_ignored(rel_dir, is_dir=True):
                    filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for d in sorted(dirs):
                full_dir = os.path.join(root, d)
                rel_to_target = os.path.relpath(full_dir, dir_path).replace("\\", "/")
                entries.append((rel_to_target, True, 0))

            for f in sorted(files):
                rel_file = f"{rel_root}/{f}".lstrip("/")
                if matcher.is_ignored(rel_file, is_dir=False):
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
    matcher = load_gitignore_matcher(project_root)

    for root, dirs, files in os.walk(project_root):
        rel_root = os.path.relpath(root, project_root).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""

        # Filter directories in-place respecting gitignore
        filtered_dirs = []
        for d in dirs:
            rel_dir = f"{rel_root}/{d}".lstrip("/")
            if not matcher.is_ignored(rel_dir, is_dir=True):
                filtered_dirs.append(d)
        dirs[:] = filtered_dirs

        for f in files:
            rel_file = f"{rel_root}/{f}".lstrip("/")
            if matcher.is_ignored(rel_file, is_dir=False):
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
    # DSH: must be integer, not float string. Reject "2.0", "1.5" like Number.isInteger.
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
        project_root = context.get("project_root") or os.getcwd()
    else:
        session_id = getattr(context, "session_id", None) or "default"
        project_root = getattr(context, "project_root", None) or os.getcwd()

    if not is_absolute_file_path(file_path):
        if file_path.startswith("../") or file_path.startswith("..\\"):
            return ToolResult(
                ok=False,
                name="read",
                error="file_path must be an absolute path.",
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

    ext = p.suffix.lower()

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
            pdf_bytes = p.read_bytes()
        except Exception as e:
            return ToolResult(ok=False, name="read", error=f"Failed to read PDF: {e}")
        page_count = _count_pdf_pages(pdf_bytes)
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
                "bytes": len(pdf_bytes),
                "pageCount": page_count if page_count is not None else 0,
            },
        )

    if ext in IMAGE_EXTENSIONS:
        try:
            img_bytes = p.read_bytes()
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
        raw_bytes = p.read_bytes()
    except Exception as e:
        return ToolResult(ok=False, name="read", error=f"Failed to read file: {e}")

    if is_binary_buffer(raw_bytes):
        mark_file_read(
            session_id,
            file_path,
            {"content": "", "timestamp": int(st.st_mtime * 1000), "is_partial_view": True},
        )
        return ToolResult(
            ok=True,
            name="read",
            output="WARNING: File is binary.",
            metadata={"isBinary": True, "bytes": len(raw_bytes)},
        )

    offset_ok, offset, offset_err = _parse_line_number(args.get("offset"), "offset")
    if not offset_ok:
        return ToolResult(
            ok=False, name="read", error=offset_err or "offset must be a number >= 1."
        )

    limit_ok, limit, limit_err = _parse_line_limit(args.get("limit"))
    if not limit_ok:
        return ToolResult(ok=False, name="read", error=limit_err or "limit must be a number > 0.")

    try:
        metadata = read_text_file_with_metadata(file_path)
    except Exception as e:
        return ToolResult(ok=False, name="read", error=str(e))

    raw = metadata["content"]
    if not raw:
        return ToolResult(
            ok=True,
            name="read",
            output="WARNING: File is empty.",
        )

    lines = raw.split("\n")
    if len(lines) == 1 and lines[0] == "":
        return ToolResult(
            ok=True,
            name="read",
            output="WARNING: File is empty.",
        )

    total_lines = len(lines)
    # DSH: offset out of range (except empty+offset1 which already returned)
    if offset is not None and offset > total_lines:
        return ToolResult(
            ok=False,
            name="read",
            error=f'offset {offset} is out of range for "{file_path}" ({total_lines} lines)',
            metadata={"code": "FS_NOT_FOUND"},
        )
    start_index = (offset - 1) if offset else 0
    # DSH READ_MAX_BYTES: byte cap like buildWindow — stop adding lines when cap hit
    # ponytail: linear scan, no streaming; upgrade to chunked scan if large files regress
    truncated_by_bytes = False
    selected: list[str] = []
    output_bytes = 0
    for idx in range(start_index, min(start_index + limit, total_lines)):
        raw_line = lines[idx]
        # apply line truncation first for byte counting (matches DSH truncateLine)
        display_line = (
            raw_line[:MAX_LINE_LENGTH] + f"... (line truncated to {MAX_LINE_LENGTH} chars)"
            if len(raw_line) > MAX_LINE_LENGTH
            else raw_line
        )
        bsize = len(display_line.encode("utf-8")) + (1 if selected else 0)
        if output_bytes + bsize > READ_MAX_BYTES:
            truncated_by_bytes = True
            break
        output_bytes += bsize
        selected.append(raw_line)
    start_line = start_index + 1
    end_line = (start_index + len(selected)) if selected else start_line
    is_partial_view = start_line != 1 or end_line < total_lines or truncated_by_bytes

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
        },
    )

    from coderai.tools.legacy.observation import get_observation_tracker

    get_observation_tracker().record_observation(session_id, file_path, content=raw)

    formatted_output = _format_with_line_numbers(selected, start_line)

    # DSH formatReadOutput footers: capped > paged > eof
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


# --- Kimi CallableTool2 Parity ---

from collections import deque as _deque
from pathlib import Path as _Path
from kaos.path import KaosPath as _KaosPath
from kosong.tooling import CallableTool2 as _CallableTool2, ToolError as _ToolError, ToolOk as _ToolOk, ToolReturnValue as _ToolReturnValue
from pydantic import BaseModel as _BaseModel, Field as _Field, model_validator as _model_validator

from coderai.soul.agent import Runtime as _Runtime
from coderai.tools.file.utils import MEDIA_SNIFF_BYTES as _MEDIA_SNIFF_BYTES, detect_file_type as _detect_file_type
from coderai.tools.utils import load_desc as _load_desc, truncate_line as _truncate_line
from coderai.utils.logging import logger as _logger
from coderai.utils.path import is_within_workspace as _is_within_workspace, kaos_path_from_user_input as _kaos_path_from_user_input
from coderai.utils.sensitive import is_sensitive_file as _is_sensitive_file

MAX_READ_LINES = 1000
MAX_READ_LINE_LENGTH = 2000
MAX_READ_BYTES = 100 << 10  # 100KB


class ReadParams(_BaseModel):
    path: str = _Field(
        description=(
            "The path to the file to read. Absolute paths are required when reading files "
            "outside the working directory."
        )
    )
    line_offset: int = _Field(
        description=(
            "The line number to start reading from. "
            "By default read from the beginning of the file. "
            "Set this when the file is too large to read at once. "
            "Negative values read from the end of the file (e.g. -100 reads the last 100 lines). "
            f"The absolute value of negative offset cannot exceed {MAX_READ_LINES}."
        ),
        default=1,
    )
    n_lines: int = _Field(
        description=(
            "The number of lines to read. "
            f"By default read up to {MAX_READ_LINES} lines, which is the max allowed value. "
            "Set this value when the file is too large to read at once."
        ),
        default=MAX_READ_LINES,
        ge=1,
    )

    @_model_validator(mode="after")
    def _validate_line_offset(self) -> "ReadParams":
        if self.line_offset == 0:
            raise ValueError(
                "line_offset cannot be 0; use 1 for the first line or -1 for the last line"
            )
        if self.line_offset < -MAX_READ_LINES:
            raise ValueError(
                f"line_offset cannot be less than -{MAX_READ_LINES}. "
                "Use a positive line_offset with the total line count "
                "to read from a specific position."
            )
        return self


class ReadFile(_CallableTool2[ReadParams]):
    name: str = "ReadFile"
    params: type[ReadParams] = ReadParams

    def __init__(self, runtime: _Runtime) -> None:
        description = _load_desc(
            _Path(__file__).parent / "read.md",
            {
                "MAX_LINES": MAX_READ_LINES,
                "MAX_LINE_LENGTH": MAX_READ_LINE_LENGTH,
                "MAX_BYTES": MAX_READ_BYTES,
            },
        )
        super().__init__(description=description)
        self._runtime = runtime
        builtin = getattr(runtime, "builtin_args", None)
        self._work_dir = getattr(builtin, "CODERAI_WORK_DIR", getattr(builtin, "KIMI_WORK_DIR", _KaosPath.cwd()))
        self._additional_dirs = getattr(runtime, "additional_dirs", [])

    async def _validate_path(self, path: _KaosPath) -> _ToolError | None:
        resolved_path = path.canonical()
        if (
            not _is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            return _ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to read a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    async def __call__(self, params: ReadParams) -> _ToolReturnValue:
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

            if _is_sensitive_file(str(p)):
                return _ToolError(
                    message=(
                        f"`{params.path}` appears to contain secrets "
                        "(matched sensitive file pattern). "
                        "Reading this file is blocked to protect credentials."
                    ),
                    brief="Sensitive file",
                )

            if not await p.exists():
                return _ToolError(
                    message=f"`{params.path}` does not exist.",
                    brief="File not found",
                )
            if not await p.is_file():
                return _ToolError(
                    message=f"`{params.path}` is not a file.",
                    brief="Invalid path",
                )

            header = await p.read_bytes(_MEDIA_SNIFF_BYTES)
            file_type = _detect_file_type(str(p), header=header)
            if file_type.kind in ("image", "video"):
                return _ToolError(
                    message=(
                        f"`{params.path}` is a {file_type.kind} file. "
                        "Use other appropriate tools to read image or video files."
                    ),
                    brief="Unsupported file type",
                )

            if file_type.kind == "unknown":
                return _ToolError(
                    message=(
                        f"`{params.path}` seems not readable. "
                        "You may need to read it with proper shell commands, Python tools "
                        "or MCP tools if available."
                    ),
                    brief="File not readable",
                )

            assert params.n_lines >= 1
            assert params.line_offset != 0

            if params.line_offset < 0:
                return await self._read_tail(p, params)
            else:
                return await self._read_forward(p, params)
        except Exception as e:
            _logger.warning("ReadFile failed: {path}: {error}", path=params.path, error=e)
            return _ToolError(
                message=f"Failed to read {params.path}. Error: {e}",
                brief="Failed to read file",
            )

    async def _read_forward(self, p: _KaosPath, params: ReadParams) -> _ToolReturnValue:
        lines: list[str] = []
        n_bytes = 0
        truncated_line_numbers: list[int] = []
        max_lines_reached = False
        max_bytes_reached = False
        collecting = True
        current_line_no = 0
        async for line in p.read_lines(errors="replace"):
            current_line_no += 1
            if not collecting:
                continue
            if current_line_no < params.line_offset:
                continue
            truncated = _truncate_line(line, MAX_READ_LINE_LENGTH)
            if truncated != line:
                truncated_line_numbers.append(current_line_no)
            lines.append(truncated)
            n_bytes += len(truncated.encode("utf-8"))
            if len(lines) >= params.n_lines:
                collecting = False
            elif len(lines) >= MAX_READ_LINES:
                max_lines_reached = True
                collecting = False
            elif n_bytes >= MAX_READ_BYTES:
                max_bytes_reached = True
                collecting = False

        total_lines = current_line_no
        start_line = params.line_offset
        lines_with_no: list[str] = []
        for line_num, line in zip(range(start_line, start_line + len(lines)), lines, strict=True):
            lines_with_no.append(f"{line_num:6d}\t{line}")

        message = (
            f"{len(lines)} lines read from file starting from line {start_line}."
            if len(lines) > 0
            else "No lines read from file."
        )
        message += f" Total lines in file: {total_lines}."
        if max_lines_reached:
            message += f" Max {MAX_READ_LINES} lines reached."
        elif max_bytes_reached:
            message += f" Max {MAX_READ_BYTES} bytes reached."
        elif len(lines) < params.n_lines:
            message += " End of file reached."
        if truncated_line_numbers:
            message += f" Lines {truncated_line_numbers} were truncated."
        return _ToolOk(
            output="".join(lines_with_no),
            message=message,
        )

    async def _read_tail(self, p: _KaosPath, params: ReadParams) -> _ToolReturnValue:
        tail_count = abs(params.line_offset)
        tail_buf: _deque[tuple[int, str, bool]] = _deque(maxlen=tail_count)
        current_line_no = 0
        async for line in p.read_lines(errors="replace"):
            current_line_no += 1
            truncated = _truncate_line(line, MAX_READ_LINE_LENGTH)
            tail_buf.append((current_line_no, truncated, truncated != line))

        total_lines = current_line_no
        all_entries = list(tail_buf)
        line_limit = min(params.n_lines, MAX_READ_LINES)
        candidates = all_entries[:line_limit]
        max_lines_reached = len(all_entries) > MAX_READ_LINES and len(candidates) == MAX_READ_LINES

        total_candidate_bytes = sum(len(entry[1].encode("utf-8")) for entry in candidates)
        if total_candidate_bytes > MAX_READ_BYTES:
            max_bytes_reached = True
            kept = 0
            n_bytes = 0
            for entry in reversed(candidates):
                n_bytes += len(entry[1].encode("utf-8"))
                if n_bytes > MAX_READ_BYTES:
                    break
                kept += 1
            candidates = candidates[len(candidates) - kept :]
        else:
            max_bytes_reached = False

        lines: list[str] = []
        line_numbers: list[int] = []
        truncated_line_numbers: list[int] = []

        for line_no, truncated, was_truncated in candidates:
            if was_truncated:
                truncated_line_numbers.append(line_no)
            lines.append(truncated)
            line_numbers.append(line_no)

        lines_with_no: list[str] = []
        for line_num, line in zip(line_numbers, lines, strict=True):
            lines_with_no.append(f"{line_num:6d}\t{line}")

        start_line = line_numbers[0] if line_numbers else total_lines + 1
        message = (
            f"{len(lines)} lines read from file starting from line {start_line}."
            if len(lines) > 0
            else "No lines read from file."
        )
        message += f" Total lines in file: {total_lines}."
        if max_lines_reached:
            message += f" Max {MAX_READ_LINES} lines reached."
        elif max_bytes_reached:
            message += f" Max {MAX_READ_BYTES} bytes reached."
        elif len(lines) < params.n_lines:
            message += " End of file reached."
        if truncated_line_numbers:
            message += f" Lines {truncated_line_numbers} were truncated."
        return _ToolOk(
            output="".join(lines_with_no),
            message=message,
        )

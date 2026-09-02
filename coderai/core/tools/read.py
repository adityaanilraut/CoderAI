"""read tool — returns snippet_id for scoped edits."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
from typing import Any

from coderai.core.common.file_utils import is_binary_buffer, read_text_file_with_metadata
from coderai.core.state import (
    create_full_file_snippet,
    create_snippet,
    is_absolute_file_path,
    mark_file_read,
    normalize_file_path,
)
from coderai.core.tools.types import (
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

    from coderai.core.tools.observation import get_observation_tracker

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

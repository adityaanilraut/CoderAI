"""read tool — returns snippet_id for scoped edits (deepcode read-handler.ts)."""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
from typing import Any

from coderai.core.common.file_utils import read_text_file_with_metadata
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
        trimmed = line[:MAX_LINE_LENGTH]
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


def _normalize_relative_suffix(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").strip().lstrip("./")
    return normalized


def _find_suffix_matches(project_root: str, suffix: str) -> list[str]:
    matches: list[str] = []
    normalized_suffix = suffix.replace("\\", "/").lower()

    for root, dirs, files in os.walk(project_root):
        # Exclude ignored directories
        dirs[:] = [
            d
            for d in dirs
            if d not in {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist"}
        ]
        for f in files:
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
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return False, None, f"{label} must be a number."
    if not (numeric == numeric and int(numeric) == numeric):
        return False, None, f"{label} must be an integer."
    integer = int(numeric)
    if integer < 1:
        return False, None, f"{label} must be >= 1."
    return True, integer, None


def _parse_line_limit(value: Any) -> tuple[bool, int, str | None]:
    if value is None or value == "":
        return True, DEFAULT_LINE_LIMIT, None
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return False, DEFAULT_LINE_LIMIT, "limit must be a number."
    if not (numeric == numeric and int(numeric) == numeric):
        return False, DEFAULT_LINE_LIMIT, "limit must be an integer."
    integer = int(numeric)
    if integer <= 0:
        return False, DEFAULT_LINE_LIMIT, "limit must be > 0."
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
        return ToolResult(
            ok=False,
            name="read",
            error="file_path points to a directory. Use bash ls for directories.",
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
    start_index = (offset - 1) if offset else 0
    end_index = start_index + limit
    selected = lines[start_index:end_index]
    start_line = start_index + 1
    end_line = (start_index + len(selected)) if selected else start_line
    is_partial_view = start_line != 1 or end_line < total_lines

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

    formatted_output = _format_with_line_numbers(selected, start_line)

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

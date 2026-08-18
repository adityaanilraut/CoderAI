"""File I/O with metadata — port of deepcode core/src/common/file-utils.ts."""

from __future__ import annotations

import pathlib
from typing import Any


def normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n")


def detect_line_endings(value: str) -> str:
    return "CRLF" if "\r\n" in value else "LF"


def detect_encoding(buf: bytes) -> str:
    if len(buf) >= 2 and buf[0] == 0xFF and buf[1] == 0xFE:
        return "utf16le"
    return "utf8"


def read_text_file_with_metadata(path: str) -> dict[str, Any]:
    p = pathlib.Path(path)
    raw = p.read_bytes()
    enc = detect_encoding(raw)
    text = raw.decode("utf-16-le" if enc == "utf16le" else "utf-8", errors="replace")
    content = normalize_content(text)
    return {
        "content": content,
        "encoding": enc,
        "lineEndings": detect_line_endings(text),
        "timestamp": int(p.stat().st_mtime * 1000),
    }


def write_text_file(
    path: str, content: str, encoding: str = "utf8", line_endings: str = "LF"
) -> int:
    norm = normalize_content(content)
    to_write = norm.replace("\n", "\r\n") if line_endings == "CRLF" else norm
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    target_encoding = "utf-16-le" if encoding == "utf16le" else "utf-8"
    p.write_text(to_write, encoding=target_encoding)
    return len(to_write.encode(target_encoding))


def ensure_parent_directory(path: str) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)


def has_file_changed_since_state(path: str, state: Any) -> bool:
    try:
        cur = read_text_file_with_metadata(path)
        return cur["timestamp"] > state.timestamp
    except Exception:
        return True


def build_diff_preview(file_path: str, old_content: str | None, new_content: str) -> str:
    import difflib

    old_lines = (old_content or "").splitlines()
    new_lines = new_content.splitlines()
    diff = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{file_path}", tofile=f"b/{file_path}", lineterm=""
        )
    )
    return "\n".join(diff[:200])


def read_text_file_tail(path: str, max_chars: int = 4000) -> dict[str, Any] | None:
    """Read the trailing character slice of a file safely."""
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        if size == 0:
            return {"content": "", "total_bytes": 0, "truncated": False}
        with open(p, "rb") as f:
            chunk_size = min(size, max_chars * 4)
            if size > chunk_size:
                f.seek(size - chunk_size)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > max_chars or size > chunk_size
        if len(text) > max_chars:
            text = text[-max_chars:]
        return {
            "content": text.strip(),
            "total_bytes": size,
            "truncated": truncated,
        }
    except Exception:
        return None

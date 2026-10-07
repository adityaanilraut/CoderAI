"""Bounded, non-consuming previews of output files."""

from pathlib import Path


def tail_file(path: str | Path, *, lines: int = 12, max_bytes: int = 65536) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read(max_bytes)
        text = data.decode("utf-8", errors="replace")
        if offset and "\n" in text:
            text = text.split("\n", 1)[1]
        return "\n".join(text.splitlines()[-lines:])
    except OSError as exc:
        return f"Cannot read output: {exc}"

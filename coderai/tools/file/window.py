"""Bounded text windows with a digest of the exact bytes read."""

from __future__ import annotations

import codecs
import hashlib
from typing import Any

from coderai.tools.legacy.observation import file_identity
from coderai.tools.legacy.sanitizer import sanitize_text


def read_text_window(
    path: str, offset: int, limit: int, *, max_bytes: int = 50 * 1024, max_line: int = 2000
) -> dict[str, Any]:
    before = file_identity(path)
    digest = hashlib.sha256()
    selected: list[str] = []
    number, output_bytes = 1, 0
    prefix = ""
    length = 0
    last_char = ""
    capped = omitted = crlf = False

    def add_piece(piece: str) -> None:
        nonlocal prefix, length, last_char
        if piece:
            last_char = piece[-1]
        length += len(piece)
        if len(prefix) <= max_line:
            prefix += piece[: max_line + 1 - len(prefix)]

    def finish() -> None:
        nonlocal prefix, length, output_bytes, capped, omitted, crlf, last_char
        if last_char == "\r":
            crlf = True
        if prefix.endswith("\r") and length <= max_line:
            prefix = prefix[:-1]
            length -= 1
            crlf = True
        if offset <= number < offset + limit and not capped:
            shown = prefix[:max_line]
            if length > max_line:
                shown += f"... (line truncated to {max_line} chars)"
                omitted = True
            sanitized = sanitize_text(shown)[0]
            omitted |= sanitized != shown
            cost = len(sanitized.encode("utf-8")) + 9
            if output_bytes + cost > max_bytes - 200:
                capped = True
            else:
                selected.append(shown)
                output_bytes += cost
        prefix, length, last_char = "", 0, ""

    from coderai.utils.path import open_regular_binary

    with open_regular_binary(path) as stream:
        first = stream.read(64 * 1024)
        encoding = (
            "utf16le"
            if first.startswith(b"\xff\xfe")
            else "utf16be"
            if first.startswith(b"\xfe\xff")
            else "utf8"
        )
        decoder = codecs.getincrementaldecoder(
            {"utf16le": "utf-16-le", "utf16be": "utf-16-be"}.get(encoding, "utf-8")
        )("strict")
        chunk = first
        while chunk:
            digest.update(chunk)
            parts = decoder.decode(chunk).split("\n")
            for piece in parts[:-1]:
                add_piece(piece)
                finish()
                number += 1
            add_piece(parts[-1])
            chunk = stream.read(64 * 1024)
        add_piece(decoder.decode(b"", final=True))
        finish()
    after = file_identity(path)
    if before != after:
        raise OSError("File changed while being read; retry the read.")
    return {
        "lines": selected,
        "total_lines": number,
        "digest": digest.hexdigest(),
        "identity": after,
        "encoding": encoding,
        "lineEndings": "CRLF" if crlf else "LF",
        "timestamp": after[3] // 1_000_000,
        "capped": capped,
        "omitted": omitted,
    }

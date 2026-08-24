"""Session-scoped spill store for oversized tool output.

locator plus retrieval hint, and never turn a successful tool call into an error
when storage is unavailable.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

SPILL_SKIP_TOOLS = {"read", "Read"}
DEFAULT_MAX_INLINE_BYTES = 30_000
RETRIEVAL_HINT = "Use the read tool on that path to inspect the complete result."

_default_root: pathlib.Path | None = None


@dataclass
class SpillRef:
    locator: str
    bytes: int
    retrieval_hint: str = RETRIEVAL_HINT
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "locator": self.locator,
            "bytes": self.bytes,
            "retrievalHint": self.retrieval_hint,
        }
        if self.sha256:
            d["sha256"] = self.sha256
        return d


def encode_segment(raw: str) -> str:
    """Encode an untrusted string as one filesystem-safe path segment."""
    if raw == "":
        return "~"
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out: list[str] = []
    for ch in raw:
        if ch != "~" and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append(f"~{ord(ch):04X}")
    return "".join(out) or "~"


def private_root() -> pathlib.Path:
    global _default_root
    if _default_root is None:
        _default_root = pathlib.Path(tempfile.mkdtemp(prefix="coderai-spill-"))
        try:
            os.chmod(_default_root, 0o700)
        except OSError:
            pass
    return _default_root


def session_dir(root: pathlib.Path, session_id: str) -> pathlib.Path:
    digest = sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return root / f"session-{digest}"


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def save_text(
    *,
    session_id: str,
    suggested_name: str,
    content: str,
    root: pathlib.Path | None = None,
) -> SpillRef:
    """Persist content under a private session directory. Raises on storage failure."""
    if not session_id:
        raise ValueError("spill requires a session owner")
    base = root or private_root()
    directory = session_dir(base, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    safe_name = encode_segment(suggested_name or "result.txt")
    path = directory / f"{secrets.token_hex(6)}-{safe_name}"
    data = content.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    digest = sha256(data).hexdigest()
    return SpillRef(locator=str(path), bytes=len(data), sha256=digest)


def try_save_text(
    *,
    session_id: str,
    suggested_name: str,
    content: str,
    root: pathlib.Path | None = None,
) -> SpillRef | None:
    """Best-effort save. Returns None on missing owner or storage failure."""
    if not session_id:
        return None
    try:
        return save_text(
            session_id=session_id,
            suggested_name=suggested_name,
            content=content,
            root=root,
        )
    except OSError:
        return None


def spill_notice(ref: SpillRef, omitted_label: str) -> str:
    return f"({omitted_label} Full formatted result stored at: {ref.locator}. {ref.retrieval_hint})"


_ELLIPSIS = "\n...\n"
_ELLIPSIS_BYTES = 5  # UTF-8 length of _ELLIPSIS


def preview_head_tail(text: str, budget_bytes: int) -> tuple[str, int]:
    """Keep head/tail of `text` within `budget_bytes` UTF-8 bytes. Returns (preview, omitted_bytes).

    The ellipsis between the two ends is counted inside the budget so a later
    spill notice cannot push the replacement over `maxInlineBytes`.
    """
    encoded = text.encode("utf-8")
    total = len(encoded)
    if budget_bytes <= 0:
        return "", total
    if total <= budget_bytes:
        return text, 0
    inner = budget_bytes - _ELLIPSIS_BYTES
    if inner <= 0:
        head = encoded[:budget_bytes].decode("utf-8", errors="ignore")
        return head, max(0, total - utf8_len(head))
    head_n = (inner + 1) // 2
    tail_n = inner // 2
    head = encoded[:head_n].decode("utf-8", errors="ignore")
    tail = encoded[total - tail_n :].decode("utf-8", errors="ignore")
    omitted = max(0, total - utf8_len(head) - utf8_len(tail))
    preview = f"{head}{_ELLIPSIS}{tail}"
    if utf8_len(preview) > budget_bytes:
        head = encoded[:budget_bytes].decode("utf-8", errors="ignore")
        return head, max(0, total - utf8_len(head))
    return preview, omitted


def apply_spill_policy(
    text: str,
    *,
    session_id: str,
    tool_name: str,
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    suggested_name: str | None = None,
    root: pathlib.Path | None = None,
) -> tuple[str, SpillRef | None]:
    """If text exceeds the inline budget, spill the full copy and return a preview + notice."""
    total = utf8_len(text)
    if total <= max_inline_bytes:
        return text, None
    ref = try_save_text(
        session_id=session_id,
        suggested_name=suggested_name or f"{tool_name}.txt",
        content=text,
        root=root,
    )
    if ref is None:
        return text, None
    dummy_notice = spill_notice(ref, f"{total} bytes omitted.")
    reserve = utf8_len(dummy_notice) + 2
    preview_budget = max(0, max_inline_bytes - reserve)
    preview, omitted = preview_head_tail(text, preview_budget)
    notice = spill_notice(ref, f"{omitted} bytes omitted.")
    replaced = f"{preview}\n\n{notice}" if preview else notice
    if utf8_len(replaced) > max_inline_bytes:
        return text, None
    return replaced, ref

"""Clipboard utilities for the TUI.

Copy order:
1. Native clipboard tools (pbcopy / wl-copy / xclip) — works on macOS Terminal
2. OSC-52 via an optional writer (Textual's driver) or sys.stdout
3. An explicitly requested, owner-only temp-file fallback
"""

from __future__ import annotations

import base64
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# A callback that accepts either (message) like Textual App.notify,
# or (level, message) like the toast helper.
ClipboardNotify = Callable[..., None]

# Optional sink for the OSC-52 escape sequence (e.g. Textual driver.write).
Osc52Writer = Callable[[str], None]

# Soft cap for OSC-52 payloads (many terminals truncate around 100KB).
_OSC52_ENCODED_LIMIT = 102_400


@dataclass(frozen=True)
class CopyResult:
    """Outcome of a copy attempt."""

    method: str  # "native" | "osc52" | "file" | "none"
    chars: int
    truncated: bool = False
    path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.method != "none"


def copy_text(
    text: str,
    *,
    write_osc52: Optional[Osc52Writer] = None,
    notify_fn: Optional[ClipboardNotify] = None,
    fallback_file: bool = False,
) -> CopyResult:
    """Copy ``text`` to the system clipboard, with graceful fallbacks.

    Args:
        text: Payload to copy.
        write_osc52: Optional writer for the OSC-52 sequence. Prefer Textual's
            ``App._driver.write`` so the sequence reaches the terminal. When
            omitted, falls back to ``sys.stdout``.
        notify_fn: Optional notification callback.
        fallback_file: Save a private temp-file copy only if OSC-52 fails or
            truncates the payload. Disabled by default so ordinary clipboard
            copies do not silently persist their contents to disk.
    """
    original_len = len(text)
    if _copy_native(text):
        result = CopyResult(method="native", chars=original_len)
        _notify_copy(notify_fn, result)
        return result

    truncated = False
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    reported_len = original_len
    if len(encoded) > _OSC52_ENCODED_LIMIT:
        encoded = encoded[:_OSC52_ENCODED_LIMIT]
        reported_len = min(original_len, (_OSC52_ENCODED_LIMIT // 4) * 3)
        truncated = True

    # An accepted OSC-52 sequence has no acknowledgement. Keep a complete
    # private copy only when the sequence is known to be incomplete, never for
    # a normal successful copy.
    file_path = _write_fallback_file(text) if fallback_file and truncated else None

    sequence = f"\033]52;c;{encoded}\007"
    writer = write_osc52 or _stdout_writer
    try:
        writer(sequence)
    except OSError:
        if file_path is None and fallback_file:
            file_path = _write_fallback_file(text)
        if file_path is not None:
            result = CopyResult(method="file", chars=original_len, path=file_path)
            _notify_copy(notify_fn, result)
            return result
        result = CopyResult(method="none", chars=0)
        _notify_copy(notify_fn, result)
        return result

    result = CopyResult(
        method="osc52",
        chars=reported_len,
        truncated=truncated,
        path=file_path,
    )
    _notify_copy(notify_fn, result)
    return result


def copy_to_clipboard_osc52(text: str, notify_fn: Optional[ClipboardNotify] = None) -> None:
    """Backward-compatible OSC-52 helper (also tries native clipboard first)."""
    copy_text(text, notify_fn=notify_fn, fallback_file=False)


def copy_fallback_file(text: str, notify_fn: Optional[ClipboardNotify] = None) -> Optional[Path]:
    """Save text to a fallback file if OSC-52 fails."""
    path = _write_fallback_file(text)
    if path is not None and notify_fn:
        _emit_notify(notify_fn, "info", f"Fallback: saved to {path}")
    return path


def _stdout_writer(sequence: str) -> None:
    sys.stdout.write(sequence)
    sys.stdout.flush()


def _copy_native(text: str) -> bool:
    """Copy via OS clipboard tools. Returns True on success."""
    system = platform.system()
    try:
        if system == "Darwin" and shutil.which("pbcopy"):
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=5,
            )
            return True
        if system == "Linux":
            if shutil.which("wl-copy"):
                subprocess.run(
                    ["wl-copy"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=5,
                )
                return True
            if shutil.which("xclip"):
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=5,
                )
                return True
            if shutil.which("xsel"):
                subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=5,
                )
                return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _write_fallback_file(text: str) -> Optional[Path]:
    path: Optional[Path] = None
    try:
        # mkstemp creates an unpredictable filename with mode 0600, avoiding
        # world-readable files and symlink races in a shared temp directory.
        fd, raw_path = tempfile.mkstemp(prefix="coderAI-copy-", suffix=".txt")
        path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path
    except OSError:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def _notify_copy(notify_fn: Optional[ClipboardNotify], result: CopyResult) -> None:
    if notify_fn is None or not result.ok:
        if notify_fn is not None and not result.ok:
            _emit_notify(notify_fn, "warning", "Copy failed — no clipboard method available")
        return

    if result.method == "native":
        msg = f"Copied {result.chars:,} chars to clipboard"
    elif result.method == "osc52":
        if result.truncated:
            msg = f"Copied {result.chars:,} chars via OSC-52 (truncated)"
            if result.path is not None:
                msg += f"; full text saved to {result.path}"
        else:
            msg = f"Copied {result.chars:,} chars via OSC-52"
    else:  # file
        msg = f"Saved {result.chars:,} chars to {result.path}"
    _emit_notify(notify_fn, "info", msg)


def _emit_notify(notify_fn: ClipboardNotify, level: str, message: str) -> None:
    """Call notify_fn whether it is ``(msg)`` or ``(level, msg)`` style."""
    try:
        notify_fn(level, message)
    except TypeError:
        notify_fn(message)

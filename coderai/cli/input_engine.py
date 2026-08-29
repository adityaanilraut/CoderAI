"""Multi-line and advanced input buffering engine for CoderAI CLI REPL."""

from __future__ import annotations

from collections.abc import Callable
import os
import re
import shutil
import subprocess
import tempfile

FENCE_PATTERN = re.compile(r"^```", re.MULTILINE)
TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def count_code_fences(text: str) -> int:
    """Count occurrences of triple-backtick markdown fences in text."""
    return len(FENCE_PATTERN.findall(text))


def count_triple_quotes(text: str) -> int:
    """Count occurrences of triple quotes (\"\"\" or ''') in text."""
    return len(TRIPLE_QUOTE_PATTERN.findall(text))


def is_multiline_incomplete(buffer_lines: list[str]) -> bool:
    """Determine if current multi-line input buffer requires further continuation lines."""
    if not buffer_lines:
        return False

    full_text = "\n".join(buffer_lines)

    # 1. Trailing backslash indicates explicit line continuation
    last_line = buffer_lines[-1]
    if last_line.endswith("\\") and not last_line.endswith("\\\\"):
        return True

    # 2. Odd number of ``` indicates an open code block fence
    fences = count_code_fences(full_text)
    if fences % 2 != 0:
        return True

    # 3. Odd number of triple quotes (\"\"\" or ''')
    triple_quotes = count_triple_quotes(full_text)
    if triple_quotes % 2 != 0:
        return True

    return False


def normalize_multiline_input(text: str) -> str:
    """Normalize multi-line input text (strip trailing carriage returns, resolve backslash continuations, unwrap clean envelopes)."""
    if not text:
        return ""

    # Normalize CRLF to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # If the text is wrapped in an outer triple-quote envelope (exactly 2 triple quotes total at start and end),
    # unwrap the outer shell used as the interactive multiline delimiter.
    trimmed = text.strip()
    if (
        trimmed.startswith('"""')
        and trimmed.endswith('"""')
        and len(trimmed) >= 6
        and count_triple_quotes(trimmed) == 2
    ):
        text = trimmed[3:-3].strip()
    elif (
        trimmed.startswith("'''")
        and trimmed.endswith("'''")
        and len(trimmed) >= 6
        and count_triple_quotes(trimmed) == 2
    ):
        text = trimmed[3:-3].strip()

    lines = text.split("\n")
    processed_lines: list[str] = []

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        # Count trailing backslashes to determine if it is an escaped backslash or a line continuation
        num_trailing_slashes = len(line) - len(line.rstrip("\\"))
        if num_trailing_slashes % 2 == 1 and idx + 1 < len(lines):
            # Odd number of trailing backslashes: line continuation
            joined = line[:-1].rstrip() + " " + lines[idx + 1].lstrip()
            lines[idx + 1] = joined
            idx += 1
            continue
        processed_lines.append(line)
        idx += 1

    result = "\n".join(processed_lines).strip()
    return result


def open_external_editor(initial_text: str = "") -> str:
    """Open the system default or user configured $EDITOR to compose a prompt.

    Checks $VISUAL, $EDITOR, with fallbacks to nano, vim, vi, or notepad.
    """
    editor = os.getenv("VISUAL") or os.getenv("EDITOR")
    if not editor:
        if os.name == "nt":
            editor = "notepad"
        else:
            for fallback in ("nano", "vim", "vi", "emacs"):
                if shutil.which(fallback):
                    editor = fallback
                    break
            if not editor:
                editor = "vi"

    with tempfile.NamedTemporaryFile(
        suffix=".coderai.md", mode="w+", encoding="utf-8", delete=False
    ) as tf:
        if initial_text:
            tf.write(initial_text)
        temp_path = tf.name

    try:
        # Run editor in foreground
        ret = subprocess.run(f"{editor} {temp_path}", shell=True)
        if ret.returncode == 0 and os.path.exists(temp_path):
            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()
            return content.strip()
        return ""
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def read_paste_mode(
    input_func: Callable[[str], str] = input,
    prompt_label: str = "paste (enter line with ::: or Ctrl-D to finish)> ",
) -> str:
    """Read multiline paste mode until explicit delimiter ':::' or EOF."""
    print(
        "Entered multiline paste mode. Paste your text, then type ':::' on a new line or press Ctrl-D to finish."
    )
    lines: list[str] = []
    while True:
        try:
            line = input_func("... ")
            if line.strip() == ":::":
                break
            lines.append(line)
        except (EOFError, KeyboardInterrupt):
            break
    return "\n".join(lines).strip()


def read_user_turn(
    prompt: str = "coderai> ",
    continuation_prompt: str = "... ",
    input_func: Callable[[str], str] = input,
) -> str:
    """Read a user turn with support for multi-line triple-backtick blocks, triple-quotes, and line continuations.

    Args:
        prompt: Initial prompt label.
        continuation_prompt: Prompt displayed for continuation lines.
        input_func: Input function (defaults to built-in input).

    Returns:
        Normalized input string.
    """
    first_line = input_func(prompt)
    buffer = [first_line]

    while is_multiline_incomplete(buffer):
        try:
            next_line = input_func(continuation_prompt)
            buffer.append(next_line)
        except (EOFError, KeyboardInterrupt):
            # User interrupted or closed stream during multiline entry
            break

    raw_input = "\n".join(buffer)
    return normalize_multiline_input(raw_input)

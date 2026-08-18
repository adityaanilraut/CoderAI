"""Multi-line and advanced input buffering engine for CoderAI CLI REPL."""

from __future__ import annotations

from collections.abc import Callable
import re

FENCE_PATTERN = re.compile(r"^```", re.MULTILINE)


def count_code_fences(text: str) -> int:
    """Count occurrences of triple-backtick markdown fences in text."""
    return len(FENCE_PATTERN.findall(text))


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

    return False


def normalize_multiline_input(text: str) -> str:
    """Normalize multi-line input text (strip trailing carriage returns, resolve backslash continuations)."""
    if not text:
        return ""

    # Normalize CRLF to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    processed_lines: list[str] = []

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.endswith("\\") and not line.endswith("\\\\") and idx + 1 < len(lines):
            # Line continuation: strip trailing backslash and join with next line
            joined = line[:-1].rstrip() + " " + lines[idx + 1].lstrip()
            lines[idx + 1] = joined
            idx += 1
            continue
        processed_lines.append(line)
        idx += 1

    result = "\n".join(processed_lines).strip()
    return result


def read_user_turn(
    prompt: str = "coderai> ",
    continuation_prompt: str = "... ",
    input_func: Callable[[str], str] = input,
) -> str:
    """Read a user turn with support for multi-line triple-backtick blocks and line continuations.

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

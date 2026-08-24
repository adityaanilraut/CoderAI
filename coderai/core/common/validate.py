""""""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, TypeVar
from collections.abc import Callable

T = TypeVar("T")


@dataclass
class ValidationResult:
    ok: bool
    input: dict[str, Any] | None = None
    error: str | None = None


def clean_json_string(raw: str) -> str:
    """Clean markdown fences and surrounding whitespace from raw JSON strings."""
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def repair_json_string(raw: str) -> str:
    """Heuristically repair broken, truncated, or unterminated JSON strings."""
    cleaned = clean_json_string(raw)
    if not cleaned:
        return "{}"

    # Try fast path if already valid
    import json

    try:
        json.loads(cleaned)
        return cleaned
    except Exception:
        pass

    # Strip trailing whitespace and trailing commas
    s = cleaned.rstrip()

    # Repair partial boolean/null tokens at the very end
    if s.endswith("tru"):
        s = s[:-3] + "true"
    elif s.endswith("fals"):
        s = s[:-4] + "false"
    elif s.endswith("nul"):
        s = s[:-3] + "null"

    # Track open strings and brackets/braces
    stack: list[str] = []
    in_string = False
    escaped = False
    repaired_chars: list[str] = []

    for char in s:
        if escaped:
            repaired_chars.append(char)
            escaped = False
            continue

        if char == "\\":
            repaired_chars.append(char)
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            repaired_chars.append(char)
            continue

        if in_string:
            # Escape literal unescaped newlines inside strings
            if char == "\n":
                repaired_chars.append("\\n")
            elif char == "\r":
                repaired_chars.append("\\r")
            elif char == "\t":
                repaired_chars.append("\\t")
            else:
                repaired_chars.append(char)
            continue

        # Outside of string literals
        if char in ("{", "["):
            stack.append(char)
            repaired_chars.append(char)
        elif char == "}":
            if stack and stack[-1] == "{":
                stack.pop()
            repaired_chars.append(char)
        elif char == "]":
            if stack and stack[-1] == "[":
                stack.pop()
            repaired_chars.append(char)
        else:
            repaired_chars.append(char)

    res = "".join(repaired_chars)

    # If stream ended inside a string literal, close the quote
    if in_string:
        res += '"'

    # Remove any trailing dangling comma outside string
    res = res.rstrip()
    while res.endswith(","):
        res = res[:-1].rstrip()

    # Balance unclosed brackets and braces in reverse order
    while stack:
        opener = stack.pop()
        # Remove trailing comma before closing bracket/brace
        if res.endswith(","):
            res = res[:-1].rstrip()
        if opener == "{":
            res += "}"
        elif opener == "[":
            res += "]"

    # Verify if repair produced valid JSON; if still invalid, wrap if possible
    try:
        json.loads(res)
        return res
    except Exception:
        # Fallback: if it doesn't start with { or [, wrap in {}
        if not (res.startswith("{") or res.startswith("[")):
            res = "{" + res + "}"
            try:
                json.loads(res)
                return res
            except Exception:
                pass
        return res


def semantic_boolean(value: Any, default: bool = False) -> bool:
    """Coerce boolean-like inputs ("true", "false", bool) to a boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return bool(value)


def semantic_integer(
    value: Any, label: str, min_val: int | None = None
) -> tuple[bool, int | None, str | None]:
    """Parse an integer with optional minimum constraint."""
    if value is None or value == "":
        return True, None, None
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return False, None, f"{label} must be a number."
    if not (numeric == numeric and int(numeric) == numeric):
        return False, None, f"{label} must be an integer."
    integer_val = int(numeric)
    if min_val is not None and integer_val < min_val:
        return False, None, f"{label} must be >= {min_val}."
    return True, integer_val, None


def execute_validated_tool(
    name: str,
    raw_args: dict[str, Any],
    context: Any,
    handler: Callable[[dict[str, Any], Any], Any],
    validator: Callable[[dict[str, Any]], tuple[bool, dict[str, Any], str | None]] | None = None,
    preprocessor: Callable[[dict[str, Any]], ValidationResult] | None = None,
) -> Any:
    """Validate tool input, execute handler, and catch validation/runtime errors."""
    if preprocessor:
        preprocessed = preprocessor(raw_args)
        if not preprocessed.ok:
            from coderai.core.tools.types import ToolResult

            return ToolResult(
                ok=False,
                name=name,
                error=f"InputValidationError: {preprocessed.error}",
            )
        args_to_validate = preprocessed.input or {}
    else:
        args_to_validate = raw_args

    if validator:
        is_valid, validated_args, error_msg = validator(args_to_validate)
        if not is_valid:
            from coderai.core.tools.types import ToolResult

            return ToolResult(
                ok=False,
                name=name,
                error=f"InputValidationError: {error_msg or 'Invalid tool input.'}",
            )
    else:
        validated_args = args_to_validate

    if inspect.iscoroutinefunction(handler):

        async def _run_async():
            return await handler(validated_args, context)

        return _run_async()

    return handler(validated_args, context)

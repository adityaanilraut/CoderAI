"""Tool validation utilities — port of deepcode core/src/common/validate.ts."""

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

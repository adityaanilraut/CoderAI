"""Canonical normalization for values returned by tools."""

from typing import Any, Dict

from coderAI.types.tool_error_codes import ToolErrorCode


def normalize_tool_result(
    result: Any,
    *,
    tool_name: str,
    default_error_code: str = ToolErrorCode.TOOL_ERROR,
) -> Dict[str, Any]:
    """Return an idempotent, tool-shaped result dictionary.

    Strings are treated as legacy error responses. Dictionaries are copied so
    normalization never mutates a tool-owned object or a cached result.
    """
    if isinstance(result, dict):
        normalized = dict(result)
        if "success" not in normalized:
            has_useful_output = bool(
                normalized.get("result") or normalized.get("output") or normalized.get("data")
            )
            normalized["success"] = "error" not in normalized and has_useful_output
        if normalized.get("success") is False:
            normalized["error"] = str(normalized.get("error") or f"Tool '{tool_name}' failed.")
            normalized.setdefault("error_code", default_error_code)
        return normalized

    if isinstance(result, str):
        return {
            "success": False,
            "error": result,
            "error_code": default_error_code,
        }

    if result is None:
        return {
            "success": False,
            "error": f"Tool '{tool_name}' returned no result.",
            "error_code": default_error_code,
        }

    return {"success": True, "result": result}

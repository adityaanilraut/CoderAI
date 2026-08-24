"""Declarative tool schema definition and JSON Schema validation."""

from __future__ import annotations

import re
from typing import Any
from collections.abc import Callable, Sequence
from coderai.core.tools.types import (
    ToolDefinition,
    ToolCategory,
    PluginRateLimitedTool,
)


def assert_supported_json_schema(schema: dict[str, Any], path: str = "root") -> None:
    """Validate that a schema definition is a well-formed JSON Schema object."""
    if not isinstance(schema, dict):
        raise TypeError(f"Schema at '{path}' must be a JSON dictionary object.")
    stype = schema.get("type")
    if stype is not None:
        valid_types = {"string", "number", "integer", "boolean", "array", "object", "null"}
        if isinstance(stype, str):
            if stype not in valid_types:
                raise ValueError(f"Schema at '{path}' specifies unsupported type '{stype}'.")
        elif isinstance(stype, list):
            for t in stype:
                if t not in valid_types:
                    raise ValueError(f"Schema at '{path}' specifies unsupported type '{t}'.")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise TypeError(f"Schema 'properties' at '{path}' must be a dictionary.")
        for prop_name, prop_schema in schema["properties"].items():
            assert_supported_json_schema(prop_schema, f"{path}.properties.{prop_name}")
    if "items" in schema and isinstance(schema["items"], dict):
        assert_supported_json_schema(schema["items"], f"{path}.items")


def validate_json_schema_value(schema: dict[str, Any], value: Any, path: str = "root") -> list[str]:
    """Validate a Python value against a JSON Schema, returning a list of violation messages."""
    violations: list[str] = []

    # Handle oneOf
    if "oneOf" in schema:
        branches = schema["oneOf"]
        if not isinstance(branches, list) or len(branches) < 1:
            violations.append(f"{path}: 'oneOf' must be a non-empty list.")
            return violations
        matches = 0
        branch_errors: list[str] = []
        for idx, branch in enumerate(branches):
            errs = validate_json_schema_value(branch, value, f"{path}.oneOf[{idx}]")
            if not errs:
                matches += 1
            else:
                branch_errors.extend(errs)
        if matches != 1:
            violations.append(
                f"{path}: value must match exactly one 'oneOf' schema branch, matched {matches}."
            )
        return violations

    # Handle const
    if "const" in schema:
        if value != schema["const"]:
            violations.append(f"{path}: expected constant value {schema['const']!r}, got {value!r}")
            return violations

    # Handle enum
    if "enum" in schema:
        if value not in schema["enum"]:
            allowed = ", ".join(repr(v) for v in schema["enum"])
            violations.append(f"{path}: invalid value {value!r}. Must be one of: [{allowed}]")
            return violations

    target_type = schema.get("type")
    if target_type is None:
        return violations

    # Check null
    if target_type == "null":
        if value is not None:
            violations.append(f"{path}: expected null, got {type(value).__name__}")
        return violations

    if value is None:
        # None is only permitted if null was in type
        violations.append(f"{path}: value cannot be null/None")
        return violations

    # Check string
    if target_type == "string":
        if not isinstance(value, str):
            violations.append(f"{path}: expected string, got {type(value).__name__}")
            return violations
        if "minLength" in schema and len(value) < schema["minLength"]:
            violations.append(
                f"{path}: string length {len(value)} < minLength {schema['minLength']}"
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            violations.append(
                f"{path}: string length {len(value)} > maxLength {schema['maxLength']}"
            )
        if "pattern" in schema:
            pat = schema["pattern"]
            if not re.search(pat, value):
                violations.append(f"{path}: string does not match regex pattern {pat!r}")

    # Check boolean
    elif target_type == "boolean":
        if not isinstance(value, bool):
            violations.append(f"{path}: expected boolean, got {type(value).__name__}")

    # Check integer
    elif target_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            violations.append(f"{path}: expected integer, got {type(value).__name__}")
            return violations
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(f"{path}: integer {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            violations.append(f"{path}: integer {value} > maximum {schema['maximum']}")

    # Check number
    elif target_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            violations.append(f"{path}: expected number, got {type(value).__name__}")
            return violations
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(f"{path}: number {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            violations.append(f"{path}: number {value} > maximum {schema['maximum']}")

    # Check array
    elif target_type == "array":
        if not isinstance(value, list):
            violations.append(f"{path}: expected array/list, got {type(value).__name__}")
            return violations
        if "minItems" in schema and len(value) < schema["minItems"]:
            violations.append(f"{path}: array length {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            violations.append(f"{path}: array length {len(value)} > maxItems {schema['maxItems']}")
        if "uniqueItems" in schema and schema["uniqueItems"]:
            try:
                unique_set = set(value)
                if len(unique_set) != len(value):
                    violations.append(f"{path}: array items must be unique")
            except TypeError:
                pass
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                violations.extend(validate_json_schema_value(item_schema, item, f"{path}[{i}]"))

    # Check object
    elif target_type == "object":
        if not isinstance(value, dict):
            violations.append(f"{path}: expected object/dict, got {type(value).__name__}")
            return violations
        req_list = schema.get("required") or []
        for req_prop in req_list:
            if req_prop not in value:
                violations.append(f"{path}: missing required property '{req_prop}'")
        props = schema.get("properties") or {}
        for prop_name, prop_val in value.items():
            if prop_name in props:
                violations.extend(
                    validate_json_schema_value(props[prop_name], prop_val, f"{path}.{prop_name}")
                )
            elif schema.get("additionalProperties") is False:
                violations.append(f"{path}: unexpected property '{prop_name}'")

    return violations


def define_tool(
    name: str,
    description: str = "",
    parameters: dict[str, Any] | None = None,
    required: Sequence[str] | None = None,
    handler: Callable[..., Any] | None = None,
    aliases: Sequence[str] | None = None,
    category: ToolCategory = "meta",
    rate_limited_id: PluginRateLimitedTool | None = None,
    is_mutating: bool = False,
    is_concurrency_safe: bool | Callable[[dict[str, Any]], bool] = False,
    timeout_ms: int | None = None,
    present_result: Callable[[dict[str, Any], Any], dict[str, Any] | None] | None = None,
    finalize_content: Callable[[Any, Any], str | None] | None = None,
) -> ToolDefinition:
    """Create a structured ToolDefinition."""
    params = parameters or {}
    req = list(required) if required is not None else []
    als = list(aliases) if aliases is not None else []

    # Infer required from parameters spec if property has required=True
    for key, spec in params.items():
        if isinstance(spec, dict) and spec.get("required") is True:
            if key not in req:
                req.append(key)

    return ToolDefinition(
        name=name,
        description=description,
        parameters=params,
        required=req,
        handler=handler,
        aliases=als,
        category=category,
        rate_limited_id=rate_limited_id,
        is_mutating=is_mutating,
        is_concurrency_safe=is_concurrency_safe,
        timeout_ms=timeout_ms,
        present_result=present_result,
        finalize_content=finalize_content,
    )

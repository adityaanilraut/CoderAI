"""Declarative tool schema definition and JSON Schema validation."""

from __future__ import annotations

import json
from functools import lru_cache

from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from typing import Any
from collections.abc import Callable, Sequence
from coderai.tools.legacy.types import (
    ToolDefinition,
    ToolCategory,
    PluginRateLimitedTool,
)


@lru_cache(maxsize=256)
def _validator(encoded: str) -> Any:
    schema = json.loads(encoded)
    cls = validators.validator_for(schema, default=Draft202012Validator)
    cls.check_schema(schema)
    registry = Registry().with_resource(
        "urn:coderai:tool-schema", Resource.from_contents(schema, default_specification=DRAFT202012)
    )
    resolver = registry.resolver("urn:coderai:tool-schema")
    validator = cls(schema, registry=registry)

    # Resolve references during registration, never as a network side effect.
    def check_refs(node: Any) -> None:
        if isinstance(node, dict):
            for ref in (node.get("$ref"), node.get("$dynamicRef"), node.get("$recursiveRef")):
                if not isinstance(ref, str):
                    continue
                if not ref.startswith("#"):
                    raise ValueError(f"Remote schema reference is disabled: {ref}")
                resolver.lookup(ref)
            for key, child in node.items():
                if key not in ("enum", "const", "default", "examples"):
                    check_refs(child)
        elif isinstance(node, list):
            for child in node:
                check_refs(child)

    check_refs(schema)
    return validator


def assert_supported_json_schema(schema: dict[str, Any], path: str = "root") -> None:
    if not isinstance(schema, dict):
        raise TypeError(f"Schema at '{path}' must be a JSON dictionary object.")
    try:
        _validator(json.dumps(schema, sort_keys=True, allow_nan=False))
    except Exception as exc:
        raise ValueError(f"Invalid tool schema at {path}: {exc}") from exc


def validate_json_schema_value(schema: dict[str, Any], value: Any, path: str = "root") -> list[str]:
    try:
        validator = _validator(json.dumps(schema, sort_keys=True, allow_nan=False))
        errors = sorted(validator.iter_errors(value), key=lambda e: str(list(e.absolute_path)))
        messages = []
        for error in errors:
            message = error.message
            if error.validator == "required":
                missing = next(
                    (key for key in error.validator_value if key not in error.instance), ""
                )
                message = f"missing required argument '{missing}'"
            elif error.validator == "type":
                kind = error.validator_value
                if isinstance(kind, str):
                    message = f"must be a {kind} (got {type(error.instance).__name__})"
            elif error.validator == "enum":
                message = f"invalid value {error.instance!r}; Allowed: {error.validator_value}"
            elif error.validator == "additionalProperties":
                message = f"unknown argument (unexpected property): {message}"
            messages.append(f"{path}{''.join(f'[{p!r}]' for p in error.absolute_path)}: {message}")
        return messages
    except Exception as exc:
        return [f"{path}: schema validation failed: {exc}"]


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
    execution_mode: Any = None,
    rate_limit: tuple[int, float] | None = None,
    timeout_ms: int | None = None,
    present_result: Callable[[dict[str, Any], Any], dict[str, Any] | None] | None = None,
    finalize_content: Callable[[Any, Any], str | None] | None = None,
    resource_accesses: Any = None,
    effects: Any = None,
) -> ToolDefinition:
    """Create a structured ToolDefinition."""
    params = {
        key: (
            {k: v for k, v in spec.items() if not (k == "required" and isinstance(v, bool))}
            if isinstance(spec, dict)
            else spec
        )
        for key, spec in (parameters or {}).items()
    }
    req = list(required) if required is not None else []
    als = list(aliases) if aliases is not None else []

    # Infer required from parameters spec if property has required=True
    for key, spec in (parameters or {}).items():
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
        execution_mode=execution_mode,
        rate_limit=rate_limit,
        timeout_ms=timeout_ms,
        present_result=present_result,
        finalize_content=finalize_content,
        resource_accesses=resource_accesses,
        effects=effects,
    )

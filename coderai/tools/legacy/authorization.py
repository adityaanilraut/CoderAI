"""Prepare hook decisions and bind explicit runtime grants to one invocation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from coderai.soul.approval import parse_tool_arguments, parse_tool_call_for_permissions
from coderai.tools.legacy.types import (
    ToolCallAuthorization,
    matches_tool_call_binding,
    tool_call_binding,
)


def _argument_error(
    call: dict[str, Any],
    args: dict[str, Any],
    session_id: str,
    external_tools: list[dict[str, Any]] | None,
) -> str | None:
    from coderai.tools.legacy.registry import get_tool_registry
    from coderai.tools.legacy.schema import validate_json_schema_value
    from coderai.plugin.tool import find_plugin_tool

    registry = get_tool_registry()
    if not registry.admits(call["function"]["name"], session_id):
        return "Tool is masked for this session"
    tool = registry.get(call["function"]["name"], session_id)
    if tool:
        try:
            registry.validate_arguments(tool.name, args, session_id)
        except Exception as exc:
            return str(exc)
    else:
        definition = next(
            (
                d
                for d in external_tools or []
                if (d.get("function") or {}).get("name") == call["function"]["name"]
            ),
            None,
        )
        plugin = find_plugin_tool(call["function"]["name"]) if definition is None else None
        schema = (
            definition["function"]["parameters"]
            if definition
            else plugin[1].parameters
            if plugin
            else None
        )
        if schema:
            errors = validate_json_schema_value(schema, args)
            if errors:
                return "; ".join(errors)
    return None


def prepare_pre_tool_outcomes(
    session_id: str,
    project_root: str,
    tool_calls: list[Any],
    settings: dict[str, Any],
    external_tools: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run hooks before permission planning so an ask can reach the normal UI."""
    from coderai.hooks import HookPoint, run_hook_point

    snapshots: dict[str, dict[str, Any]] = {}
    parsed_calls = [call for raw in tool_calls if (call := parse_tool_call_for_permissions(raw))]
    counts = Counter(call["id"] for call in parsed_calls)
    for raw in parsed_calls:
        call = parse_tool_call_for_permissions(raw)
        if call is None or counts[call["id"]] != 1:
            continue
        args = parse_tool_arguments(call["function"]["arguments"])
        binding = tool_call_binding(session_id, project_root, call)
        invalid = _argument_error(call, args, session_id, external_tools)
        if invalid:
            snapshots[call["id"]] = {**binding, "outcome": {"decision": "deny", "reason": invalid}}
            continue
        outcome = run_hook_point(
            HookPoint.PRE_TOOL_USE,
            payload={
                "tool_name": call["function"]["name"],
                "tool_input": args,
                "session_id": session_id,
            },
            project_root=project_root,
            settings=settings,
        )
        snapshots[call["id"]] = {**binding, "outcome": outcome.to_dict()}
        if outcome.stop:
            break
    return snapshots


async def prepare_pre_tool_outcomes_async(
    session_id: str,
    project_root: str,
    tool_calls: list[Any],
    settings: dict[str, Any],
    external_tools: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Prepare invocation-bound hook grants without blocking the terminal loop."""
    from coderai.hooks import HookPoint, run_hook_point_async

    snapshots: dict[str, dict[str, Any]] = {}
    parsed_calls = [call for raw in tool_calls if (call := parse_tool_call_for_permissions(raw))]
    counts = Counter(call["id"] for call in parsed_calls)
    for call in parsed_calls:
        if counts[call["id"]] != 1:
            continue
        args = parse_tool_arguments(call["function"]["arguments"])
        binding = tool_call_binding(session_id, project_root, call)
        invalid = _argument_error(call, args, session_id, external_tools)
        if invalid:
            snapshots[call["id"]] = {**binding, "outcome": {"decision": "deny", "reason": invalid}}
            continue
        outcome = await run_hook_point_async(
            HookPoint.PRE_TOOL_USE,
            payload={
                "tool_name": call["function"]["name"],
                "tool_input": args,
                "session_id": session_id,
            },
            project_root=project_root,
            settings=settings,
        )
        snapshots[call["id"]] = {**binding, "outcome": outcome.to_dict()}
        if outcome.stop:
            break
    return snapshots


def build_tool_authorizations(
    session_id: str,
    project_root: str,
    tool_calls: list[Any],
    permission_replies: list[dict[str, Any]] | None,
    message_permissions: list[dict[str, Any]] | None,
    pre_tool_outcomes: dict[str, dict[str, Any]] | None,
) -> dict[str, ToolCallAuthorization]:
    """Only explicit replies to recorded asks can grant elevated capabilities."""
    from coderai.sandbox import parse_sandbox_mode

    authorizations: dict[str, ToolCallAuthorization] = {}
    parsed_calls = [call for raw in tool_calls if (call := parse_tool_call_for_permissions(raw))]
    counts = Counter(call["id"] for call in parsed_calls)
    for raw in parsed_calls:
        call = parse_tool_call_for_permissions(raw)
        if call is None or counts[call["id"]] != 1:
            continue
        args = parse_tool_arguments(call["function"]["arguments"])
        binding = tool_call_binding(session_id, project_root, call)
        snapshot = (pre_tool_outcomes or {}).get(call["id"]) or {}
        outcome = snapshot.get("outcome") if matches_tool_call_binding(snapshot, binding) else None
        request = next(
            (p for p in message_permissions or [] if p.get("toolCallId") == call["id"]), {}
        )
        reply = next((p for p in permission_replies or [] if p.get("toolCallId") == call["id"]), {})
        approved = (
            reply.get("permission") == "allow"
            and request.get("permission") != "deny"
            and matches_tool_call_binding(request, binding)
        )
        scopes = request.get("scopes") or []
        authorizations[call["id"]] = ToolCallAuthorization(
            session_id=session_id,
            tool_call_id=call["id"],
            tool_name=call["function"]["name"],
            project_root=str(Path(project_root).resolve()),
            args_digest=binding["args_digest"],
            sandbox_mode=(
                parse_sandbox_mode(args.get("sandbox_permissions"))
                if approved and "sandbox-escalation" in scopes
                else None
            ),
            hook_approved=bool(approved and "hook-approval" in scopes),
            plan_approved=bool(approved and "plan-approval" in scopes),
            plan_digest=binding.get("plan_digest") or None,
            hook_outcome=outcome if isinstance(outcome, dict) else None,
        )
    return authorizations

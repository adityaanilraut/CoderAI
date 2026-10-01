"""Child execution policy and callback context, independent of loop settlement."""

from __future__ import annotations

import asyncio
from typing import Any

from coderai.subagents.builder import SubAgentSpec
from coderai.subagents.registry import is_tool_allowed, get_subagent_definition
from coderai.tools.legacy.types import ToolExecutionHooks


def subagent_tool_denial(
    spec: SubAgentSpec,
    fn_name: str,
    permission_decision: str,
    is_mutating: bool,
) -> str | None:
    """Apply inherited, exclusion, allowlist and read-only policies in order."""
    if permission_decision != "allow":
        return (
            "PermissionDenied: Parent permission policy requires approval."
            if permission_decision == "ask"
            else "PermissionDenied: Parent permission policy denies this tool call."
        )
    if spec.exclude_tools and is_tool_allowed(fn_name, "allowlist", tuple(spec.exclude_tools)):
        return f"PermissionDenied: Tool '{fn_name}' is excluded by sub-agent policy."
    if spec.allowed_tools is not None and not is_tool_allowed(
        fn_name, "allowlist", tuple(spec.allowed_tools)
    ):
        return f"PermissionDenied: Tool '{fn_name}' is not permitted by sub-agent allowlist policy."
    if spec.mode == "read_only" and (
        fn_name in ("bash", "pwsh") and spec.sandbox_mode != "read-only"
    ):
        return f"PermissionDenied: Sub-agent in read_only mode cannot invoke shell tool '{fn_name}' outside read-only sandbox."
    if spec.mode == "read_only" and (
        fn_name
        in (
            "write",
            "Write",
            "edit",
            "Edit",
            "str_replace_editor",
            "schedule_create",
            "schedule_delete",
            "spawn_teammate",
        )
        or fn_name.startswith("terminal_")
        or (is_mutating and fn_name not in ("bash", "pwsh"))
    ):
        return f"PermissionDenied: Sub-agent in read_only mode cannot invoke mutating tool '{fn_name}'."
    return None


def writable_child_denial(parsed_args: Any) -> str | None:
    child_mode = parsed_args.get("mode") if isinstance(parsed_args, dict) else None
    child_type = parsed_args.get("subagent_type") if isinstance(parsed_args, dict) else None
    child_is_writable = False
    if child_mode == "general":
        child_is_writable = True
    elif not child_mode and child_type:
        c_def = get_subagent_definition(child_type)
        if c_def and c_def.mode != "read_only":
            child_is_writable = True
    elif not child_mode and not child_type:
        child_is_writable = True

    if child_is_writable:
        return (
            "PermissionDenied: Sub-agent in read_only mode cannot spawn a writable child sub-agent."
        )
    return None


def build_child_execution_hooks(
    spec: SubAgentSpec,
    abort_event: asyncio.Event,
    isolated_cwd: str,
    diffs: list[dict[str, Any]],
) -> ToolExecutionHooks:
    """Prepare callbacks without changing their order or ownership."""

    def after_file_mutation(file_path: str) -> None:
        diffs.append({"file_path": str(file_path)})
        if spec.on_after_file_mutation:
            spec.on_after_file_mutation(file_path)

    return ToolExecutionHooks(
        on_process_start=spec.on_process_start,
        on_process_exit=spec.on_process_exit,
        on_process_stdout=spec.on_process_stdout,
        on_process_timeout_control=spec.on_process_timeout_control,
        on_background_process_complete=spec.on_background_process_complete,
        should_stop=lambda: abort_event.is_set(),
        isolated_cwd=isolated_cwd,
        dry_run=spec.dry_run,
        on_before_file_mutation=spec.on_before_file_mutation,
        on_after_file_mutation=after_file_mutation,
        sandbox_mode=spec.sandbox_mode,
        plan_mode=spec.plan_mode,
        session_manager=spec.session_manager,
        allowed_tools=spec.allowed_tools,
    )

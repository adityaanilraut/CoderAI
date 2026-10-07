"""Trusted invocation effects shared by execution and tool inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_effects(tool: Any, args: dict[str, Any]) -> str:
    declared = getattr(tool, "effects", None)
    if declared is not None:
        try:
            value = declared(args) if callable(declared) else declared
            return (
                value
                if value in {"read", "session", "workspace", "external", "unknown"}
                else "unknown"
            )
        except Exception:
            return "unknown"
    return "unknown"


def builtin_effect_policy(tool_name: str, is_mutating: bool):
    return lambda args: _builtin_effects(tool_name, is_mutating, args)


def _builtin_effects(tool_name: str, is_mutating: bool, args: dict[str, Any]) -> str:
    name = tool_name
    if name == "str_replace_editor" and args.get("command") == "view":
        return "read"
    if name == "goal" and args.get("action") == "status":
        return "read"
    if name in {"Task", "subagent", "subagent_fork"}:
        if args.get("mode") == "read_only":
            return "read"
        return "workspace"
    if name in {"todo_write", "UpdatePlan", "SendDMail", "enter_plan_mode", "exit_plan_mode"}:
        return "session"
    if is_mutating:
        return "workspace"
    return "read"


def external_effects(name: str, context: Any) -> str:
    from coderai.config import resolve_current_settings

    manager = getattr(context, "session_manager", None)
    settings = (
        manager.get_resolved_settings()
        if manager and hasattr(manager, "get_resolved_settings")
        else resolve_current_settings(context.project_root)
    )
    policies = settings.get("toolPolicies") or {}
    policy = policies.get(name, {}) if isinstance(policies, dict) else {}
    value = policy.get("effects", "unknown") if isinstance(policy, dict) else "unknown"
    return value if value in {"read", "external", "workspace"} else "unknown"


def is_owned_plan_file(context: Any, args: dict[str, Any]) -> bool:
    from coderai.tools.plan.heroes import get_plan_file_path
    from types import SimpleNamespace

    if isinstance(context, dict):
        context = SimpleNamespace(
            session_id=context.get("session_id"),
            project_root=context.get("project_root", "."),
            isolated_cwd=context.get("isolated_cwd"),
        )

    raw = args.get("file_path") or args.get("path")
    if not raw and args.get("snippet_id"):
        from coderai.file_snippets import get_snippet

        snippet = get_snippet(context.session_id, args["snippet_id"])
        raw = snippet.file_path if snippet else None
    if not isinstance(raw, str) or not context.session_id:
        return False
    plan = get_plan_file_path(context.session_id, context.project_root, create=False)
    target = Path(raw)
    if not target.is_absolute():
        target = Path(context.isolated_cwd or context.project_root) / target
    original_root = Path(context.project_root).absolute()
    if target.absolute().is_relative_to(original_root):
        target = original_root.resolve() / target.absolute().relative_to(original_root)
    # Reject plan-directory/file symlinks too, rather than treating a symlink's
    # outside destination as an owned plan file.
    return target.absolute() == plan.absolute() and plan.resolve() == plan.absolute()


def team_scope(context: Any) -> tuple[str, str]:
    """Derive a root from runtime ancestry, never a model-supplied identifier."""
    from coderai.subagents.core import get_agent_registry

    registry = get_agent_registry()
    sid = context.session_id
    if not isinstance(sid, str) or not sid:
        raise ValueError("Team operations require a session owner")
    seen: set[str] = set()
    by_session = {h.run_session_id: h for h in registry.list() if h.run_session_id}
    while sid in by_session and sid not in seen:
        seen.add(sid)
        handle = by_session[sid]
        sid = handle.parent_session_id or sid
    manager_root = getattr(context.session_manager, "project_root", None)
    return str(Path(manager_root or context.project_root).resolve()), sid

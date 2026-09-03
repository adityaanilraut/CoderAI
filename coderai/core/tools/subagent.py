"""Sub-Agent Task tool handler for CoderAI."""

from __future__ import annotations

from typing import Any

from coderai.core.subagent import SubAgentManager, SubAgentSpec
from coderai.core.tools.types import ToolExecutionContext, ToolResult, as_str


async def handle_subagent_tool(args: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Execute a sub-agent task in an isolated child session."""
    description = as_str(args.get("description", "")).strip()
    prompt = as_str(args.get("prompt", "")).strip()

    if not description:
        return ToolResult(
            ok=False,
            name="Task",
            error="Missing required argument 'description'. Provide a 3-5 word summary of the sub-task.",
        )

    if not prompt:
        return ToolResult(
            ok=False,
            name="Task",
            error="Missing required argument 'prompt'. Provide detailed instructions and context for the sub-agent.",
        )

    mode = str(args.get("mode") or "read_only").strip().lower()
    if mode not in ("read_only", "general"):
        mode = "read_only"

    timeout_raw = args.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw is not None else 90.0
    except (ValueError, TypeError):
        timeout_seconds = 90.0

    if not context.create_openai_client:
        return ToolResult(
            ok=False,
            name="Task",
            error="SubAgentExecutionError: Client factory not available in execution context.",
        )

    manager = SubAgentManager(
        project_root=context.project_root,
        create_openai_client=context.create_openai_client,
    )

    from coderai.core.tools.agents import _derive_depth

    depth = _derive_depth(context, args)

    def _to_int(val: Any, default: int | None = None) -> int | None:
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    max_depth = _to_int(args.get("max_depth"), 3) or 3
    token_budget = _to_int(args.get("token_budget"))
    max_tokens = _to_int(args.get("max_tokens"))

    seed_messages = None
    if (args.get("fork_parent_history") is True or args.get("fork") is True) and context.session_id:
        from coderai.core.session_store import JsonlSessionStore

        store = JsonlSessionStore(context.project_root)
        rows = store.read_rows(context.session_id)
        if rows:
            seed_messages = []
            for r in rows:
                role = r.get("role") or (r.get("data") or {}).get("role")
                content = r.get("content") or (r.get("data") or {}).get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content:
                    seed_messages.append({"role": role, "content": content})

    spec = SubAgentSpec(
        description=description,
        prompt=prompt,
        mode=mode,
        depth=depth,
        max_depth=max_depth,
        token_budget=token_budget,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        parent_session_id=context.session_id,
        extra_context=as_str(args.get("context", "")).strip() or None,
        seed_messages=seed_messages,
    )

    if args.get("run_in_background") is True:
        from coderai.core.tools.agents import _start_subagent_job

        return _start_subagent_job(
            context=context,
            manager=manager,
            spec=spec,
            label=description,
            tool_name="Task",
        )

    result = await manager.spawn_subagent(spec)

    is_ok = result.status == "completed"
    return ToolResult(
        ok=is_ok,
        name="Task",
        output=result.format_markdown(),
        error=result.error if not is_ok else None,
        metadata=result.to_dict(),
    )


# Alias for handler discovery
handle = handle_subagent_tool

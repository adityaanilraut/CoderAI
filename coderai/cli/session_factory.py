"""Shared SessionManager construction for every CLI execution mode."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from coderai.llm import create_openai_client
from coderai.soul.session.manager import SessionManager
from coderai.soul.session.models import SessionMessage
from coderai.config import resolve_current_settings

AssistantCallback = Callable[[SessionMessage, bool], None]
ChunkCallback = Callable[[str], None]
ClientFactory = Callable[..., dict[str, Any]]


def build_session_manager(
    project_root: str,
    *,
    model: str | None = None,
    preset: str | None = None,
    agent: str | None = None,
    on_assistant_message: AssistantCallback | None = None,
    on_stream_chunk: ChunkCallback | None = None,
    on_thinking_chunk: ChunkCallback | None = None,
    non_interactive: bool = False,
    client_factory: ClientFactory = create_openai_client,
    mcp_servers: dict[str, dict[str, Any]] | None = None,
) -> SessionManager:
    """Build a manager with consistently resolved settings and model overrides.

    ``mcp_servers`` is a session-scoped overlay merged over the resolved
    ``mcpServers`` (global file → user → project → CLI/env). Prefer it over
    the process-global ``CODERAI_MCP_CONFIG_JSON`` escape hatch whenever the
    caller serves concurrent sessions (e.g. the ACP server).
    """
    resolved = resolve_current_settings(project_root)
    if model:
        resolved["model"] = model
    if preset:
        resolved["preset"] = preset
        resolved["toolsPreset"] = preset
    if mcp_servers:
        from coderai.mcp.files import merge_mcp_servers_dicts

        overlay = {k: dict(v) for k, v in mcp_servers.items() if isinstance(v, dict)}
        merged = merge_mcp_servers_dicts(resolved.get("mcpServers"), overlay or None)
        if merged:
            resolved["mcpServers"] = merged

    agent_target = agent or os.environ.get("CODERAI_AGENT_FILE") or os.environ.get("CODERAI_AGENT")
    if agent_target:
        try:
            from pathlib import Path
            from coderai.agentspec import render_system_prompt, resolve_agent_spec

            spec = resolve_agent_spec(agent_target, project_root=Path(project_root))
            rendered = render_system_prompt(spec)
            if rendered:
                resolved["persona"] = rendered
            if spec.model and not model:
                resolved["model"] = spec.model
                model = spec.model
            if spec.allowed_tools is not None:
                resolved["allowedTools"] = list(spec.allowed_tools)
        except Exception as exc:
            import sys

            print(f"Warning: Failed to load agent '{agent_target}': {exc}", file=sys.stderr)

    manager: SessionManager | None = None

    def create_client() -> dict[str, Any]:
        active_model = manager.get_active_model() if manager is not None else model
        if client_factory is create_openai_client and manager is not None:
            return client_factory(
                project_root, model_override=active_model, oauth=manager.oauth_manager
            )
        return client_factory(project_root, model_override=active_model)

    manager = SessionManager(
        project_root=project_root,
        create_openai_client=create_client,
        get_resolved_settings=lambda: resolved,
        render_markdown=lambda text: text,
        on_assistant_message=on_assistant_message,
        on_stream_chunk=on_stream_chunk,
        on_thinking_chunk=on_thinking_chunk,
        non_interactive=non_interactive,
    )
    from coderai.telemetry import apply_telemetry_policy

    apply_telemetry_policy(resolved)
    if model:
        manager.set_model(model)
    if agent_target:
        manager.active_agent_role = (
            getattr(spec, "name", str(agent_target))
            if "spec" in locals() and spec
            else str(agent_target)
        )
    return manager


async def close_session_manager(manager: SessionManager) -> None:
    """Close async resources before disposing the synchronous manager state."""
    agent_tasks = manager.cancel_owned_resources()
    if agent_tasks:
        await asyncio.gather(*agent_tasks, return_exceptions=True)
    manager.close_event_streams()

    try:
        from coderai.soul.session.approval import unregister_session_manager

        unregister_session_manager(manager)
    except Exception:
        pass

    executor = getattr(manager, "tool_executor", None)
    if executor is not None and hasattr(executor, "aclose"):
        await executor.aclose()
    await manager.mcp_manager.disconnect()

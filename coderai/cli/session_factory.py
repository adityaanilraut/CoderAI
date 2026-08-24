"""Shared SessionManager construction for every CLI execution mode."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from coderai.core.openai_client import create_openai_client
from coderai.core.session import SessionManager, SessionMessage
from coderai.core.settings import resolve_current_settings

AssistantCallback = Callable[[SessionMessage, bool], None]
ChunkCallback = Callable[[str], None]
ClientFactory = Callable[..., dict[str, Any]]


def build_session_manager(
    project_root: str,
    *,
    model: str | None = None,
    preset: str | None = None,
    on_assistant_message: AssistantCallback | None = None,
    on_stream_chunk: ChunkCallback | None = None,
    on_thinking_chunk: ChunkCallback | None = None,
    non_interactive: bool = False,
    client_factory: ClientFactory = create_openai_client,
) -> SessionManager:
    """Build a manager with consistently resolved settings and model overrides."""
    resolved = resolve_current_settings(project_root)
    if model:
        resolved["model"] = model
    if preset:
        resolved["preset"] = preset
        resolved["toolsPreset"] = preset

    manager: SessionManager | None = None

    def create_client() -> dict[str, Any]:
        active_model = manager.get_active_model() if manager is not None else model
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
    if model:
        manager.set_model(model)
    return manager


async def close_session_manager(manager: SessionManager) -> None:
    """Close async resources before disposing the synchronous manager state."""
    for event in manager.session_controllers.values():
        event.set()

    agent_tasks = []
    for handle in manager.agent_registry.list():
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            agent_tasks.append(handle.task)
    if agent_tasks:
        await asyncio.gather(*agent_tasks, return_exceptions=True)

    for job in list(manager.job_store._jobs.values()):
        if job.status in ("running", "stopping"):
            manager.job_store.kill(job.id, job.session_id, reason="CoderAI is shutting down")

    from coderai.core.lsp import client as lsp_module
    from coderai.core.terminal import manager as terminal_module

    terminal_manager = terminal_module._default_terminal_manager
    if terminal_manager is not None:
        terminal_manager.close_all()

    lsp_client = lsp_module._default_lsp_client
    if lsp_client is not None:
        instances = list(lsp_client._instances.values())
        lsp_client._instances.clear()
        if instances:
            await asyncio.gather(
                *(instance.close() for instance in instances),
                return_exceptions=True,
            )

    await manager.mcp_manager.disconnect()

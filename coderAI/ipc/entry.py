"""Headless entry point invoked by the Ink UI (``python -m coderAI.ipc.entry``).

This replaces the Rich-based interactive loop with the IPC server so that the
TypeScript front-end can drive the agent over stdio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from ..agent import Agent
from ..agent_tracker import AgentStatus, agent_tracker
from .jsonrpc_server import IPCServer
from .streaming import IPCStreamingHandler


def _configure_logging() -> None:
    """Send all Python logs to stderr so they don't corrupt the stdout NDJSON."""
    level = os.environ.get("CODERAI_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Redirect stdout to stderr so stray third-party print() calls don't
    # corrupt the NDJSON stream on fd 1. The JSON-RPC server writes directly
    # to sys.__stdout__, so it is unaffected by this global mutation.
    sys.stdout = sys.stderr


def _activate_resumed_session_model(agent: Agent, requested_model: str | None) -> None:
    """Restore or override the active model after loading a saved session."""
    session = getattr(agent, "session", None)
    if session is None:
        return

    effective_model = requested_model or session.model or agent.model
    if agent.model != effective_model:
        agent.model = effective_model
        agent.provider = agent._create_provider()

    session.model = effective_model
    agent.realign_provider_usage_counters()
    agent._configure_delegate_tool_context()


async def _main() -> None:
    _configure_logging()

    model = os.environ.get("CODERAI_MODEL") or None
    auto_approve = os.environ.get("CODERAI_AUTO_APPROVE") == "1"
    resume_id = os.environ.get("CODERAI_RESUME") or None
    continue_ = os.environ.get("CODERAI_CONTINUE") == "1"

    if continue_ and not resume_id:
        from ..history import history_manager
        resume_id = history_manager.get_latest_session_id()

    agent = Agent(
        model=model,
        streaming=True,
        auto_approve=auto_approve,
    )

    # Resume an existing session when the CLI passed --resume; fall back to a
    # fresh session when the id is missing or unknown so the UI can still boot.
    if resume_id:
        try:
            session = agent.load_session(resume_id)
        except Exception:
            session = None
            logging.getLogger(__name__).exception(
                "Failed to load session %s; starting fresh", resume_id
            )
        if session is None:
            logging.getLogger(__name__).warning(
                "Resume id %r not found; starting new session", resume_id
            )
            agent.create_session()
        else:
            _activate_resumed_session_model(agent, model)
    else:
        agent.create_session()

    server = IPCServer(agent=agent)
    agent.ipc_server = server
    agent._configure_delegate_tool_context()

    # Seed the tracker with an idle root entry so the UI's Agents panel shows
    # the main agent from boot, not only after the first turn registers it.
    agent.tracker_info = agent_tracker.register(
        name=agent.persona.name if agent.persona else "main",
        role=agent.persona.description if agent.persona else None,
        model=agent.model,
        context_limit=agent.config.context_window,
    )
    agent.tracker_info.status = AgentStatus.IDLE
    agent._tracker_start_completion = agent.total_completion_tokens
    agent._tracker_start_tokens = agent.total_tokens
    agent._tracker_start_cost = agent.cost_tracker.get_total_cost()

    # Redirect the streaming handler so token deltas become NDJSON events
    # instead of Rich console prints.
    agent.streaming_handler = IPCStreamingHandler(server)

    async def _parent_watchdog() -> None:
        while not server._exit.is_set():
            await asyncio.sleep(5)
            try:
                ppid = os.getppid()
                if ppid == 1:
                    logging.getLogger(__name__).warning("Parent process is PID 1; shutting down IPC server.")
                    server._exit.set()
                    return
                os.kill(ppid, 0)
            except OSError:
                logging.getLogger(__name__).warning("Parent process disappeared; shutting down IPC server.")
                server._exit.set()
                return

    try:
        watchdog = asyncio.create_task(_parent_watchdog())
        try:
            await server.run()
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except Exception:
                pass
    finally:
        try:
            await agent.close()
        except Exception:
            pass


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

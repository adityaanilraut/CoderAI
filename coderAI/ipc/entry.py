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


async def _main() -> None:
    _configure_logging()

    model = os.environ.get("CODERAI_MODEL") or None
    auto_approve = os.environ.get("CODERAI_AUTO_APPROVE") == "1"
    resume_id = os.environ.get("CODERAI_RESUME") or None

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
        agent.create_session()

    server = IPCServer(agent=agent)

    # Redirect the streaming handler so token deltas become NDJSON events
    # instead of Rich console prints.
    agent.streaming_handler = IPCStreamingHandler(server)

    try:
        await server.run()
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

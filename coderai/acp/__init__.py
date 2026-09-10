# Ported from kimi_cli/acp/__init__.py - kimi structure.
"""Agent Control Protocol (ACP) server package."""

from __future__ import annotations


def acp_main() -> None:
    """Entry point for the multi-session ACP server."""
    import asyncio

    import acp

    from coderai.acp.server import ACPServer
    from coderai.app import enable_logging
    from coderai.utils.logging import logger

    enable_logging()
    logger.info("Starting ACP server on stdio")
    asyncio.run(acp.run_agent(ACPServer(), use_unstable_protocol=True))


from coderai.acp.server import ACPServer

__all__ = ["ACPServer", "acp_main"]

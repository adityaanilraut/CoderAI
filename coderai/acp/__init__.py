"""Agent Control Protocol (ACP) server package."""

from __future__ import annotations


def acp_main() -> None:
    """Entry point for the multi-session ACP server."""
    import asyncio

    import acp

    from coderai.acp.server import ACPServer
    from coderai.log import enable_logging
    from coderai.utils.logging import logger

    enable_logging(redirect_stderr=False)
    logger.info("Starting ACP server on stdio")

    async def serve() -> None:
        server = ACPServer()
        try:
            await acp.run_agent(server, use_unstable_protocol=True)
        finally:
            await server.close()

    asyncio.run(serve())


from coderai.acp.server import ACPServer

__all__ = ["ACPServer", "acp_main"]

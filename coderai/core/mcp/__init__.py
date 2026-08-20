"""MCP support — dynamic tool registry over stdio and SSE servers."""

from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager
from coderai.core.mcp.transport import (
    McpTransport,
    SseMcpTransport,
    StdioMcpTransport,
    StreamableHttpMcpTransport,
)

__all__ = [
    "McpClient",
    "McpManager",
    "McpTransport",
    "SseMcpTransport",
    "StdioMcpTransport",
    "StreamableHttpMcpTransport",
]

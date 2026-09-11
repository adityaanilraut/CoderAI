"""MCP support — dynamic tool registry over stdio and SSE servers."""

from coderai.mcp.client import McpClient
from coderai.mcp.manager import McpManager
from coderai.mcp.transport import (
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

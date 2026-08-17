"""MCP support — dynamic tool registry over stdio servers."""

from coderai.core.mcp.client import McpClient  # noqa: F401
from coderai.core.mcp.manager import McpManager  # noqa: F401

__all__ = ["McpClient", "McpManager"]

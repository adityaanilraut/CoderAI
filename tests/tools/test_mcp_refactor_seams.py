"""Focused contracts for the responsibility split behind tools.mcp."""

from coderAI.tools.mcp import MCPClient, MCPConnectTool, normalize_parameters_schema_ex
from coderAI.tools.mcp_discovery import MCPCatalog
from coderAI.tools.mcp_native_tools import MCPConnectTool as NativeMCPConnectTool
from coderAI.tools.mcp_remote_transport import MCPRemoteTransport
from coderAI.tools.mcp_session import MCPSession
from coderAI.tools.mcp_stdio_transport import MCPStdioTransport


def test_public_client_composes_transport_session_and_catalog_seams() -> None:
    assert issubclass(MCPClient, MCPSession)
    assert issubclass(MCPClient, MCPCatalog)
    assert issubclass(MCPClient, MCPStdioTransport)
    assert issubclass(MCPClient, MCPRemoteTransport)
    assert "connect_stdio" in MCPStdioTransport.__dict__
    assert "connect_http" in MCPRemoteTransport.__dict__
    assert "disconnect" in MCPSession.__dict__
    assert "get_tools_as_openai_format" in MCPCatalog.__dict__


def test_public_native_tool_and_schema_imports_remain_compatible() -> None:
    assert MCPConnectTool is NativeMCPConnectTool
    schema, wrapped = normalize_parameters_schema_ex({"type": "string"})
    assert wrapped is True
    assert schema["properties"]["value"] == {"type": "string"}

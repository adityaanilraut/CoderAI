# mypy: disable-error-code="no-any-return"
"""Native CoderAI tools that manage MCP sessions and resources."""

from typing import Any, Optional

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.types.provenance import Provenance
from coderAI.types.tool_error_codes import ToolErrorCode


def _mcp() -> Any:
    from coderAI.tools import mcp

    return mcp


class MCPConnectParams(BaseModel):
    server_name: str = Field(..., description="Friendly name for this server connection")
    command: str = Field(
        "", description="Command to start the MCP server (e.g., 'npx'), for stdio transport"
    )
    args: Optional[list[str]] = Field(None, description="Arguments for the server command")
    transport: str = Field(
        "stdio",
        description="Transport type: 'stdio', 'sse', or 'http' (Streamable HTTP). Default: stdio",
    )
    url: Optional[str] = Field(
        None,
        description=(
            "Endpoint URL for remote transports — SSE (e.g. http://host:port/sse) "
            "or Streamable HTTP (e.g. https://host/mcp)."
        ),
    )
    headers: Optional[dict[str, str]] = Field(
        None,
        description=(
            "Extra HTTP headers (e.g. {'Authorization': 'Bearer …'}) sent on every "
            "request for the 'http' transport — used for token-authenticated servers."
        ),
    )
    env: Optional[dict[str, str]] = Field(
        None,
        description=(
            "Environment variables for stdio servers (applied after scrubbing). "
            "Values may use ${VAR} or ${VAR:-default} expansion."
        ),
    )
    cwd: Optional[str] = Field(
        None,
        description="Working directory for stdio servers (relative paths resolve from the project root).",
    )
    timeout: Optional[int] = Field(
        None,
        description="Per-request timeout in milliseconds for initialize/tools (stdio).",
    )
    persist: bool = Field(
        True,
        description=(
            "Save this server to ~/.coderAI/mcp_servers.json so it auto-reconnects "
            "in future sessions. Set false for a one-off, session-only connection."
        ),
    )


class MCPConnectTool(Tool):
    """Tool for connecting to MCP servers via stdio, SSE, or Streamable HTTP transport."""

    name = "mcp_connect"
    description = "Connect to an MCP (Model Context Protocol) server to discover and use its tools"
    category = "mcp"
    parameters_model = MCPConnectParams
    requires_confirmation = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True
    # url/headers are an outbound channel (they can carry exfiltrated data to an
    # attacker-chosen endpoint), so this control-plane call performs egress.
    is_egress = True

    async def execute(  # type: ignore[override]
        self,
        server_name: str,
        command: str = "",
        args: Optional[list[str]] = None,
        transport: str = "stdio",
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Connect to an MCP server."""
        from coderAI.tools.mcp_config import parse_timeout_ms

        if transport == "sse":
            if not url:
                return {"success": False, "error": "URL is required for SSE transport"}
            result = await _mcp().mcp_client.connect_sse(server_name, url)
            if result.get("success") and persist:
                _mcp().persist_mcp_server(server_name, {"transport": "sse", "url": url})
            return result
        if transport == "http":
            if not url:
                return {"success": False, "error": "URL is required for HTTP transport"}
            result = await _mcp().mcp_client.connect_http(server_name, url, headers)
            if result.get("success") and persist:
                entry: dict[str, Any] = {"transport": "http", "url": url}
                if headers:
                    entry["headers"] = headers
                _mcp().persist_mcp_server(server_name, entry)
            return result
        # Launcher allow-list, inline-exec block, blocklist and interactive checks
        # all live in ``connect_stdio`` (via ``validate_stdio_launch``) so this
        # LLM-driven path and config-driven autoconnect share one choke point.
        timeout_s = parse_timeout_ms({"timeout": timeout} if timeout else {}, default_s=10.0)
        result = await _mcp().mcp_client.connect_stdio(
            server_name, command, args, env=env, cwd=cwd, timeout=timeout_s
        )
        if result.get("success") and persist:
            saved: dict[str, Any] = {"command": command, "args": list(args or [])}
            if env:
                saved["env"] = env
            if cwd:
                saved["cwd"] = cwd
            if timeout:
                saved["timeout"] = timeout
            _mcp().persist_mcp_server(server_name, saved)
        return result


class MCPListParams(BaseModel):
    pass


class MCPListTool(Tool):
    """Tool for listing connected MCP servers and their tools."""

    name = "mcp_list"
    description = "List all connected MCP servers and discovered tools"
    category = "mcp"
    parameters_model = MCPListParams
    is_read_only = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True

    async def execute(self) -> dict[str, Any]:  # type: ignore[override]
        """List MCP servers and tools (live connections + effective config)."""
        configured = _mcp().effective_mcp_servers().get("mcpServers", {})
        servers = {}
        for name, info in _mcp().mcp_client.servers.items():
            servers[name] = {
                "connected": True,
                "degraded": bool(info.get("degraded")),
                "disabled": bool(configured.get(name, {}).get("disabled")),
                "tools": [t.get("name") for t in info.get("tools", [])],
                "resources": [
                    r.get("uri")
                    for r in _mcp().mcp_client.discovered_resources
                    if r.get("server") == name
                ],
                "prompts": [
                    p.get("name")
                    for p in _mcp().mcp_client.discovered_prompts
                    if p.get("server") == name
                ],
                "server_info": info.get("server_info", {}),
            }

        # Surface saved servers that aren't currently connected so the list is
        # never misleadingly empty when a persisted server failed to autoconnect.
        for name, cfg in configured.items():
            if name in servers:
                continue
            servers[name] = {
                "connected": False,
                "disabled": bool(cfg.get("disabled")),
                "transport": cfg.get("transport", "stdio"),
                "tools": [],
            }

        connected = sum(1 for s in servers.values() if s.get("connected"))
        return {
            "success": True,
            "connected_servers": connected,
            "configured_servers": len(configured),
            "servers": servers,
            "total_tools": len(_mcp().mcp_client.discovered_tools),
            "total_resources": len(_mcp().mcp_client.discovered_resources),
            "total_prompts": len(_mcp().mcp_client.discovered_prompts),
        }


class MCPDisconnectParams(BaseModel):
    server_name: str = Field(..., description="Name of the MCP server to disconnect from")


class MCPDisconnectTool(Tool):
    """Disconnect from a connected MCP server."""

    name = "mcp_disconnect"
    description = "Disconnect from a connected MCP server and free its resources"
    category = "mcp"
    parameters_model = MCPDisconnectParams
    is_read_only = False
    requires_confirmation = True

    async def execute(self, server_name: str) -> dict[str, Any]:  # type: ignore[override]
        try:
            return await _mcp().mcp_client.disconnect(server_name)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class MCPListResourcesParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")


class MCPListResourcesTool(Tool):
    """List resources exposed by a connected MCP server."""

    name = "mcp_list_resources"
    description = "List resources (files, data) exposed by a connected MCP server"
    category = "mcp"
    parameters_model = MCPListResourcesParams
    is_read_only = True
    # Resource URIs/names come from the third-party server → untrusted. No
    # is_egress: the only argument is server_name, so there is no payload channel.
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True

    async def execute(self, server_name: str) -> dict[str, Any]:  # type: ignore[override]
        return await _mcp().mcp_client.list_resources(server_name)


class MCPReadResourceParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")
    uri: str = Field(..., description="URI of the resource to read (from mcp_list_resources)")


class MCPReadResourceTool(Tool):
    """Read the contents of a resource from a connected MCP server."""

    name = "mcp_read_resource"
    description = "Read the contents of a resource (by URI) from a connected MCP server"
    category = "mcp"
    parameters_model = MCPReadResourceParams
    is_read_only = True
    # Returns raw resource content from the third-party server → untrusted, and
    # the uri argument is an outbound channel.
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    is_egress = True
    mcp_source = True

    async def execute(self, server_name: str, uri: str) -> dict[str, Any]:  # type: ignore[override]
        return await _mcp().mcp_client.read_resource(server_name, uri)


class MCPListPromptsParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")


class MCPListPromptsTool(Tool):
    """List prompt templates exposed by a connected MCP server."""

    name = "mcp_list_prompts"
    description = "List prompt templates exposed by a connected MCP server"
    category = "mcp"
    parameters_model = MCPListPromptsParams
    is_read_only = True
    # Prompt names/metadata come from the third-party server → untrusted. No
    # is_egress: the only argument is server_name.
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True

    async def execute(self, server_name: str) -> dict[str, Any]:  # type: ignore[override]
        return await _mcp().mcp_client.list_prompts(server_name)


class MCPGetPromptParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")
    name: str = Field(..., description="Name of the prompt to fetch (from mcp_list_prompts)")
    arguments: Optional[dict[str, Any]] = Field(
        None, description="Arguments to fill into the prompt template"
    )


class MCPGetPromptTool(Tool):
    """Fetch a prompt template (with arguments filled in) from a connected MCP server."""

    name = "mcp_get_prompt"
    description = "Fetch a prompt template (with arguments filled in) from a connected MCP server"
    category = "mcp"
    parameters_model = MCPGetPromptParams
    is_read_only = True
    # Returns raw prompt content from the third-party server → untrusted, and the
    # arguments are an outbound channel.
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    is_egress = True
    mcp_source = True

    async def execute(  # type: ignore[override]
        self, server_name: str, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        return await _mcp().mcp_client.get_prompt(server_name, name, arguments or {})

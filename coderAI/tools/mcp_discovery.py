# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""MCP discovery catalog, resource/prompt access, and schema shaping."""

import logging
from typing import Any, Optional

from coderAI.core.tool_routing import build_mcp_function_name
from coderAI.tools.mcp import (
    MCP_MAX_DESCRIPTION_LENGTH,
    MCP_MAX_LIST_ITEMS,
    MCP_MAX_PAGES,
    WRAPPED_ARG_KEY,
    _sanitize_metadata_text,
    _sanitize_model_metadata,
    _shape_tool_result,
    _validate_discovered_tools,
)

logger = logging.getLogger("coderAI.tools.mcp")


def _normalize_parameters_schema(schema: Any) -> dict[str, Any]:
    """Ensure JSON Schema is OpenAI-tool friendly (object root with properties)."""
    normalized, _ = normalize_parameters_schema_ex(schema)
    return normalized


def normalize_parameters_schema_ex(schema: Any) -> tuple[dict[str, Any], bool]:
    """Return ``(openai_schema, wrapped)`` for an MCP ``inputSchema``.

    ``wrapped`` is ``True`` when a non-object root had to be nested under
    :data:`WRAPPED_ARG_KEY` to satisfy providers that only accept object-rooted
    parameters. Callers dispatching a tool call need that bit to undo the nesting
    (see :meth:`MCPClient._unwrap_tool_arguments`) — otherwise the model's
    ``{"value": …}`` reply reaches a server that never asked for it.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}, False
    out = dict(schema)
    if out.get("type") is None and "properties" in out:
        out["type"] = "object"
    if out.get("type") != "object":
        # Non-object roots (e.g. union) — wrap for providers expecting object args
        return {"type": "object", "properties": {WRAPPED_ARG_KEY: out}}, True
    if "properties" not in out:
        out["properties"] = {}
    return out, False


class MCPCatalog:
    discovered_tools: list[dict[str, Any]]
    discovered_resources: list[dict[str, Any]]
    discovered_prompts: list[dict[str, Any]]

    async def _rediscover_server(self, server_name: str, entry: dict[str, Any]) -> None:
        """Re-run tools/resources/prompts discovery after a list_changed notification."""
        raw_tools = await self._paginate_entry(server_name, entry, "tools/list", "tools")
        server_tools = _validate_discovered_tools(server_name, raw_tools, self.discovered_tools)
        staged_tools = [
            {
                "server": server_name,
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["inputSchema"],
            }
            for tool in server_tools
        ]
        entry["tools"] = server_tools
        staged_resources, staged_prompts = await self._discover_extras_for_entry(server_name, entry)
        self.discovered_tools = [
            tool for tool in self.discovered_tools if tool.get("server") != server_name
        ] + staged_tools
        self.discovered_resources = [
            resource
            for resource in self.discovered_resources
            if resource.get("server") != server_name
        ] + staged_resources
        self.discovered_prompts = [
            prompt for prompt in self.discovered_prompts if prompt.get("server") != server_name
        ] + staged_prompts
        logger.info(
            "Refreshed MCP server '%s' after list_changed (%d tools)",
            server_name,
            len(server_tools),
        )

    def _init_request(self, init_id: int, protocol_version: Optional[str] = None) -> dict[str, Any]:
        """The MCP ``initialize`` request (JSON-RPC 2.0), shared by all transports."""
        version = protocol_version or self.PREFERRED_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {
                    "roots": {"listChanged": True},
                    "elicitation": {},
                    "sampling": {},
                },
                "clientInfo": {"name": "CoderAI", "version": "0.1.0"},
            },
        }

    @staticmethod
    def _response_result(response: Any, method: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise RuntimeError(f"MCP {method} returned a non-object response")
        error = response.get("error")
        if error:
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"MCP {method} failed: {message}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {method} returned a non-object result")
        return result

    async def _paginate_entry(
        self,
        server_name: str,
        entry: dict[str, Any],
        method: str,
        item_key: str,
        first_response: Optional[dict[str, Any]] = None,
    ) -> list[Any]:
        """Collect a cursor-based MCP list with hard page and item limits."""
        items: list[Any] = []
        cursor: Optional[str] = None
        seen_cursors = set()
        for page in range(MCP_MAX_PAGES):
            if page == 0 and first_response is not None:
                result = self._response_result(first_response, method)
            else:
                params = {"cursor": cursor} if cursor is not None else None
                response = await self._request_entry(server_name, entry, method, params)
                if not response.get("success"):
                    raise RuntimeError(str(response.get("error", f"MCP {method} failed")))
                result = response.get("result", {})

            page_items = result.get(item_key, [])
            if not isinstance(page_items, list):
                raise RuntimeError(f"MCP {method} result field {item_key!r} must be an array")
            if len(items) + len(page_items) > MCP_MAX_LIST_ITEMS:
                raise RuntimeError(
                    f"MCP {method} exceeded the {MCP_MAX_LIST_ITEMS}-item discovery limit"
                )
            items.extend(page_items)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor:
                raise RuntimeError(f"MCP {method} returned an invalid nextCursor")
            if next_cursor in seen_cursors:
                raise RuntimeError(f"MCP {method} repeated pagination cursor {next_cursor!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(f"MCP {method} exceeded the {MCP_MAX_PAGES}-page discovery limit")

    async def _finish_connect(
        self,
        server_name: str,
        entry: dict[str, Any],
        init_response: dict[str, Any],
        tools_response: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and stage discovery, then atomically replace same-name state."""
        init_result = _sanitize_model_metadata(self._response_result(init_response, "initialize"))
        raw_tools = await self._paginate_entry(
            server_name, entry, "tools/list", "tools", first_response=tools_response
        )
        server_tools = _validate_discovered_tools(server_name, raw_tools, self.discovered_tools)
        staged_tools = [
            {
                "server": server_name,
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["inputSchema"],
            }
            for tool in server_tools
        ]
        entry["tools"] = server_tools
        entry["server_info"] = init_result
        staged_resources, staged_prompts = await self._discover_extras_for_entry(server_name, entry)

        old_entry = self.servers.get(server_name)
        self.servers[server_name] = entry
        self.discovered_tools = [
            tool for tool in self.discovered_tools if tool.get("server") != server_name
        ] + staged_tools
        self.discovered_resources = [
            resource
            for resource in self.discovered_resources
            if resource.get("server") != server_name
        ] + staged_resources
        self.discovered_prompts = [
            prompt for prompt in self.discovered_prompts if prompt.get("server") != server_name
        ] + staged_prompts

        if old_entry is not None and old_entry is not entry:
            try:
                await self._close_server_entry(old_entry)
            except Exception:
                logger.warning(
                    "Failed to close replaced MCP server '%s'", server_name, exc_info=True
                )

        out: dict[str, Any] = {
            "success": True,
            "server": server_name,
            "tools_discovered": len(server_tools),
            "resources_discovered": len(staged_resources),
            "prompts_discovered": len(staged_prompts),
            "tools": [tool["name"] for tool in server_tools],
            "server_info": init_result.get("serverInfo", {}),
        }
        if entry["transport"] != "stdio":
            out["transport"] = entry["transport"]
        return out

    def _unwrap_tool_arguments(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        """Undo the ``{"value": …}`` nesting applied to a non-object input schema.

        ``get_tools_as_openai_format`` nests a non-object schema root under
        :data:`WRAPPED_ARG_KEY` because providers only accept object-rooted
        parameters, so the model answers with ``{"value": …}``. The server never
        advertised that property and would reject it, so peel the wrapper back off
        for exactly the tools whose advertised schema was rewritten.
        """
        if not isinstance(arguments, dict) or set(arguments) != {WRAPPED_ARG_KEY}:
            return arguments
        for item in self.discovered_tools:
            if item.get("server") == server_name and item.get("name") == tool_name:
                _, wrapped = normalize_parameters_schema_ex(item.get("input_schema"))
                if wrapped:
                    return arguments[WRAPPED_ARG_KEY]
                break
        return arguments

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on a connected MCP server.

        Dispatches over the connected transport via :meth:`_request` and shapes
        the reply with :func:`_shape_tool_result`.
        """
        payload = self._unwrap_tool_arguments(server_name, tool_name, arguments)
        res = await self._request(
            server_name, "tools/call", {"name": tool_name, "arguments": payload}
        )
        if not res["success"]:
            return res
        return _shape_tool_result(res["result"])

    def _capabilities(self, server_name: str) -> dict[str, Any]:
        """Return the server's advertised capabilities from the initialize reply."""
        server = self.servers.get(server_name, {})
        caps = (server.get("server_info") or {}).get("capabilities", {})
        return caps if isinstance(caps, dict) else {}

    async def list_resources(self, server_name: str) -> dict[str, Any]:
        """List resources exposed by a connected server (``resources/list``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "resources" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise resource support",
            }
        try:
            resources = await self._paginate_entry(
                server_name, self.servers[server_name], "resources/list", "resources"
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        resources = _sanitize_model_metadata(resources)
        return {
            "success": True,
            "server": server_name,
            "count": len(resources),
            "resources": resources,
        }

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        """Read the contents of a resource (``resources/read``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "resources" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise resource support",
            }
        resp = await self._request(server_name, "resources/read", {"uri": uri})
        if not resp.get("success"):
            return resp
        contents = resp["result"].get("contents", [])
        text = "".join(c.get("text", "") for c in contents if isinstance(c, dict) and c.get("text"))
        return {
            "success": True,
            "server": server_name,
            "uri": uri,
            "contents": contents,
            "text": text,
        }

    async def list_prompts(self, server_name: str) -> dict[str, Any]:
        """List prompt templates exposed by a connected server (``prompts/list``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "prompts" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise prompt support",
            }
        try:
            prompts = await self._paginate_entry(
                server_name, self.servers[server_name], "prompts/list", "prompts"
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        prompts = _sanitize_model_metadata(prompts)
        return {"success": True, "server": server_name, "count": len(prompts), "prompts": prompts}

    async def get_prompt(
        self, server_name: str, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Fetch a prompt template with arguments filled in (``prompts/get``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "prompts" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise prompt support",
            }
        resp = await self._request(
            server_name, "prompts/get", {"name": name, "arguments": arguments or {}}
        )
        if not resp.get("success"):
            return resp
        result = resp["result"]
        return {
            "success": True,
            "server": server_name,
            "prompt": name,
            "description": result.get("description", ""),
            "messages": result.get("messages", []),
        }

    async def _discover_extras_for_entry(
        self, server_name: str, entry: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Stage bounded resource/prompt metadata without mutating global state."""
        caps = (entry.get("server_info") or {}).get("capabilities", {})
        caps = caps if isinstance(caps, dict) else {}
        resources: list[dict[str, Any]] = []
        prompts: list[dict[str, Any]] = []
        if "resources" in caps:
            try:
                discovered = await self._paginate_entry(
                    server_name, entry, "resources/list", "resources"
                )
                for raw in _sanitize_model_metadata(discovered):
                    if isinstance(raw, dict):
                        resources.append(
                            {
                                "server": server_name,
                                "uri": raw.get("uri", ""),
                                "name": raw.get("name", ""),
                                "description": raw.get("description", ""),
                                "mimeType": raw.get("mimeType", ""),
                            }
                        )
            except Exception:
                logger.debug("resource discovery failed for '%s'", server_name, exc_info=True)
        if "prompts" in caps:
            try:
                discovered = await self._paginate_entry(
                    server_name, entry, "prompts/list", "prompts"
                )
                for raw in _sanitize_model_metadata(discovered):
                    if isinstance(raw, dict):
                        prompts.append(
                            {
                                "server": server_name,
                                "name": raw.get("name", ""),
                                "description": raw.get("description", ""),
                                "arguments": raw.get("arguments", []),
                            }
                        )
            except Exception:
                logger.debug("prompt discovery failed for '%s'", server_name, exc_info=True)
        return resources, prompts

    def get_tools_as_openai_format(self) -> list[dict[str, Any]]:
        """Get discovered MCP tools in OpenAI function-calling format.

        Returns:
            List of tool definitions compatible with OpenAI's API
        """
        tools = []
        for tool in self.discovered_tools:
            params = tool.get("input_schema")
            params = _normalize_parameters_schema(params)
            function_name = build_mcp_function_name(str(tool["server"]), str(tool["name"]))
            description = _sanitize_metadata_text(tool.get("description", ""))
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": (
                            f"[Untrusted MCP metadata: {tool['server']}] {description}"
                        )[:MCP_MAX_DESCRIPTION_LENGTH],
                        "parameters": params,
                    },
                }
            )
        return tools

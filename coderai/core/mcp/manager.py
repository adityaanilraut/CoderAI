"""McpManager — manages MCP server lifecycle, namespaces tools, and merges definitions (deepcode mcp-manager.ts)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from coderai.core.mcp.client import McpClient
from coderai.core.tools.types import ToolResult

API_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
API_TOOL_NAME_MAX_LENGTH = 64


@dataclass
class McpToolEntry:
    server_name: str
    original_name: str
    namespaced_name: str
    definition: dict[str, Any]
    client: McpClient


@dataclass
class McpServerStatus:
    name: str
    status: str  # "starting" | "ready" | "failed" | "reconnecting"
    connected: bool
    error: str | None = None
    tool_count: int = 0
    tools: list[str] = field(default_factory=list)
    prompt_count: int = 0
    prompts: list[str] = field(default_factory=list)
    resource_count: int = 0
    resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "connected": self.connected,
            "toolCount": self.tool_count,
            "tools": self.tools,
            "promptCount": self.prompt_count,
            "prompts": self.prompts,
            "resourceCount": self.resource_count,
            "resources": self.resources,
        }
        if self.error:
            d["error"] = self.error
        return d


def sanitize_api_tool_name_part(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", value)
    return sanitized or "unnamed"


def hash_tool_name(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def fit_api_tool_name_with_suffix(name: str, suffix: str) -> str:
    max_prefix = API_TOOL_NAME_MAX_LENGTH - len(suffix)
    prefix = name[: max(1, max_prefix)]
    return f"{prefix}{suffix}"


def fit_api_tool_name(name: str, raw_name: str) -> str:
    if API_TOOL_NAME_PATTERN.match(name) and len(name) <= API_TOOL_NAME_MAX_LENGTH:
        return name
    return fit_api_tool_name_with_suffix(name, f"_{hash_tool_name(raw_name)}")


def build_raw_mcp_namespaced_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{server_name}__{tool_name}"


def build_mcp_namespaced_name(
    server_name: str, tool_name: str, used_names: set[str] | None = None
) -> str:
    used = used_names or set()
    raw_name = build_raw_mcp_namespaced_name(server_name, tool_name)
    sanitized_name = (
        f"mcp__{sanitize_api_tool_name_part(server_name)}__{sanitize_api_tool_name_part(tool_name)}"
    )
    candidate = fit_api_tool_name(sanitized_name, raw_name)
    if candidate not in used:
        return candidate

    h = hash_tool_name(raw_name)
    candidate = fit_api_tool_name_with_suffix(sanitized_name, f"_{h}")
    if candidate not in used:
        return candidate

    idx = 2
    while True:
        candidate = fit_api_tool_name_with_suffix(sanitized_name, f"_{h}_{idx}")
        if candidate not in used:
            return candidate
        idx += 1


class McpManager:
    def __init__(self) -> None:
        self.clients: list[McpClient] = []
        self.tools: list[McpToolEntry] = []
        self.prompts: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self.server_statuses: list[McpServerStatus] = []
        self.configured_server_names: list[str] = []
        self.server_configs: dict[str, dict[str, Any]] = {}
        self.initialized = False
        self.disposed = False
        self.on_tools_list_changed: Callable[[], None] | None = None
        self.on_status_changed: Callable[[], None] | None = None

    def set_on_tools_list_changed(self, handler: Callable[[], None]) -> None:
        self.on_tools_list_changed = handler

    def set_on_status_changed(self, handler: Callable[[], None]) -> None:
        self.on_status_changed = handler

    def _set_status(self, status: McpServerStatus) -> None:
        if self.disposed:
            return
        idx = next((i for i, s in enumerate(self.server_statuses) if s.name == status.name), None)
        if idx is None:
            self.server_statuses.append(status)
        else:
            self.server_statuses[idx] = status
        if self.on_status_changed:
            self.on_status_changed()

    def prepare(self, servers: dict[str, dict[str, Any]] | None) -> None:
        if not servers:
            return
        self.disposed = False
        for name in servers:
            if name not in self.configured_server_names:
                self.configured_server_names.append(name)
            if any(s.name == name for s in self.server_statuses):
                continue
            self._set_status(
                McpServerStatus(
                    name=name,
                    status="starting",
                    connected=False,
                )
            )

    async def initialize(self, servers: dict[str, dict[str, Any]] | None = None) -> None:
        if self.initialized or self.disposed:
            return
        self.initialized = True
        if not servers:
            return

        self.server_configs = servers
        self.prepare(servers)

        for name, config in servers.items():
            if self.disposed:
                break
            await self._connect_server(name, config)

    async def sync_servers(self, servers: dict[str, dict[str, Any]] | None) -> None:
        """Dynamically sync MCP servers with updated configuration during runtime."""
        if self.disposed:
            return
        new_servers = servers or {}
        new_names = set(new_servers.keys())
        current_names = set(self.configured_server_names)

        # Disconnect and remove deleted servers
        removed_names = current_names - new_names
        for name in removed_names:
            client = next((c for c in self.clients if c.server_name == name), None)
            if client:
                await client.disconnect()
            self.clients = [c for c in self.clients if c.server_name != name]
            self.tools = [t for t in self.tools if t.server_name != name]
            self.prompts = [p for p in self.prompts if p.get("server_name") != name]
            self.resources = [r for r in self.resources if r.get("server_name") != name]
            self.server_statuses = [s for s in self.server_statuses if s.name != name]
            if name in self.configured_server_names:
                self.configured_server_names.remove(name)
            self.server_configs.pop(name, None)

        # Connect new or updated servers
        for name, config in new_servers.items():
            old_config = self.server_configs.get(name)
            if name not in self.configured_server_names or old_config != config:
                if name not in self.configured_server_names:
                    self.configured_server_names.append(name)
                self.server_configs[name] = config
                await self._connect_server(name, config)

        if self.on_tools_list_changed:
            self.on_tools_list_changed()

    async def reconnect(self, name: str, config: dict[str, Any] | None = None) -> bool:
        if self.disposed:
            return False
        effective_config = config or self.server_configs.get(name)
        if not effective_config:
            return False
        if config:
            self.server_configs[name] = config

        existing_client = next((c for c in self.clients if c.server_name == name), None)
        if existing_client:
            try:
                await existing_client.disconnect()
            except Exception:
                pass

        self._set_status(
            McpServerStatus(
                name=name,
                status="reconnecting",
                connected=False,
                error="Reconnecting...",
            )
        )
        await self._connect_server(name, effective_config)
        status = next((s for s in self.server_statuses if s.name == name), None)
        return status.connected if status else False

    def list_tools(self) -> list[McpToolEntry]:
        return list(self.tools)

    def get_prompts(self) -> list[dict[str, Any]]:
        return list(self.prompts)

    def get_resources(self) -> list[dict[str, Any]]:
        return list(self.resources)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        target = next(
            (
                r
                for r in self.resources
                if r.get("definition", {}).get("uri") == uri
                or r.get("original_name") == uri
                or r.get("namespaced_name") == uri
            ),
            None,
        )
        if not target:
            if self.clients:
                return await self.clients[0].read_resource(uri)
            return {"contents": [], "error": f"No client available to read resource '{uri}'"}
        client: McpClient = target["client"]
        target_uri = target.get("definition", {}).get("uri") or uri
        return await client.read_resource(target_uri)

    async def _connect_server(self, name: str, config: dict[str, Any]) -> None:
        if self.disposed:
            return

        # Filter out disconnected clients for this server
        self.clients = [c for c in self.clients if c.server_name != name and c.is_connected()]
        self.tools = [t for t in self.tools if t.server_name != name]
        self.prompts = [p for p in self.prompts if p.get("server_name") != name]
        self.resources = [r for r in self.resources if r.get("server_name") != name]

        client = McpClient(name, config)
        client.set_on_disconnect(lambda reason: self._on_server_crash(name, reason))

        try:
            await client.connect()
            self.clients.append(client)

            server_tools = client._tools or await client.list_tools(timeout_s=10.0)
            tool_names: list[str] = []
            used_names = {t.namespaced_name for t in self.tools}

            for tool in server_tools:
                orig_name = tool.get("name", "")
                namespaced = build_mcp_namespaced_name(name, orig_name, used_names)
                used_names.add(namespaced)
                tool_names.append(namespaced)
                self.tools.append(
                    McpToolEntry(
                        server_name=name,
                        original_name=orig_name,
                        namespaced_name=namespaced,
                        definition=tool,
                        client=client,
                    )
                )

            # Prompts (if supported)
            try:
                server_prompts = await client.list_prompts(timeout_s=5.0)
            except Exception:
                server_prompts = []
            prompt_names: list[str] = []
            for p in server_prompts:
                p_name = p.get("name", "")
                ns_p = f"mcp__{sanitize_api_tool_name_part(name)}__{sanitize_api_tool_name_part(p_name)}"
                prompt_names.append(ns_p)
                self.prompts.append(
                    {
                        "server_name": name,
                        "original_name": p_name,
                        "namespaced_name": ns_p,
                        "definition": p,
                        "client": client,
                    }
                )

            # Resources (if supported)
            try:
                server_resources = await client.list_resources(timeout_s=5.0)
            except Exception:
                server_resources = []
            resource_names: list[str] = []
            for r in server_resources:
                r_name = r.get("name", "")
                ns_r = f"mcp__{sanitize_api_tool_name_part(name)}__{sanitize_api_tool_name_part(r_name)}"
                resource_names.append(ns_r)
                self.resources.append(
                    {
                        "server_name": name,
                        "original_name": r_name,
                        "namespaced_name": ns_r,
                        "definition": r,
                        "client": client,
                    }
                )

            self._set_status(
                McpServerStatus(
                    name=name,
                    status="ready",
                    connected=True,
                    tool_count=len(tool_names),
                    tools=tool_names,
                    prompt_count=len(prompt_names),
                    prompts=prompt_names,
                    resource_count=len(resource_names),
                    resources=resource_names,
                )
            )
            if self.on_tools_list_changed:
                self.on_tools_list_changed()
        except Exception as err:
            await client.disconnect()
            self._set_status(
                McpServerStatus(
                    name=name,
                    status="failed",
                    connected=False,
                    error=str(err),
                )
            )

    def _on_server_crash(self, name: str, reason: str) -> None:
        if self.disposed:
            return
        self.clients = [c for c in self.clients if c.is_connected()]
        self.tools = [t for t in self.tools if t.server_name != name]
        self.prompts = [p for p in self.prompts if p.get("server_name") != name]
        self.resources = [r for r in self.resources if r.get("server_name") != name]
        if self.on_tools_list_changed:
            self.on_tools_list_changed()
        self._set_status(
            McpServerStatus(
                name=name,
                status="failed",
                connected=False,
                error=reason,
            )
        )

    def get_status(self) -> list[McpServerStatus]:
        result = list(self.server_statuses)
        known = {s.name for s in result}
        for name in self.configured_server_names:
            if name not in known:
                result.append(
                    McpServerStatus(
                        name=name,
                        status="starting",
                        connected=False,
                    )
                )
        return result

    def get_mcp_tool_definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        for t in self.tools:
            input_schema = t.definition.get("inputSchema") or {}
            props = input_schema.get("properties") or {}
            params: dict[str, Any] = {
                "type": "object",
                "properties": props,
            }
            if input_schema.get("required"):
                params["required"] = input_schema["required"]
            if input_schema.get("additionalProperties") is not None:
                params["additionalProperties"] = input_schema["additionalProperties"]

            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.namespaced_name,
                        "description": self._build_mcp_tool_description(t),
                        "parameters": params,
                    },
                }
            )
        return defs

    def _build_mcp_tool_description(self, tool: McpToolEntry) -> str:
        desc = (tool.definition.get("description") or "").strip()
        source = f"{tool.server_name}: {tool.original_name}"
        if not desc:
            return source
        if tool.namespaced_name == build_raw_mcp_namespaced_name(
            tool.server_name, tool.original_name
        ):
            return desc
        return f"{desc}\nMCP source: {source}"

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__") and any(t.namespaced_name == name for t in self.tools)

    async def execute_mcp_tool(
        self, name: str, args: dict[str, Any], timeout_s: float = 60.0
    ) -> ToolResult:
        tool = next((t for t in self.tools if t.namespaced_name == name), None)
        if not tool:
            return ToolResult(ok=False, name=name, error=f"Unknown MCP tool: {name}")

        try:
            result = await tool.client.call_tool(tool.original_name, args, timeout_s=timeout_s)
            content_list = result.get("content", [])
            text_parts = [
                c.get("text", "")
                for c in content_list
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
            ]
            text = "\n".join(text_parts) if text_parts else json.dumps(content_list)
            is_error = bool(result.get("isError", False))
            return ToolResult(
                ok=not is_error,
                name=name,
                output=text if not is_error else None,
                error=text if is_error else None,
                metadata={"rawResult": result},
            )
        except Exception as err:
            return ToolResult(ok=False, name=name, error=str(err))

    async def get_mcp_prompt(
        self, name: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> ToolResult:
        prompt = next((p for p in self.prompts if p["namespaced_name"] == name), None)
        if not prompt:
            return ToolResult(ok=False, name=name, error=f"Unknown MCP prompt: {name}")

        try:
            result = await prompt["client"].get_prompt(
                prompt["definition"]["name"], args, timeout_s=timeout_s
            )
            messages = result.get("messages", [])
            lines = [
                f"[{m.get('role', 'user')}] {m.get('content', {}).get('text', '')}"
                for m in messages
                if isinstance(m, dict)
            ]
            return ToolResult(
                ok=True,
                name=name,
                output="\n".join(lines) or json.dumps(result),
                metadata=result,
            )
        except Exception as err:
            return ToolResult(ok=False, name=name, error=str(err))

    async def read_mcp_resource(self, name: str, uri: str, timeout_s: float = 30.0) -> ToolResult:
        resource = next((r for r in self.resources if r["namespaced_name"] == name), None)
        if not resource:
            return ToolResult(ok=False, name=name, error=f"Unknown MCP resource: {name}")

        try:
            result = await resource["client"].read_resource(uri, timeout_s=timeout_s)
            contents = result.get("contents", [])
            lines = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("text")]
            return ToolResult(
                ok=True,
                name=name,
                output="\n".join(lines) or json.dumps(result),
                metadata=result,
            )
        except Exception as err:
            return ToolResult(ok=False, name=name, error=str(err))

    async def disconnect(self) -> None:
        self.disposed = True
        for client in self.clients:
            await client.disconnect()
        self.clients.clear()
        self.tools.clear()
        self.prompts.clear()
        self.resources.clear()
        self.server_statuses.clear()
        self.configured_server_names.clear()
        self.server_configs.clear()
        self.initialized = False

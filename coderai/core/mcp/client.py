"""McpClient — JSON-RPC 2.0 stdio client for Model Context Protocol (deepcode mcp-client.ts)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Any
from collections.abc import Callable

from coderai.core.common.process_tree import kill_process_tree

CMD_METACHARS_PATTERN = re.compile(r'[\s&<>()|^"%]')


def create_mcp_spawn_spec(
    command: str, args: list[str] | None = None, platform: str = sys.platform
) -> dict[str, Any]:
    args = args or []
    if platform != "win32":
        return {
            "command": command,
            "args": args,
            "shell": False,
        }

    # Windows command building
    def quote_windows_arg(arg: str) -> str:
        if not arg:
            return '""'
        if not CMD_METACHARS_PATTERN.search(arg):
            return arg
        escaped = arg.replace('"', '\\"')
        return f'"{escaped}"'

    quoted_cmd = quote_windows_arg(command) if CMD_METACHARS_PATTERN.search(command) else command
    quoted_args = [quote_windows_arg(a) for a in args]
    cmd_line = f"{quoted_cmd} {' '.join(quoted_args)}".strip()

    return {
        "command": cmd_line,
        "args": [],
        "shell": True,
        "windowsHide": True,
    }


class McpClient:
    def __init__(
        self,
        server_name: str,
        command_or_config: str | dict[str, Any],
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.server_name = server_name
        if isinstance(command_or_config, dict):
            self.command = command_or_config.get("command", "")
            self.args = list(command_or_config.get("args") or [])
            self.env = command_or_config.get("env")
        else:
            self.command = command_or_config
            self.args = list(args or [])
            self.env = env

        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._tools: list[dict[str, Any]] = []
        self._prompts: list[dict[str, Any]] = []
        self._resources: list[dict[str, Any]] = []
        self._disconnected = False
        self._disconnect_handler: Callable[[str], None] | None = None

    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._disconnected

    def set_on_disconnect(self, handler: Callable[[str], None]) -> None:
        self._disconnect_handler = handler

    async def connect(self, timeout_s: float = 30.0) -> None:
        if not self.command:
            raise RuntimeError(f"MCP server '{self.server_name}' has no command specified.")

        spawn_spec = create_mcp_spawn_spec(self.command, self.args)
        merged_env = dict(os.environ)
        if self.env:
            merged_env.update(self.env)

        if spawn_spec["shell"]:
            self._proc = subprocess.Popen(
                spawn_spec["command"],
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                bufsize=1,
                env=merged_env,
            )
        else:
            self._proc = subprocess.Popen(
                [spawn_spec["command"], *spawn_spec["args"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                bufsize=1,
                env=merged_env,
            )

        self._disconnected = False
        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "coderai", "version": "0.0.0"},
                    },
                ),
                timeout=timeout_s,
            )
            await self._notify("notifications/initialized", {})
            tools_res = await self.list_tools(timeout_s=timeout_s)
            self._tools = tools_res
        except Exception as e:
            await self.disconnect()
            raise RuntimeError(f"Failed to connect to MCP server '{self.server_name}': {e}") from e

    async def disconnect(self) -> None:
        self._disconnected = True
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

        if self._proc:
            pid = self._proc.pid
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass

            try:
                kill_process_tree(pid)
            except Exception:
                pass
            self._proc = None

        # Fail any pending requests
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' disconnected."))
        self._pending.clear()

    async def list_tools(self, timeout_s: float = 30.0) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, timeout_s=timeout_s)
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        self._tools = tools
        return tools

    async def call_tool(
        self, name: str, args: dict[str, Any], timeout_s: float = 60.0
    ) -> dict[str, Any]:
        result = await self._request(
            "tools/call", {"name": name, "arguments": args}, timeout_s=timeout_s
        )
        if isinstance(result, dict):
            return result
        return {"content": [{"type": "text", "text": str(result)}], "isError": False}

    async def list_prompts(self, timeout_s: float = 30.0) -> list[dict[str, Any]]:
        result = await self._request("prompts/list", {}, timeout_s=timeout_s)
        prompts = (result or {}).get("prompts", []) if isinstance(result, dict) else []
        self._prompts = prompts
        return prompts

    async def get_prompt(
        self, name: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        result = await self._request(
            "prompts/get", {"name": name, "arguments": args}, timeout_s=timeout_s
        )
        return result if isinstance(result, dict) else {"messages": []}

    async def list_resources(self, timeout_s: float = 30.0) -> list[dict[str, Any]]:
        result = await self._request("resources/list", {}, timeout_s=timeout_s)
        resources = (result or {}).get("resources", []) if isinstance(result, dict) else []
        self._resources = resources
        return resources

    async def read_resource(self, uri: str, timeout_s: float = 30.0) -> dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri}, timeout_s=timeout_s)
        return result if isinstance(result, dict) else {"contents": []}

    async def _request(self, method: str, params: dict[str, Any], timeout_s: float = 30.0) -> Any:
        if self._disconnected or not self._proc:
            raise RuntimeError(f"MCP server '{self.server_name}' is not connected.")

        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(msg_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict[str, Any]) -> None:
        if self._proc and self._proc.stdin and not self._disconnected:
            try:
                line = json.dumps(message) + "\n"
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except Exception:
                pass

    async def _read_loop(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        loop = asyncio.get_running_loop()
        while not self._disconnected and self._proc and self._proc.stdout:
            try:
                line = await loop.run_in_executor(None, self._proc.stdout.readline)
            except Exception:
                break
            if not line:
                break
            try:
                message = json.loads(line.strip())
            except Exception:
                continue

            if not isinstance(message, dict):
                continue

            msg_id = message.get("id")
            if isinstance(msg_id, int):
                future = self._pending.get(msg_id)
                if future and not future.done():
                    if "error" in message:
                        err_obj = message["error"]
                        err_msg = (
                            err_obj.get("message", "MCP error")
                            if isinstance(err_obj, dict)
                            else str(err_obj)
                        )
                        future.set_exception(RuntimeError(err_msg))
                    else:
                        future.set_result(message.get("result"))

        if not self._disconnected and self._disconnect_handler:
            self._disconnect_handler(f"MCP server '{self.server_name}' process exited.")

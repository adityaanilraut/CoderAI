"""MCP Transport layer — Stdio and SSE transport implementations for Model Context Protocol."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any
from collections.abc import Callable

import requests

from coderai.core.common.process_tree import kill_process_tree
from coderai.core.network.security import check_outbound_url

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

    # Windows command escaping
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


class McpTransport(ABC):
    """Abstract base class for MCP client transports (stdio, SSE, etc.)."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        self.on_message: Callable[[dict[str, Any]], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None

    def set_handlers(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[str], None],
    ) -> None:
        self.on_message = on_message
        self.on_disconnect = on_disconnect

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if transport is currently connected and active."""
        ...

    @abstractmethod
    async def connect(self, timeout_s: float = 30.0) -> None:
        """Establish the connection."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Terminate the connection and release resources."""
        ...

    @abstractmethod
    def send(self, message: dict[str, Any]) -> None:
        """Send a JSON-RPC message over the transport."""
        ...


class StdioMcpTransport(McpTransport):
    """MCP transport over standard I/O pipes to a child process."""

    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        super().__init__(server_name)
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._disconnected = False

    def is_connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._disconnected

    async def connect(self, timeout_s: float = 30.0) -> None:
        if not self.command:
            raise RuntimeError(f"Stdio MCP server '{self.server_name}' has no command specified.")

        spawn_spec = create_mcp_spawn_spec(self.command, self.args)
        merged_env = dict(os.environ)
        if self.env:
            merged_env.update(self.env)

        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "errors": "replace",
            "bufsize": 1,
            "env": merged_env,
        }
        if self.cwd:
            kwargs["cwd"] = self.cwd

        if spawn_spec["shell"]:
            self._proc = subprocess.Popen(spawn_spec["command"], shell=True, **kwargs)
        else:
            self._proc = subprocess.Popen([spawn_spec["command"], *spawn_spec["args"]], **kwargs)

        self._disconnected = False
        self._reader_task = asyncio.create_task(self._read_loop())

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
                self._proc.terminate()
            except Exception:
                pass

            try:
                kill_process_tree(pid)
            except Exception:
                pass

            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def send(self, message: dict[str, Any]) -> None:
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
                parsed = json.loads(line.strip())
                if isinstance(parsed, dict) and self.on_message:
                    self.on_message(parsed)
            except Exception:
                continue

        if not self._disconnected:
            self._disconnected = True
            if self.on_disconnect:
                self.on_disconnect(f"MCP server '{self.server_name}' process exited.")


class SseMcpTransport(McpTransport):
    """MCP transport over Server-Sent Events (SSE) stream and HTTP POST for messages."""

    def __init__(
        self,
        server_name: str,
        url: str,
        headers: dict[str, str] | None = None,
        policy: Any = None,
    ) -> None:
        super().__init__(server_name)
        self.url = url
        self.headers = headers or {}
        self.policy = policy
        self._post_endpoint: str | None = None
        self._session: requests.Session | None = None
        self._response: requests.Response | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._disconnected = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def is_connected(self) -> bool:
        return self._running and not self._disconnected

    async def connect(self, timeout_s: float = 30.0) -> None:
        check_outbound_url(self.url, self.policy)
        self._loop = asyncio.get_running_loop()
        self._session = requests.Session()
        self._session.headers.update(self.headers)
        self._disconnected = False
        self._running = True

        endpoint_ready = asyncio.Event()

        def _sse_worker() -> None:
            try:
                assert self._session is not None
                self._response = self._session.get(self.url, stream=True, timeout=(timeout_s, None))
                response = self._response
                if not response.ok:
                    if not endpoint_ready.is_set():
                        if self._loop:
                            self._loop.call_soon_threadsafe(endpoint_ready.set)
                    return

                current_event = "message"
                data_buffer: list[str] = []

                for raw_line in response.iter_lines(decode_unicode=True):
                    if not self._running:
                        break
                    if raw_line is None:
                        continue
                    line = raw_line.strip()
                    if not line:
                        # End of SSE event
                        if data_buffer:
                            full_data = "\n".join(data_buffer)
                            self._handle_sse_event(current_event, full_data, endpoint_ready)
                        current_event = "message"
                        data_buffer.clear()
                        continue

                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_buffer.append(line[len("data:") :].strip())

            except Exception:
                pass
            finally:
                self._running = False
                if not endpoint_ready.is_set() and self._loop:
                    self._loop.call_soon_threadsafe(endpoint_ready.set)
                if not self._disconnected and self.on_disconnect and self._loop:
                    self._loop.call_soon_threadsafe(
                        self.on_disconnect, f"SSE MCP connection to '{self.server_name}' closed."
                    )

        self._reader_thread = threading.Thread(target=_sse_worker, daemon=True)
        self._reader_thread.start()

        # Wait for endpoint discovery event or timeout
        try:
            await asyncio.wait_for(endpoint_ready.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise RuntimeError(
                f"Timeout waiting for SSE endpoint from '{self.server_name}' at {self.url}"
            )

        if not self._post_endpoint:
            # If no custom endpoint event received, default POST endpoint is same URL or /message
            self._post_endpoint = self.url

    def _handle_sse_event(self, event_type: str, data: str, endpoint_ready: asyncio.Event) -> None:
        if event_type == "endpoint":
            # Relative or absolute endpoint URL for POST messages
            post_url = data.strip()
            if post_url.startswith("/"):
                base_parts = urllib.parse.urlsplit(self.url)
                self._post_endpoint = f"{base_parts.scheme}://{base_parts.netloc}{post_url}"
            elif post_url.startswith("http://") or post_url.startswith("https://"):
                self._post_endpoint = post_url
            else:
                self._post_endpoint = urllib.parse.urljoin(self.url, post_url)

            if not endpoint_ready.is_set() and self._loop:
                self._loop.call_soon_threadsafe(endpoint_ready.set)

        elif event_type == "message":
            if not endpoint_ready.is_set() and self._loop:
                if not self._post_endpoint:
                    self._post_endpoint = self.url
                self._loop.call_soon_threadsafe(endpoint_ready.set)

            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and self.on_message and self._loop:
                    self._loop.call_soon_threadsafe(self.on_message, parsed)
            except Exception:
                pass

    def send(self, message: dict[str, Any]) -> None:
        if not self._post_endpoint or self._disconnected or not self._session:
            return

        def _do_post() -> None:
            try:
                assert self._session is not None and self._post_endpoint is not None
                self._session.post(self._post_endpoint, json=message, timeout=30.0)
            except Exception:
                pass

        threading.Thread(target=_do_post, daemon=True).start()

    async def disconnect(self) -> None:
        self._disconnected = True
        self._running = False
        if self._response:
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

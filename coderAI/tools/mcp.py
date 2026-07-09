"""MCP (Model Context Protocol) client for connecting to external MCP servers."""

import atexit
import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.core.provenance import Provenance
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.fsperms import atomic_write_json
from coderAI.tools.base import Tool

logger = logging.getLogger(__name__)

# Launchers permitted for stdio MCP servers. Shared by the ``mcp_connect`` tool
# and the ``coderAI mcp`` CLI so both validate against the same allow-list.
ALLOWED_MCP_LAUNCHERS = {"npx", "node", "python", "python3", "uvx", "bun", "deno"}

# Per-launcher tokens that evaluate inline code, turning an *allowed* launcher
# into an arbitrary-code sink (``python -c "…"``, ``node -e "…"``, ``deno eval
# "…"``). ``ALLOWED_MCP_LAUNCHERS`` only constrains the launcher itself, so a
# config planted in ``mcp_servers.json`` with an allowed launcher could still run
# attacker-chosen code through one of these. Scoped per launcher so npx's
# legitimate ``-p <pkg>`` (package selector) is not confused with node's ``-p``
# (eval-and-print). Enforced in the single ``validate_stdio_launch`` choke point.
_INLINE_EXEC_TOKENS = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "bun": {"-e", "--eval", "-p", "--print"},
    "deno": {"eval"},
}


def validate_stdio_launch(command: str, args: Optional[List[str]]) -> Optional[str]:
    """Validate a stdio MCP launcher + argv; return an error string or ``None``.

    The single choke point shared by ``MCPConnectTool`` (LLM-driven) and startup
    autoconnect (``_autoconnect_mcp_servers``, config-driven), so a server planted
    in ``mcp_servers.json`` is held to the same launcher allow-list, inline-exec
    block, command blocklist, and interactive-command check as an interactive
    ``mcp_connect``. Previously only the tool path enforced these; autoconnect
    called ``connect_stdio`` directly and bypassed them.
    """
    if not command:
        return "Command is required for stdio transport"

    cmd_lower = command.lower()
    allowed = any(
        cmd_lower == launcher or cmd_lower.endswith("/" + launcher)
        for launcher in ALLOWED_MCP_LAUNCHERS
    )
    if not allowed:
        return (
            f"MCP server launcher '{command}' is not in the allowed set: "
            f"{', '.join(sorted(ALLOWED_MCP_LAUNCHERS))}"
        )

    arg_list = list(args or [])
    base = cmd_lower.rsplit("/", 1)[-1]
    blocked_tokens = _INLINE_EXEC_TOKENS.get(base, set())
    for token in arg_list:
        if token in blocked_tokens:
            return (
                f"MCP launcher flag '{command} {token}' runs arbitrary inline code, "
                "which is not allowed (it defeats the launcher allow-list)."
            )

    from coderAI.tools.terminal import is_command_blocked
    from coderAI.system.safeguards import is_interactive_command

    full_cmd = command + " " + " ".join(arg_list) if arg_list else command
    if is_command_blocked(full_cmd):
        return "MCP server command is blocked for safety"
    if is_interactive_command(full_cmd):
        return "MCP server command appears interactive, which is not allowed"
    return None


def _is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and any loopback IP literal (127.0.0.0/8, ::1)."""
    import ipaddress

    h = (host or "").strip().lower()
    if not h:
        return False
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def validate_remote_mcp_url(url: str) -> Optional[str]:
    """Validate a remote MCP/OAuth endpoint URL.

    Returns an error string when *url* is not an acceptable remote endpoint, or
    ``None`` when it is. Requires ``https://`` for every remote host; plaintext
    ``http://`` is allowed only for loopback dev hosts (``127.0.0.1``/``localhost``).
    This is the single scheme gate shared by ``connect_http``/``connect_sse``, the
    ``coderAI mcp add`` CLI, and the OAuth discovery/token calls, so an untrusted
    ``mcp_servers.json`` cannot downgrade a connection or an OAuth token exchange
    onto the network in cleartext.
    """
    from urllib.parse import urlparse

    raw = (url or "").strip()
    if not raw:
        return "Empty MCP endpoint URL."
    try:
        parsed = urlparse(raw)
    except ValueError:
        return f"Invalid MCP endpoint URL: {url!r}"
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return None
    if scheme == "http":
        if _is_loopback_host(parsed.hostname or ""):
            return None
        return (
            f"Refusing plaintext http:// for remote MCP/OAuth endpoint {url!r}; use "
            "https:// (http:// is allowed only for loopback dev hosts like "
            "127.0.0.1/localhost)."
        )
    return (
        f"Unsupported URL scheme {scheme or '(none)'!r} for a remote MCP endpoint "
        f"(use https://): {url!r}"
    )


def _shape_tool_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a ``tools/call`` result into the tool-facing dict.

    Concatenates the text content parts; MCP's ``isError: true`` marks a
    tool-level failure (distinct from a JSON-RPC protocol error, which
    :meth:`MCPClient._request` already turned into ``success=False``).
    """
    text_content = "".join(
        part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
    )
    is_error = bool(result.get("isError"))
    out: Dict[str, Any] = {"success": not is_error, "content": text_content, "raw": result}
    if is_error:
        out["error"] = text_content or "MCP tool returned an error."
    return out


def _reject_reserved_server_name(server_name: str) -> Optional[Dict[str, Any]]:
    """``mcp__<server>__<tool>`` routing uses the first ``__`` after the prefix;
    disallow ``__`` in the server segment so names stay unambiguous."""
    if "__" in server_name:
        return {
            "success": False,
            "error": (
                "server_name must not contain '__' — it is reserved for MCP tool "
                f"id encoding (got {server_name!r}). Use a name like 'my_server'."
            ),
        }
    return None


class MCPAuthRequiredError(Exception):
    """Raised when an HTTP MCP server demands OAuth (HTTP 401).

    Carries the ``WWW-Authenticate`` header so the OAuth layer can discover the
    authorization server. Callers map this to a "run ``coderAI mcp login``" hint.
    """

    def __init__(self, www_authenticate: Optional[str] = None):
        self.www_authenticate = www_authenticate
        super().__init__("authorization required (HTTP 401)")


def mcp_servers_path() -> Path:
    """Path to the persisted MCP server config (``~/.coderAI/mcp_servers.json``)."""
    from coderAI.system.config import config_manager

    return config_manager.config_dir / "mcp_servers.json"


# Bundled MCP server name for rarely used git tools (see mcp_servers/git_extended.py).
BUNDLED_GIT_EXTENDED_SERVER = "git_extended"


def bundled_mcp_servers() -> Dict[str, Dict[str, Any]]:
    """Built-in MCP servers shipped with CoderAI.

    These are merged by :func:`effective_mcp_servers` so they auto-connect on
    startup unless the user overrides or disables the same name in
    ``mcp_servers.json``.
    """
    import sys

    return {
        BUNDLED_GIT_EXTENDED_SERVER: {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "coderAI.mcp_servers.git_extended"],
            "bundled": True,
        }
    }


def load_mcp_servers() -> Dict[str, Any]:
    """Read the on-disk MCP server config, tolerating a missing or corrupt file.

    Always returns a dict with an ``mcpServers`` mapping so callers can index
    into it without extra guards. Does **not** include bundled servers — use
    :func:`effective_mcp_servers` for autoconnect / listing so saves never
    accidentally persist built-in entries into ``mcp_servers.json``.
    """
    path = mcp_servers_path()
    if not path.exists():
        return {"mcpServers": {}}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"mcpServers": {}}
        data.setdefault("mcpServers", {})
        return data
    except Exception:
        logger.warning("Failed to read %s; treating as empty", path, exc_info=True)
        return {"mcpServers": {}}


def effective_mcp_servers() -> Dict[str, Any]:
    """On-disk MCP config merged with bundled servers.

    User entries win on name collision (so ``disabled: true`` or a custom
    launcher for ``git_extended`` overrides the built-in). Bundled defaults
    are only injected when the name is absent from disk.
    """
    data = load_mcp_servers()
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    for name, entry in bundled_mcp_servers().items():
        if name not in servers:
            servers[name] = dict(entry)
    return data


def persist_mcp_server(name: str, entry: Dict[str, Any]) -> None:
    """Add or overwrite a single server entry in the persisted MCP config.

    Called after a successful interactive ``mcp_connect`` so the server is
    auto-reconnected on the next session (see
    ``ExecutionLoop._autoconnect_mcp_servers``). Without this, connections made
    via the agent tool live only in ``mcp_client.servers`` and are forgotten
    when the session ends, so a fresh ``mcp_list`` comes back empty.

    Idempotent: an existing entry of the same name is replaced. Persistence
    failures are logged but never propagated — a live connection must not be
    torn down just because the config file could not be written.
    """
    try:
        data = load_mcp_servers()
        servers = data.setdefault("mcpServers", {})
        servers[name] = entry
        save_mcp_servers(data)
    except Exception:
        logger.warning(
            "Failed to persist MCP server %r; it will not auto-reconnect next session",
            name,
            exc_info=True,
        )


def set_mcp_server_disabled(name: str, disabled: bool) -> bool:
    """Flip the ``disabled`` flag on a persisted MCP server.

    A disabled server stays in ``mcp_servers.json`` but is skipped by
    ``ExecutionLoop._autoconnect_mcp_servers`` on startup, so it does not
    auto-reconnect until re-enabled. Enabling simply removes the flag (absence
    means enabled) to keep the on-disk config tidy.

    Bundled servers (e.g. ``git_extended``) may not have an on-disk entry yet;
    disabling them writes a stub so the override sticks. Returns ``False`` only
    when the name is neither on disk nor bundled.
    """
    data = load_mcp_servers()
    servers = data.setdefault("mcpServers", {})
    entry = servers.get(name)
    if not isinstance(entry, dict):
        bundled = bundled_mcp_servers().get(name)
        if bundled is None:
            return False
        entry = dict(bundled)
        servers[name] = entry
    if disabled:
        entry["disabled"] = True
    else:
        entry.pop("disabled", None)
        # Drop a pure disable-stub for a bundled server so the built-in returns.
        if entry.get("bundled") and set(entry.keys()) <= {
            "transport",
            "command",
            "args",
            "bundled",
        }:
            servers.pop(name, None)
    save_mcp_servers(data)
    return True


def save_mcp_servers(data: Dict[str, Any]) -> None:
    """Write the MCP server config as pretty-printed JSON.

    Writes to a temp file in the same directory and ``os.replace``s it into
    place so a crash or a concurrent writer can never leave a truncated file.
    ``load_mcp_servers`` silently treats a corrupt file as empty, so a
    non-atomic write would risk wiping every configured server. Mirrors the
    atomic-save pattern in ``system.config.ConfigManager.save``.
    """
    atomic_write_json(mcp_servers_path(), data)


class MCPClient:
    """Client for connecting to MCP servers and discovering tools.

    Supports stdio, SSE, and Streamable HTTP transports for connecting to
    MCP-compatible servers. Discovered tools are registered in the CoderAI tool
    registry.
    """

    def __init__(self):
        """Initialize MCP client."""
        self.servers: Dict[str, Dict[str, Any]] = {}
        self.discovered_tools: List[Dict[str, Any]] = []
        self.discovered_resources: List[Dict[str, Any]] = []
        self.discovered_prompts: List[Dict[str, Any]] = []
        self._next_id: int = 1
        self._reconnect_attempts: Dict[str, int] = {}

    def _get_next_id(self) -> int:
        """Return a unique, incrementing JSON-RPC request ID."""
        current = self._next_id
        self._next_id += 1
        return current

    async def _read_response(
        self,
        stdout: asyncio.StreamReader,
        expected_id: int,
        timeout: float = 10,
    ) -> Dict[str, Any]:
        """Read lines from stdout until a JSON-RPC response with the expected id arrives.

        Skips any notifications (messages without an 'id' field) that servers
        may send between requests.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("Server closed stdout unexpectedly")
            try:
                decoded_line = line.decode("utf-8", errors="replace")
                parsed = json.loads(decoded_line)
                if not isinstance(parsed, dict):
                    logger.warning(f"Parsed line is not a dictionary: {decoded_line}")
                    continue
            except Exception as e:
                logger.warning(f"Failed to decode or parse line: {line!r}. Error: {e}")
                continue

            # Skip notifications (no 'id' field)
            if "id" not in parsed:
                continue
            if parsed["id"] == expected_id:
                return parsed
            # Unexpected id — log and keep reading
            logger.warning(f"Unexpected JSON-RPC id {parsed.get('id')}, expected {expected_id}")

    async def _drain_stderr(self, server_name: str, stream: asyncio.StreamReader) -> None:
        """Continuously read a stdio server's stderr so its pipe never fills.

        ``stderr`` is a PIPE but nothing else reads it. The OS pipe buffer is
        small (~64KB); once a chatty server fills it, its next write to stderr
        blocks, and because stdio MCP servers are typically single-threaded,
        that also stalls the stdout responses we read — deadlocking the
        connection. Draining to the debug log keeps the buffer clear while
        preserving server diagnostics.
        """
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[mcp:%s stderr] %s", server_name, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "stderr drain for MCP server '%s' ended unexpectedly", server_name, exc_info=True
            )

    def _init_request(self, init_id: int) -> Dict[str, Any]:
        """The MCP ``initialize`` request (JSON-RPC 2.0), shared by all transports."""
        return {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "CoderAI", "version": "0.1.0"},
            },
        }

    async def _finish_connect(
        self,
        server_name: str,
        entry: Dict[str, Any],
        init_response: Dict[str, Any],
        tools_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Shared tail of the ``connect_*`` methods.

        Registers the server entry (adding its tools and initialize result),
        records the discovered tools, probes resources/prompts, and shapes the
        connection summary.
        """
        server_info = tools_response.get("result", {}).get("tools", [])
        entry["tools"] = server_info
        entry["server_info"] = init_response.get("result", {})
        self.servers[server_name] = entry

        for tool in server_info:
            self.discovered_tools.append(
                {
                    "server": server_name,
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema", {}),
                }
            )

        extras = await self._discover_extras(server_name)

        out: Dict[str, Any] = {
            "success": True,
            "server": server_name,
            "tools_discovered": len(server_info),
            "resources_discovered": extras["resources"],
            "prompts_discovered": extras["prompts"],
            "tools": [t.get("name") for t in server_info],
            "server_info": init_response.get("result", {}).get("serverInfo", {}),
        }
        if entry["transport"] != "stdio":
            out["transport"] = entry["transport"]
        return out

    async def connect_stdio(
        self, server_name: str, command: str, args: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Connect to an MCP server via stdio transport.

        Args:
            server_name: Friendly name for this server connection
            command: Server command to run (e.g., 'npx', 'python3')
            args: Command line arguments for the server

        Returns:
            Connection result with discovered tools
        """
        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        # Single launcher-validation choke point: applies to both LLM-driven
        # ``mcp_connect`` and config-driven autoconnect (which calls us directly).
        launch_err = validate_stdio_launch(command, args)
        if launch_err:
            return {"success": False, "error": launch_err}

        process = None
        stderr_task: Optional["asyncio.Task[None]"] = None
        connection_failed = True
        try:
            # On Windows ``create_subprocess_exec`` does not consult PATHEXT, so
            # a bare ``npx``/``npm``/``node`` won't resolve to its ``.cmd``/``.exe``
            # launcher the way it does on POSIX. Resolve via ``shutil.which``
            # (which honours PATHEXT) so npx-based MCP servers can start.
            launch_command = command
            if os.name == "nt":
                resolved = shutil.which(command)
                if resolved:
                    launch_command = resolved
            full_args = [launch_command] + (args or [])
            process = await asyncio.create_subprocess_exec(
                *full_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            # Drain stderr in the background so the server can never block on a
            # full stderr pipe (see ``_drain_stderr``).
            if process.stderr is not None:
                stderr_task = asyncio.create_task(self._drain_stderr(server_name, process.stderr))

            # Send MCP initialize request (JSON-RPC 2.0)
            init_id = self._get_next_id()
            process.stdin.write((json.dumps(self._init_request(init_id)) + "\n").encode())
            await process.stdin.drain()

            # Read response with timeout (skips any interleaved notifications)
            try:
                init_response = await self._read_response(process.stdout, init_id, timeout=10)
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to initialize within 10s",
                }

            # Send initialized notification
            initialized_notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            assert process.stdin is not None
            process.stdin.write((json.dumps(initialized_notif) + "\n").encode())
            await process.stdin.drain()

            # Request tool list
            tools_id = self._get_next_id()
            tools_request = {
                "jsonrpc": "2.0",
                "id": tools_id,
                "method": "tools/list",
            }
            assert process.stdin is not None
            process.stdin.write((json.dumps(tools_request) + "\n").encode())
            await process.stdin.drain()

            try:
                tools_response = await self._read_response(process.stdout, tools_id, timeout=10)
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to tools/list",
                }

            result = await self._finish_connect(
                server_name,
                {
                    "transport": "stdio",
                    "process": process,
                    "stderr_task": stderr_task,
                    "_conn_params": {"command": command, "args": args},
                },
                init_response,
                tools_response,
            )
            connection_failed = False
            return result

        except FileNotFoundError:
            return {
                "success": False,
                "error": f"Command not found: {command}. Is the MCP server installed?",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            # Kill the process if the connection attempt failed.
            # connection_failed starts True and is only cleared after
            # successful server registration. This avoids leaking a zombie
            # process when tools/list times out but server_name was
            # previously connected.
            if process is not None and connection_failed:
                if stderr_task is not None:
                    stderr_task.cancel()
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    logger.debug(
                        "Failed to kill MCP process in connect_stdio finally", exc_info=True
                    )

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on a connected MCP server.

        Dispatches over the connected transport via :meth:`_request` and shapes
        the reply with :func:`_shape_tool_result`.
        """
        res = await self._request(
            server_name, "tools/call", {"name": tool_name, "arguments": arguments}
        )
        if not res["success"]:
            return res
        return _shape_tool_result(res["result"])

    async def _request(
        self,
        server_name: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """Send one JSON-RPC request to a connected server and return its result.

        Dispatches across the stdio / SSE / HTTP transports using the same
        primitives as :meth:`call_tool`. Returns
        ``{"success": True, "result": <result dict>}`` on success, or
        ``{"success": False, "error": <message>}`` otherwise.
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        server = self.servers[server_name]
        transport = server.get("transport", "stdio")
        req_id = self._get_next_id()
        request: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params

        try:
            if transport == "sse":
                import aiohttp as _aiohttp

                session = server.get("session")
                message_url = server.get("message_url")
                if not session or not message_url:
                    return {
                        "success": False,
                        "error": f"SSE connection state invalid for '{server_name}'",
                    }
                async with session.post(
                    message_url, json=request, timeout=_aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    response = await resp.json()
            elif transport == "http":
                session = server.get("session")
                url = server.get("url")
                session_id = server.get("session_id")
                if not session or not url:
                    return {
                        "success": False,
                        "error": f"HTTP connection state invalid for '{server_name}'",
                    }
                response, _ = await self._http_send(
                    session,
                    url,
                    request,
                    expected_id=req_id,
                    session_id=session_id,
                    timeout=timeout,
                )
                response = response or {}
            else:
                process = server.get("process")
                if process is None or process.returncode is not None:
                    return {"success": False, "error": f"Server '{server_name}' process has exited"}
                assert process.stdin is not None
                process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
                response = await self._read_response(process.stdout, req_id, timeout=timeout)
        except MCPAuthRequiredError:
            return {
                "success": False,
                "needs_auth": True,
                "error": (
                    f"Authorization expired for '{server_name}'. "
                    f"Run: coderAI mcp login {server_name}"
                ),
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Request '{method}' to '{server_name}' timed out after {timeout}s",
            }
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": ToolErrorCode.TOOL_ERROR}

        error = response.get("error")
        if error:
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            return {"success": False, "error": message}
        return {"success": True, "result": response.get("result", {})}

    def _capabilities(self, server_name: str) -> Dict[str, Any]:
        """Return the server's advertised capabilities from the initialize reply."""
        server = self.servers.get(server_name, {})
        caps = (server.get("server_info") or {}).get("capabilities", {})
        return caps if isinstance(caps, dict) else {}

    async def list_resources(self, server_name: str) -> Dict[str, Any]:
        """List resources exposed by a connected server (``resources/list``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "resources" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise resource support",
            }
        resp = await self._request(server_name, "resources/list")
        if not resp.get("success"):
            return resp
        resources = resp["result"].get("resources", [])
        return {
            "success": True,
            "server": server_name,
            "count": len(resources),
            "resources": resources,
        }

    async def read_resource(self, server_name: str, uri: str) -> Dict[str, Any]:
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

    async def list_prompts(self, server_name: str) -> Dict[str, Any]:
        """List prompt templates exposed by a connected server (``prompts/list``)."""
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        if "prompts" not in self._capabilities(server_name):
            return {
                "success": False,
                "error": f"Server '{server_name}' does not advertise prompt support",
            }
        resp = await self._request(server_name, "prompts/list")
        if not resp.get("success"):
            return resp
        prompts = resp["result"].get("prompts", [])
        return {"success": True, "server": server_name, "count": len(prompts), "prompts": prompts}

    async def get_prompt(
        self, server_name: str, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
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

    async def _discover_extras(self, server_name: str) -> Dict[str, int]:
        """Best-effort discovery of resources & prompts after connect.

        Non-fatal: a server exposing only tools must still connect cleanly, so
        every failure here is swallowed to the debug log. Returns the counts
        discovered so callers can surface them in the connection result.
        """
        caps = self._capabilities(server_name)
        n_resources = 0
        n_prompts = 0
        if "resources" in caps:
            try:
                r = await self.list_resources(server_name)
                if r.get("success"):
                    for res in r.get("resources", []):
                        self.discovered_resources.append(
                            {
                                "server": server_name,
                                "uri": res.get("uri", ""),
                                "name": res.get("name", ""),
                                "description": res.get("description", ""),
                                "mimeType": res.get("mimeType", ""),
                            }
                        )
                    n_resources = len(r.get("resources", []))
            except Exception:
                logger.debug("resource discovery failed for '%s'", server_name, exc_info=True)
        if "prompts" in caps:
            try:
                p = await self.list_prompts(server_name)
                if p.get("success"):
                    for pr in p.get("prompts", []):
                        self.discovered_prompts.append(
                            {
                                "server": server_name,
                                "name": pr.get("name", ""),
                                "description": pr.get("description", ""),
                                "arguments": pr.get("arguments", []),
                            }
                        )
                    n_prompts = len(p.get("prompts", []))
            except Exception:
                logger.debug("prompt discovery failed for '%s'", server_name, exc_info=True)
        return {"resources": n_resources, "prompts": n_prompts}

    async def disconnect(self, server_name: str) -> Dict[str, Any]:
        """Disconnect from an MCP server.

        Args:
            server_name: Name of the server to disconnect from

        Returns:
            Result dictionary
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        server = self.servers[server_name]
        transport = server.get("transport", "stdio")

        if transport in ("sse", "http"):
            session = server.get("session")
            if session:
                try:
                    await session.close()
                except Exception:
                    logger.debug(
                        "Failed to close %s session during disconnect", transport, exc_info=True
                    )
        else:
            stderr_task = server.get("stderr_task")
            if stderr_task is not None:
                stderr_task.cancel()
            try:
                process = server["process"]
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()

        del self.servers[server_name]
        self.discovered_tools = [t for t in self.discovered_tools if t.get("server") != server_name]
        self.discovered_resources = [
            r for r in self.discovered_resources if r.get("server") != server_name
        ]
        self.discovered_prompts = [
            p for p in self.discovered_prompts if p.get("server") != server_name
        ]

        return {"success": True, "message": f"Disconnected from {server_name}"}

    async def connect_sse(self, server_name: str, url: str) -> Dict[str, Any]:
        """Connect to an MCP server via SSE transport.

        Args:
            server_name: Friendly name for this server connection
            url: SSE endpoint URL (e.g., http://localhost:8080/sse)

        Returns:
            Connection result with discovered tools
        """
        import aiohttp

        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        scheme_err = validate_remote_mcp_url(url)
        if scheme_err:
            return {"success": False, "error": scheme_err}

        session = None
        try:
            session = aiohttp.ClientSession()
            # Connect to SSE endpoint and discover the message endpoint
            async with session.get(url) as resp:
                if resp.status != 200:
                    await session.close()
                    return {
                        "success": False,
                        "error": f"SSE endpoint returned HTTP {resp.status}",
                    }
                # Read SSE events to find the endpoint
                message_url = None
                while True:
                    try:
                        line = await asyncio.wait_for(resp.content.readline(), timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Timeout reading SSE stream while discovering message endpoint."
                        )
                        break
                    if not line:
                        break
                    line_text = line.decode("utf-8", errors="replace").strip()
                    if line_text.startswith("event: endpoint"):
                        # Read next line for data
                        continue
                    if line_text.startswith("data: "):
                        data = line_text[6:]
                        # The 'endpoint' event carries the message URL
                        message_url = data
                        break
                    if line_text and not line_text.startswith(":"):
                        # Generic SSE — try parsing as endpoint data
                        if "http" in line_text and not line_text.startswith("data:"):
                            message_url = line_text
                            break

                if not message_url:
                    # Fallback: derive message URL from SSE URL
                    from urllib.parse import urlparse, urlunparse

                    parsed = list(urlparse(url))
                    parsed[2] = parsed[2].replace("/sse", "/messages") or "/messages"
                    message_url = urlunparse(parsed)

            # Send initialize request via POST to message endpoint
            init_id = self._get_next_id()
            async with session.post(message_url, json=self._init_request(init_id)) as resp:
                init_response = await resp.json()

            # Send initialized notification
            await session.post(
                message_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )

            # Request tool list
            tools_id = self._get_next_id()
            tools_request = {
                "jsonrpc": "2.0",
                "id": tools_id,
                "method": "tools/list",
            }
            async with session.post(message_url, json=tools_request) as resp:
                tools_response = await resp.json()

            return await self._finish_connect(
                server_name,
                {
                    "transport": "sse",
                    "session": session,
                    "message_url": message_url,
                    "sse_url": url,
                    "_conn_params": {"url": url},
                },
                init_response,
                tools_response,
            )

        except ImportError:
            if session:
                await session.close()
            return {"success": False, "error": "aiohttp is required for SSE transport"}
        except Exception as e:
            if session:
                await session.close()
            if server_name in self.servers:
                del self.servers[server_name]
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def connect_http(
        self,
        server_name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Connect to an MCP server via Streamable HTTP transport.

        This is the modern remote-server transport (MCP spec 2025-03-26) that
        superseded HTTP+SSE: every JSON-RPC message is an HTTP POST to a single
        endpoint, and the server may answer either with a plain JSON body or an
        ``text/event-stream`` body carrying the response. A ``Mcp-Session-Id``
        header returned on ``initialize`` is echoed back on every later request.

        Args:
            server_name: Friendly name for this server connection.
            url: The single MCP endpoint URL (e.g. https://host/mcp).
            headers: Optional extra headers (e.g. ``Authorization``) sent on
                every request — used for token-authenticated remote servers.

        Returns:
            Connection result with discovered tools.
        """
        import aiohttp

        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        scheme_err = validate_remote_mcp_url(url)
        if scheme_err:
            return {"success": False, "error": scheme_err}

        session = None
        try:
            base_headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if headers:
                base_headers.update(headers)

            # Inject a stored OAuth bearer token (refreshing silently if needed)
            # unless the caller already supplied an explicit Authorization header.
            if not any(k.lower() == "authorization" for k in base_headers):
                from coderAI.tools.mcp_oauth import get_valid_token_sync

                token = await asyncio.to_thread(get_valid_token_sync, server_name)
                if token:
                    base_headers["Authorization"] = f"Bearer {token}"

            session = aiohttp.ClientSession(headers=base_headers)

            init_id = self._get_next_id()
            init_response, session_id = await self._http_send(
                session, url, self._init_request(init_id), expected_id=init_id, session_id=None
            )
            if init_response is None:
                await session.close()
                return {
                    "success": False,
                    "error": f"Server '{server_name}' returned no response to initialize",
                }

            # The session id (if any) must accompany every subsequent request.
            await self._http_send(
                session,
                url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                expected_id=None,
                session_id=session_id,
            )

            tools_id = self._get_next_id()
            tools_response, _ = await self._http_send(
                session,
                url,
                {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                expected_id=tools_id,
                session_id=session_id,
            )
            tools_response = tools_response or {}

            return await self._finish_connect(
                server_name,
                {
                    "transport": "http",
                    "session": session,
                    "url": url,
                    "session_id": session_id,
                    "headers": headers or {},
                    "_conn_params": {"url": url, "headers": headers or {}},
                },
                init_response,
                tools_response,
            )

        except MCPAuthRequiredError as e:
            if session:
                await session.close()
            if server_name in self.servers:
                del self.servers[server_name]
            return {
                "success": False,
                "needs_auth": True,
                "www_authenticate": e.www_authenticate,
                "error": (
                    f"MCP server '{server_name}' requires authorization. "
                    f"Run: coderAI mcp login {server_name}"
                ),
            }
        except ImportError:
            if session:
                await session.close()
            return {"success": False, "error": "aiohttp is required for HTTP transport"}
        except Exception as e:
            if session:
                await session.close()
            if server_name in self.servers:
                del self.servers[server_name]
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def _http_send(
        self,
        session: Any,
        url: str,
        payload: Dict[str, Any],
        expected_id: Optional[int],
        session_id: Optional[str],
        timeout: float = 30,
    ) -> tuple:
        """POST one JSON-RPC message over Streamable HTTP and read the reply.

        Returns ``(response_dict_or_None, session_id)``. ``expected_id`` is
        ``None`` for notifications (the server replies 202/empty and we return
        ``None``). The server's ``Mcp-Session-Id`` header — present on the
        ``initialize`` reply — is threaded back out so callers can reuse it.
        """
        import aiohttp

        req_headers = {}
        if session_id:
            req_headers["Mcp-Session-Id"] = session_id
        async with session.post(
            url,
            json=payload,
            headers=req_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            new_session_id = resp.headers.get("Mcp-Session-Id") or session_id
            if resp.status == 401:
                raise MCPAuthRequiredError(resp.headers.get("WWW-Authenticate"))
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"MCP server returned HTTP {resp.status}: {body[:300]}")
            # Notifications expect no response body (202 Accepted is typical).
            if expected_id is None:
                return None, new_session_id
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                parsed = await self._read_http_sse(resp, expected_id, timeout=timeout)
            else:
                parsed = await resp.json()
            return parsed, new_session_id

    async def _read_http_sse(
        self, resp: Any, expected_id: int, timeout: float = 30
    ) -> Dict[str, Any]:
        """Read an SSE-framed HTTP body until the matching JSON-RPC reply lands.

        Streamable HTTP servers may answer a single request with an event
        stream that interleaves server notifications before the actual result;
        we accumulate ``data:`` lines per event and return the first event whose
        JSON-RPC ``id`` matches ``expected_id``.
        """
        import time

        deadline = time.monotonic() + timeout
        data_lines: List[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(resp.content.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("Server closed the event stream before responding")
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == "":
                # Blank line dispatches the buffered event.
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        parsed = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(parsed, dict) and parsed.get("id") == expected_id:
                        return parsed
                continue
            if text.startswith(":"):
                continue  # SSE comment / keep-alive
            if text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
            # Other SSE fields (event:, id:, retry:) are not needed here.

    def get_tools_as_openai_format(self) -> List[Dict[str, Any]]:
        """Get discovered MCP tools in OpenAI function-calling format.

        Returns:
            List of tool definitions compatible with OpenAI's API
        """
        tools = []
        for tool in self.discovered_tools:
            params = tool.get("input_schema")
            params = _normalize_parameters_schema(params)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"mcp__{tool['server']}__{tool['name']}",
                        "description": f"[MCP: {tool['server']}] {tool.get('description', '')}",
                        "parameters": params,
                    },
                }
            )
        return tools

    async def check_server_health(self):
        """Check if each connected MCP server is still alive.

        For stdio servers: checks if the subprocess has exited (returncode is not None).
        For SSE servers: attempts a lightweight request to the message URL.
        Dead servers are marked with a ``degraded`` flag and a warning is logged.
        """
        import aiohttp

        async def _probe_sse(name: str, info: Dict[str, Any]) -> None:
            message_url = info.get("message_url")
            if not message_url:
                return
            session = info.get("session")
            if session is None or session.closed:
                if not info.get("degraded"):
                    logger.warning("MCP server '%s' (SSE) session is closed", name)
                    info["degraded"] = True
                return
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession() as tmp_session:
                    async with tmp_session.options(message_url, timeout=timeout) as _resp:
                        _resp.raise_for_status()
            except Exception as e:
                if not info.get("degraded"):
                    logger.warning(
                        "MCP server '%s' (SSE) health check failed: %s",
                        name,
                        e,
                    )
                    info["degraded"] = True

        sse_probes = []
        for name, info in list(self.servers.items()):
            transport = info.get("transport", "stdio")

            if transport == "stdio":
                process = info.get("process")
                if process is not None and process.returncode is not None:
                    if not info.get("degraded"):
                        logger.warning(
                            "MCP server '%s' (stdio) appears dead (returncode=%s)",
                            name,
                            process.returncode,
                        )
                        info["degraded"] = True
            elif transport == "sse":
                # Probe SSE servers concurrently — a slow/hung server must not
                # delay the health check for the others (each waits up to 5s).
                sse_probes.append(_probe_sse(name, info))
            elif transport == "http":
                # Streamable HTTP keeps a single aiohttp session; a closed
                # session means the connection is gone.
                session = info.get("session")
                if session is None or session.closed:
                    if not info.get("degraded"):
                        logger.warning("MCP server '%s' (HTTP) session is closed", name)
                        info["degraded"] = True

        if sse_probes:
            await asyncio.gather(*sse_probes, return_exceptions=True)

    async def auto_reconnect_degraded(self):
        """Attempt to reconnect degraded MCP servers.

        Tracks reconnect attempts per server (max 3) and uses exponential
        backoff between attempts. Clears the degraded flag on success.
        """
        import asyncio as _asyncio

        for name, info in list(self.servers.items()):
            if not info.get("degraded"):
                continue

            attempts = self._reconnect_attempts.get(name, 0)
            if attempts >= 3:
                logger.warning(
                    "MCP server '%s' reached max reconnect attempts (3), giving up",
                    name,
                )
                continue

            self._reconnect_attempts[name] = attempts + 1
            backoff = 2 ** (attempts + 1)
            logger.info(
                "Reconnecting MCP server '%s' (attempt %d/3, backoff %ds)…",
                name,
                attempts + 1,
                backoff,
            )
            await _asyncio.sleep(backoff)

            transport = info.get("transport", "stdio")
            conn_params = info.get("_conn_params", {})

            try:
                await self.disconnect(name)
            except Exception:
                # Reconnect path: the old connection is likely already dead;
                # proceed to establish a fresh one regardless.
                logger.debug(f"disconnect of '{name}' before reconnect failed", exc_info=True)

            result: Dict[str, Any]
            if transport == "sse":
                result = await self.connect_sse(name, conn_params.get("url", ""))
            elif transport == "http":
                result = await self.connect_http(
                    name, conn_params.get("url", ""), conn_params.get("headers")
                )
            else:
                result = await self.connect_stdio(
                    name,
                    conn_params.get("command", ""),
                    conn_params.get("args"),
                )

            if result.get("success"):
                self._reconnect_attempts.pop(name, None)
                logger.info("Successfully reconnected to MCP server '%s'", name)
            else:
                logger.warning(
                    "Failed to reconnect MCP server '%s': %s",
                    name,
                    result.get("error"),
                )


def _normalize_parameters_schema(schema: Any) -> Dict[str, Any]:
    """Ensure JSON Schema is OpenAI-tool friendly (object root with properties)."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if out.get("type") is None and "properties" in out:
        out["type"] = "object"
    if out.get("type") != "object":
        # Non-object roots (e.g. union) — wrap for providers expecting object args
        return {"type": "object", "properties": {"value": out}}
    if "properties" not in out:
        out["properties"] = {}
    return out


# Global MCP client instance
mcp_client = MCPClient()


def _cleanup_mcp_servers():
    """Synchronous cleanup of MCP servers on exit.

    Only stdio servers own a child process to reap here; http/sse servers hold
    an async session that can't be awaited from an atexit hook (the loop is
    gone) and is released when the interpreter tears down, so they're skipped.
    """
    for name, info in list(mcp_client.servers.items()):
        proc = info.get("process")
        if proc is None:
            continue
        try:
            if proc.returncode is None:
                proc.kill()
        except Exception:
            logger.debug("Failed to kill MCP server process during atexit cleanup", exc_info=True)
    mcp_client.servers.clear()


atexit.register(_cleanup_mcp_servers)


class MCPConnectParams(BaseModel):
    server_name: str = Field(..., description="Friendly name for this server connection")
    command: str = Field(
        "", description="Command to start the MCP server (e.g., 'npx'), for stdio transport"
    )
    args: Optional[List[str]] = Field(None, description="Arguments for the server command")
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
    headers: Optional[Dict[str, str]] = Field(
        None,
        description=(
            "Extra HTTP headers (e.g. {'Authorization': 'Bearer …'}) sent on every "
            "request for the 'http' transport — used for token-authenticated servers."
        ),
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
    # url/headers are an outbound channel (they can carry exfiltrated data to an
    # attacker-chosen endpoint), so this control-plane call performs egress.
    is_egress = True

    async def execute(  # type: ignore[override]
        self,
        server_name: str,
        command: str = "",
        args: Optional[List[str]] = None,
        transport: str = "stdio",
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Connect to an MCP server."""
        if transport == "sse":
            if not url:
                return {"success": False, "error": "URL is required for SSE transport"}
            result = await mcp_client.connect_sse(server_name, url)
            if result.get("success") and persist:
                persist_mcp_server(server_name, {"transport": "sse", "url": url})
            return result
        if transport == "http":
            if not url:
                return {"success": False, "error": "URL is required for HTTP transport"}
            result = await mcp_client.connect_http(server_name, url, headers)
            if result.get("success") and persist:
                entry: Dict[str, Any] = {"transport": "http", "url": url}
                if headers:
                    entry["headers"] = headers
                persist_mcp_server(server_name, entry)
            return result
        # Launcher allow-list, inline-exec block, blocklist and interactive checks
        # all live in ``connect_stdio`` (via ``validate_stdio_launch``) so this
        # LLM-driven path and config-driven autoconnect share one choke point.
        result = await mcp_client.connect_stdio(server_name, command, args)
        if result.get("success") and persist:
            persist_mcp_server(server_name, {"command": command, "args": list(args or [])})
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

    async def execute(self) -> Dict[str, Any]:  # type: ignore[override]
        """List MCP servers and tools (live connections + effective config)."""
        configured = effective_mcp_servers().get("mcpServers", {})
        servers = {}
        for name, info in mcp_client.servers.items():
            servers[name] = {
                "connected": True,
                "degraded": bool(info.get("degraded")),
                "disabled": bool(configured.get(name, {}).get("disabled")),
                "tools": [t.get("name") for t in info.get("tools", [])],
                "resources": [
                    r.get("uri") for r in mcp_client.discovered_resources if r.get("server") == name
                ],
                "prompts": [
                    p.get("name") for p in mcp_client.discovered_prompts if p.get("server") == name
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
            "total_tools": len(mcp_client.discovered_tools),
            "total_resources": len(mcp_client.discovered_resources),
            "total_prompts": len(mcp_client.discovered_prompts),
        }


# ---------------------------------------------------------------------------
# MCP disconnect
# ---------------------------------------------------------------------------


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

    async def execute(self, server_name: str) -> Dict[str, Any]:  # type: ignore[override]
        try:
            await mcp_client.disconnect(server_name)
            return {"success": True, "message": f"Disconnected from MCP server: {server_name}"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


# ---------------------------------------------------------------------------
# MCP resources
# ---------------------------------------------------------------------------


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

    async def execute(self, server_name: str) -> Dict[str, Any]:  # type: ignore[override]
        return await mcp_client.list_resources(server_name)


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

    async def execute(self, server_name: str, uri: str) -> Dict[str, Any]:  # type: ignore[override]
        return await mcp_client.read_resource(server_name, uri)


# ---------------------------------------------------------------------------
# MCP prompts
# ---------------------------------------------------------------------------


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

    async def execute(self, server_name: str) -> Dict[str, Any]:  # type: ignore[override]
        return await mcp_client.list_prompts(server_name)


class MCPGetPromptParams(BaseModel):
    server_name: str = Field(..., description="Name of the connected MCP server")
    name: str = Field(..., description="Name of the prompt to fetch (from mcp_list_prompts)")
    arguments: Optional[Dict[str, Any]] = Field(
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
        self, server_name: str, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await mcp_client.get_prompt(server_name, name, arguments or {})

"""MCP (Model Context Protocol) client for connecting to external MCP servers."""

import atexit
import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from coderAI.types.provenance import Provenance
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.core.tool_routing import build_mcp_function_name
from coderAI.system.fsperms import atomic_write_json
from coderAI.system.sandbox import prepare_sandbox_launch
from coderAI.tools.base import Tool

logger = logging.getLogger(__name__)

# Launchers permitted for stdio MCP servers. Shared by the ``mcp_connect`` tool
# and the ``coderAI mcp`` CLI so both validate against the same allow-list.
ALLOWED_MCP_LAUNCHERS = {"npx", "node", "python", "python3", "uvx", "bun", "deno"}
MCP_MAX_PAGES = 100
MCP_MAX_LIST_ITEMS = 10_000
MCP_MAX_DESCRIPTION_LENGTH = 1_024
MCP_MAX_METADATA_DEPTH = 12
MCP_MAX_METADATA_ITEMS = 1_000

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


def _launcher_kind(command: str) -> Optional[str]:
    """Return the allow-listed launcher kind, including versioned Python executables."""
    basename = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if basename.endswith(".exe"):
        basename = basename[:-4]
    if basename in ALLOWED_MCP_LAUNCHERS:
        return basename
    # Virtual environments commonly expose python3.10/python3.12 rather than
    # exactly python3. This remains Python-only and does not broaden the launcher set.
    if re.fullmatch(r"python3\.\d+", basename):
        return "python3"
    return None


def _sanitize_metadata_text(value: Any, limit: int = MCP_MAX_DESCRIPTION_LENGTH) -> str:
    """Collapse control/formatting characters and clamp untrusted model metadata."""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _sanitize_model_metadata(value: Any, *, depth: int = 0) -> Any:
    """Bound server-controlled structures returned to the model or used as schemas."""
    if depth >= MCP_MAX_METADATA_DEPTH:
        return None
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MCP_MAX_METADATA_ITEMS:
                break
            safe_key = _sanitize_metadata_text(str(key), 128)
            if not safe_key:
                continue
            if safe_key.lower() in {"description", "title", "$comment"}:
                out[safe_key] = _sanitize_metadata_text(item)
            else:
                out[safe_key] = _sanitize_model_metadata(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [
            _sanitize_model_metadata(item, depth=depth + 1)
            for item in value[:MCP_MAX_METADATA_ITEMS]
        ]
    if isinstance(value, str):
        return _sanitize_metadata_text(value, 4_096)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _validate_discovered_tools(
    server_name: str,
    tools: Any,
    existing_tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Validate exact names and reject duplicate/colliding provider function IDs."""
    if not isinstance(tools, list):
        raise ValueError("MCP tools/list result must contain a tools array")
    if len(tools) > MCP_MAX_LIST_ITEMS:
        raise ValueError(f"MCP server returned more than {MCP_MAX_LIST_ITEMS} tools")

    occupied = {
        build_mcp_function_name(str(item.get("server", "")), str(item.get("name", "")))
        for item in existing_tools
        if item.get("server") != server_name
    }
    seen = set()
    validated: List[Dict[str, Any]] = []
    for raw in tools:
        if not isinstance(raw, dict):
            raise ValueError("MCP tools/list entries must be objects")
        name = raw.get("name")
        if not isinstance(name, str):
            raise ValueError("MCP tool name must be a string")
        function_name = build_mcp_function_name(server_name, name)
        if function_name in seen:
            raise ValueError(f"MCP server returned duplicate tool name {name!r}")
        if function_name in occupied:
            raise ValueError(f"MCP tool function name collision: {function_name!r}")
        seen.add(function_name)
        validated.append(
            {
                "name": name,
                "description": _sanitize_metadata_text(raw.get("description", "")),
                "inputSchema": _sanitize_model_metadata(raw.get("inputSchema", {})),
            }
        )
    return validated


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

    launcher_kind = _launcher_kind(command)
    if launcher_kind is None:
        return (
            f"MCP server launcher '{command}' is not in the allowed set: "
            f"{', '.join(sorted(ALLOWED_MCP_LAUNCHERS))}"
        )

    arg_list = list(args or [])
    blocked_tokens = _INLINE_EXEC_TOKENS.get(launcher_kind, set())
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
    if not parsed.hostname:
        return f"MCP endpoint URL must include a host: {url!r}"
    if parsed.username is not None or parsed.password is not None:
        return "MCP endpoint URLs must not contain embedded credentials"
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


def _validated_same_origin_url(base_url: str, endpoint: str) -> str:
    """Resolve an advertised endpoint and require the exact origin of ``base_url``."""
    from urllib.parse import urljoin, urlparse

    resolved = urljoin(base_url, endpoint.strip())
    error = validate_remote_mcp_url(resolved)
    if error:
        raise ValueError(error)
    base = urlparse(base_url)
    target = urlparse(resolved)

    def origin(parsed: Any) -> tuple[str, str, int]:
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port

    if origin(base) != origin(target):
        raise ValueError(
            f"Refusing cross-origin MCP endpoint {resolved!r} advertised by {base_url!r}"
        )
    if target.username is not None or target.password is not None:
        raise ValueError("MCP endpoints must not contain URL credentials")
    return resolved


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
    """Reject server names that cannot form an exact provider-safe function ID."""
    try:
        build_mcp_function_name(server_name, "t")
    except ValueError as exc:
        return {
            "success": False,
            "error": f"Invalid server_name {server_name!r}: {exc}",
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

    @staticmethod
    def _fail_pending(entry: Dict[str, Any], error: BaseException) -> None:
        """Fail all requests waiting on a transport that reached EOF or closed."""
        pending = entry.get("pending", {})
        for future in list(pending.values()):
            if not future.done():
                future.set_exception(error)
        pending.clear()

    @staticmethod
    def _dispatch_response(entry: Dict[str, Any], response: Dict[str, Any]) -> None:
        """Resolve the future for a JSON-RPC response without consuming notifications."""
        response_id = response.get("id")
        if response_id is None:
            return
        future = entry.get("pending", {}).get(response_id)
        if future is None:
            logger.debug("Ignoring late or unknown MCP response id %r", response_id)
            return
        if not future.done():
            future.set_result(response)

    async def _stdio_reader(
        self, server_name: str, entry: Dict[str, Any], stdout: asyncio.StreamReader
    ) -> None:
        """Sole stdout reader for a stdio connection; dispatch replies by JSON-RPC ID."""
        error: BaseException = RuntimeError(f"MCP server '{server_name}' closed stdout")
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    parsed = json.loads(line.decode("utf-8", errors="replace"))
                except (UnicodeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed JSON from MCP server '%s'", server_name)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Ignoring non-object JSON from MCP server '%s'", server_name)
                    continue
                self._dispatch_response(entry, parsed)
        except asyncio.CancelledError:
            error = RuntimeError(f"MCP server '{server_name}' reader was cancelled")
            raise
        except Exception as exc:
            error = RuntimeError(f"MCP server '{server_name}' stdout reader failed: {exc}")
            logger.debug("MCP stdio reader failed", exc_info=True)
        finally:
            self._fail_pending(entry, error)
            if self.servers.get(server_name) is entry:
                entry["degraded"] = True

    async def _stdio_send(self, entry: Dict[str, Any], payload: Dict[str, Any]) -> None:
        process = entry.get("process")
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeError("MCP stdio process is not running")
        async with entry["write_lock"]:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()

    async def _stdio_exchange(
        self, entry: Dict[str, Any], request: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """Register a request future before writing, then await dispatcher delivery."""
        request_id = request.get("id")
        if request_id is None:
            await self._stdio_send(entry, request)
            return {}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = entry["pending"]
        if request_id in pending:
            raise RuntimeError(f"Duplicate in-flight MCP request id {request_id!r}")
        pending[request_id] = future
        try:
            await self._stdio_send(entry, request)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            future.cancel()
            self._schedule_request_cancellation(entry, request_id)
            raise
        finally:
            pending.pop(request_id, None)

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

    @staticmethod
    def _response_result(response: Any, method: str) -> Dict[str, Any]:
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
        entry: Dict[str, Any],
        method: str,
        item_key: str,
        first_response: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """Collect a cursor-based MCP list with hard page and item limits."""
        items: List[Any] = []
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
        entry: Dict[str, Any],
        init_response: Dict[str, Any],
        tools_response: Dict[str, Any],
    ) -> Dict[str, Any]:
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

        out: Dict[str, Any] = {
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
        candidate_entry: Optional[Dict[str, Any]] = None
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
            launch = prepare_sandbox_launch(full_args, cwd=Path.cwd())
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            stderr_task: Optional["asyncio.Task[None]"] = None
            if process.stderr is not None:
                stderr_task = asyncio.create_task(self._drain_stderr(server_name, process.stderr))
            candidate_entry = {
                "transport": "stdio",
                "process": process,
                "stderr_task": stderr_task,
                "pending": {},
                "write_lock": asyncio.Lock(),
                "_conn_params": {"command": command, "args": args},
            }
            candidate_entry["reader_task"] = asyncio.create_task(
                self._stdio_reader(server_name, candidate_entry, process.stdout)
            )

            init_id = self._get_next_id()
            try:
                init_response = await self._stdio_exchange(
                    candidate_entry, self._init_request(init_id), timeout=10
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to initialize within 10s",
                }

            await self._stdio_send(
                candidate_entry,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            tools_id = self._get_next_id()
            try:
                tools_response = await self._stdio_exchange(
                    candidate_entry,
                    {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                    timeout=10,
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to tools/list",
                }

            result = await self._finish_connect(
                server_name,
                candidate_entry,
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
            if candidate_entry is not None and connection_failed:
                try:
                    await self._close_server_entry(candidate_entry, force=True)
                except Exception:
                    logger.debug(
                        "Failed to close MCP candidate in connect_stdio finally", exc_info=True
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
        return await self._request_entry(
            server_name, self.servers[server_name], method, params, timeout
        )

    def _schedule_request_cancellation(self, entry: Dict[str, Any], request_id: Any) -> None:
        """Best-effort MCP cancellation without delaying local timeout/cancellation."""
        try:
            task = asyncio.create_task(self._send_request_cancellation(entry, request_id))
        except RuntimeError:
            return

        def _consume_result(done: "asyncio.Task[None]") -> None:
            with suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_consume_result)

    async def _send_request_cancellation(self, entry: Dict[str, Any], request_id: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": "client request cancelled"},
        }
        transport = entry.get("transport", "stdio")
        if transport == "stdio":
            await self._stdio_send(entry, payload)
            return
        if transport == "sse":
            import aiohttp

            session = entry.get("session")
            message_url = entry.get("message_url")
            if session and message_url:
                async with session.post(
                    message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        logger.debug("MCP cancellation returned HTTP %s", response.status)
            return
        session = entry.get("session")
        url = entry.get("url")
        if session and url:
            await self._http_send(
                session,
                url,
                payload,
                expected_id=None,
                session_id=entry.get("session_id"),
                timeout=5,
            )

    async def _sse_exchange(
        self, entry: Dict[str, Any], request: Dict[str, Any], timeout: float
    ) -> Dict[str, Any]:
        """POST to a legacy SSE endpoint and await the long-lived stream dispatcher."""
        import aiohttp

        session = entry.get("session")
        message_url = entry.get("message_url")
        if not session or not message_url:
            raise RuntimeError("SSE connection state is invalid")
        request_id = request.get("id")
        future = None
        if request_id is not None:
            future = asyncio.get_running_loop().create_future()
            if request_id in entry["pending"]:
                raise RuntimeError(f"Duplicate in-flight MCP request id {request_id!r}")
            entry["pending"][request_id] = future
        try:
            async with session.post(
                message_url,
                json=request,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status >= 300:
                    body = await response.text()
                    raise RuntimeError(
                        f"MCP SSE message endpoint returned HTTP {response.status}: {body[:300]}"
                    )
                if request_id is None:
                    return {}
                if "application/json" in response.headers.get("Content-Type", ""):
                    parsed = await response.json()
                    if isinstance(parsed, dict):
                        self._dispatch_response(entry, parsed)
            assert future is not None
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if future is not None:
                future.cancel()
            if request_id is not None:
                self._schedule_request_cancellation(entry, request_id)
            raise
        finally:
            if request_id is not None:
                entry["pending"].pop(request_id, None)

    async def _request_entry(
        self,
        server_name: str,
        server: Dict[str, Any],
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """Send one request against an active or not-yet-committed connection entry."""

        transport = server.get("transport", "stdio")
        req_id = self._get_next_id()
        request: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params

        try:
            if transport == "sse":
                response = await self._sse_exchange(server, request, timeout)
            elif transport == "http":
                session = server.get("session")
                url = server.get("url")
                session_id = server.get("session_id")
                if not session or not url:
                    return {
                        "success": False,
                        "error": f"HTTP connection state invalid for '{server_name}'",
                    }
                response, new_session_id = await self._http_send(
                    session,
                    url,
                    request,
                    expected_id=req_id,
                    session_id=session_id,
                    timeout=timeout,
                )
                server["session_id"] = new_session_id
                response = response or {}
            else:
                response = await self._stdio_exchange(server, request, timeout=timeout)
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
            if transport == "http":
                self._schedule_request_cancellation(server, req_id)
            return {
                "success": False,
                "error": f"Request '{method}' to '{server_name}' timed out after {timeout}s",
            }
        except asyncio.CancelledError:
            if transport == "http":
                self._schedule_request_cancellation(server, req_id)
            raise
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": ToolErrorCode.TOOL_ERROR}

        if not isinstance(response, dict):
            return {"success": False, "error": f"MCP {method} returned a non-object response"}
        error = response.get("error")
        if error:
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            return {"success": False, "error": message}
        result = response.get("result", {})
        if not isinstance(result, dict):
            return {"success": False, "error": f"MCP {method} returned a non-object result"}
        return {"success": True, "result": result}

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
        try:
            prompts = await self._paginate_entry(
                server_name, self.servers[server_name], "prompts/list", "prompts"
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        prompts = _sanitize_model_metadata(prompts)
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

    async def _discover_extras_for_entry(
        self, server_name: str, entry: Dict[str, Any]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Stage bounded resource/prompt metadata without mutating global state."""
        caps = (entry.get("server_info") or {}).get("capabilities", {})
        caps = caps if isinstance(caps, dict) else {}
        resources: List[Dict[str, Any]] = []
        prompts: List[Dict[str, Any]] = []
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

    async def _discover_extras(self, server_name: str) -> Dict[str, int]:
        """Refresh extra discovery for an already-connected server."""
        entry = self.servers[server_name]
        resources, prompts = await self._discover_extras_for_entry(server_name, entry)
        self.discovered_resources = [
            item for item in self.discovered_resources if item.get("server") != server_name
        ] + resources
        self.discovered_prompts = [
            item for item in self.discovered_prompts if item.get("server") != server_name
        ] + prompts
        return {"resources": len(resources), "prompts": len(prompts)}

    async def _close_server_entry(self, server: Dict[str, Any], *, force: bool = False) -> None:
        """Close one transport entry and surface cleanup failures to the caller."""
        errors: List[str] = []
        self._fail_pending(server, RuntimeError("MCP connection closed"))
        tasks = [server.get("reader_task"), server.get("stderr_task")]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()

        transport = server.get("transport", "stdio")
        if transport in ("sse", "http"):
            response = server.get("sse_response")
            if response is not None:
                with suppress(Exception):
                    response.close()
            session = server.get("session")
            if session:
                try:
                    await session.close()
                except Exception as exc:
                    errors.append(f"failed to close {transport} session: {exc}")
        else:
            process = server.get("process")
            if process is not None and process.returncode is None:
                try:
                    if force:
                        process.kill()
                    else:
                        process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                        await process.wait()
                    except Exception as exc:
                        errors.append(f"failed to kill stdio process: {exc}")
                except Exception as exc:
                    errors.append(f"failed to stop stdio process: {exc}")

        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not None), return_exceptions=True
            )
        if errors:
            raise RuntimeError("; ".join(errors))

    async def disconnect(self, server_name: str) -> Dict[str, Any]:
        """Disconnect from an MCP server.

        Args:
            server_name: Name of the server to disconnect from

        Returns:
            Result dictionary
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        server = self.servers.pop(server_name)
        self.discovered_tools = [t for t in self.discovered_tools if t.get("server") != server_name]
        self.discovered_resources = [
            r for r in self.discovered_resources if r.get("server") != server_name
        ]
        self.discovered_prompts = [
            p for p in self.discovered_prompts if p.get("server") != server_name
        ]

        try:
            await self._close_server_entry(server)
        except Exception as exc:
            return {
                "success": False,
                "error": f"Disconnected '{server_name}', but cleanup failed: {exc}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        return {"success": True, "message": f"Disconnected from {server_name}"}

    async def _sse_reader(self, server_name: str, entry: Dict[str, Any], response: Any) -> None:
        """Keep the legacy SSE response open and dispatch complete events."""
        event_name = "message"
        data_lines: List[str] = []
        error: BaseException = RuntimeError(f"MCP SSE server '{server_name}' closed the stream")
        try:
            while True:
                line = await response.content.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text == "":
                    if data_lines:
                        data = "\n".join(data_lines)
                        if event_name == "endpoint":
                            future = entry.get("endpoint_future")
                            if future is not None and not future.done():
                                future.set_result(data)
                        else:
                            try:
                                parsed = json.loads(data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Ignoring malformed SSE event from '%s'", server_name
                                )
                            else:
                                if isinstance(parsed, dict):
                                    self._dispatch_response(entry, parsed)
                    event_name = "message"
                    data_lines = []
                    continue
                if text.startswith(":"):
                    continue
                field, _, value = text.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
        except asyncio.CancelledError:
            error = RuntimeError(f"MCP SSE reader for '{server_name}' was cancelled")
            raise
        except Exception as exc:
            error = RuntimeError(f"MCP SSE reader for '{server_name}' failed: {exc}")
            logger.debug("MCP SSE reader failed", exc_info=True)
        finally:
            endpoint_future = entry.get("endpoint_future")
            if endpoint_future is not None and not endpoint_future.done():
                endpoint_future.set_exception(error)
            self._fail_pending(entry, error)
            if self.servers.get(server_name) is entry:
                entry["degraded"] = True

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

        candidate_entry: Optional[Dict[str, Any]] = None
        committed = False
        try:
            session = aiohttp.ClientSession()
            response = await session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                allow_redirects=False,
            )
            if response.status != 200:
                raise RuntimeError(f"SSE endpoint returned HTTP {response.status}")
            endpoint_future = asyncio.get_running_loop().create_future()
            candidate_entry = {
                "transport": "sse",
                "session": session,
                "sse_response": response,
                "sse_url": url,
                "endpoint_future": endpoint_future,
                "pending": {},
                "_conn_params": {"url": url},
            }
            candidate_entry["reader_task"] = asyncio.create_task(
                self._sse_reader(server_name, candidate_entry, response)
            )
            advertised_endpoint = await asyncio.wait_for(
                asyncio.shield(endpoint_future), timeout=10
            )
            message_url = _validated_same_origin_url(url, advertised_endpoint)
            candidate_entry["message_url"] = message_url

            init_id = self._get_next_id()
            init_response = await self._sse_exchange(
                candidate_entry, self._init_request(init_id), timeout=10
            )
            await self._sse_exchange(
                candidate_entry,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=10,
            )

            tools_id = self._get_next_id()
            tools_response = await self._sse_exchange(
                candidate_entry,
                {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                timeout=10,
            )
            result = await self._finish_connect(
                server_name,
                candidate_entry,
                init_response,
                tools_response,
            )
            committed = True
            return result

        except ImportError:
            return {"success": False, "error": "aiohttp is required for SSE transport"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            if candidate_entry is not None and not committed:
                with suppress(Exception):
                    await self._close_server_entry(candidate_entry, force=True)

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

        candidate_entry: Optional[Dict[str, Any]] = None
        committed = False
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
            candidate_entry = {
                "transport": "http",
                "session": session,
                "url": url,
                "session_id": None,
                "headers": headers or {},
                "_conn_params": {"url": url, "headers": headers or {}},
            }

            init_id = self._get_next_id()
            init_response, session_id = await self._http_send(
                session, url, self._init_request(init_id), expected_id=init_id, session_id=None
            )
            if init_response is None:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' returned no response to initialize",
                }

            # The session id (if any) must accompany every subsequent request.
            candidate_entry["session_id"] = session_id
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

            result = await self._finish_connect(
                server_name,
                candidate_entry,
                init_response,
                tools_response,
            )
            committed = True
            return result

        except MCPAuthRequiredError as e:
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
            return {"success": False, "error": "aiohttp is required for HTTP transport"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            if candidate_entry is not None and not committed:
                with suppress(Exception):
                    await self._close_server_entry(candidate_entry, force=True)

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
            allow_redirects=False,
        ) as resp:
            new_session_id = resp.headers.get("Mcp-Session-Id") or session_id
            if resp.status == 401:
                raise MCPAuthRequiredError(resp.headers.get("WWW-Authenticate"))
            if resp.status >= 300:
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
            response_id = parsed.get("id") if isinstance(parsed, dict) else None
            if not isinstance(parsed, dict) or response_id != expected_id:
                raise RuntimeError(
                    f"MCP server returned response id {response_id!r}; expected {expected_id!r}"
                )
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
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True
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
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    mcp_source = True

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
            return await mcp_client.disconnect(server_name)
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

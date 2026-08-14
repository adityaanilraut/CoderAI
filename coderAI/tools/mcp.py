# mypy: disable-error-code="misc"
"""MCP (Model Context Protocol) client for connecting to external MCP servers."""

import atexit
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional


from coderAI.core.tool_routing import build_mcp_function_name
from coderAI.system.fsperms import atomic_write_json
from coderAI.system.proc import kill_process_group, new_session_kwargs  # noqa: F401 - compatibility patch point
from coderAI.tools.mcp_sanitize import (  # noqa: E402 — re-export sanitizers extracted to keep client focused
    _sanitize_metadata_text as _sanitize_metadata_text,
    _sanitize_model_metadata as _sanitize_model_metadata,
)

logger = logging.getLogger(__name__)

# Launchers permitted for stdio MCP servers. Shared by the ``mcp_connect`` tool
# and the ``coderAI mcp`` CLI so both validate against the same allow-list.
ALLOWED_MCP_LAUNCHERS = {"npx", "node", "python", "python3", "uvx", "bun", "deno"}
MCP_MAX_PAGES = 100
MCP_MAX_LIST_ITEMS = 10_000
MCP_MAX_DESCRIPTION_LENGTH = 1_024
MCP_MAX_METADATA_DEPTH = 12
MCP_MAX_METADATA_ITEMS = 1_000
# Property a non-object ``inputSchema`` root is nested under so providers that
# require object-rooted parameters accept it. Stripped again before dispatch.
WRAPPED_ARG_KEY = "value"

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


def _is_inline_exec_arg(launcher_kind: str, token: str) -> bool:
    """Return whether *token* enables the launcher's inline-code mode.

    Python, Node, and Bun accept code attached to short options (``-cCODE`` /
    ``-eCODE``) and Node-style long options accept ``=CODE``. Exact-token-only
    checks are therefore bypassable even though the subprocess is launched
    without a shell.
    """
    blocked = _INLINE_EXEC_TOKENS.get(launcher_kind, set())
    if token in blocked:
        return True
    if launcher_kind in {"python", "python3"}:
        return token.startswith("-c") and len(token) > 2
    if launcher_kind in {"node", "bun"}:
        return (
            (token.startswith("-e") and not token.startswith("--") and len(token) > 2)
            or (token.startswith("-p") and not token.startswith("--") and len(token) > 2)
            or token.startswith("--eval=")
            or token.startswith("--print=")
        )
    return False


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


# (sanitizers imported at top — kept here as comment for git history)


def _validate_discovered_tools(
    server_name: str,
    tools: Any,
    existing_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate exact names, dropping duplicate/colliding/unrepresentable tools.

    Only structural faults in the reply itself (not an array, past the item cap)
    abort the connection. A single tool the provider naming rules cannot express
    — a dot in the name, or a ``mcp__<server>__<tool>`` id over the length limit
    — used to raise straight out of ``connect_*`` and make the whole server
    unusable; such tools are now skipped with a warning so the server's other
    tools still work.
    """
    if not isinstance(tools, list):
        raise ValueError("MCP tools/list result must contain a tools array")
    if len(tools) > MCP_MAX_LIST_ITEMS:
        raise ValueError(f"MCP server returned more than {MCP_MAX_LIST_ITEMS} tools")

    occupied = set()
    for item in existing_tools:
        if item.get("server") == server_name:
            continue
        try:
            occupied.add(
                build_mcp_function_name(str(item.get("server", "")), str(item.get("name", "")))
            )
        except ValueError:
            continue
    seen = set()
    skipped: list[str] = []
    validated: list[dict[str, Any]] = []
    for raw in tools:
        if not isinstance(raw, dict):
            skipped.append("<non-object entry>")
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            skipped.append(f"<non-string name {name!r}>")
            continue
        try:
            function_name = build_mcp_function_name(server_name, name)
        except ValueError as exc:
            skipped.append(f"{name} ({exc})")
            continue
        if function_name in seen:
            skipped.append(f"{name} (duplicate)")
            continue
        if function_name in occupied:
            skipped.append(f"{name} (function name collision)")
            continue
        seen.add(function_name)
        validated.append(
            {
                "name": name,
                "description": _sanitize_metadata_text(raw.get("description", "")),
                "inputSchema": _sanitize_model_metadata(raw.get("inputSchema", {})),
            }
        )
    if skipped:
        logger.warning(
            "Skipped %d unusable tool(s) from MCP server '%s': %s",
            len(skipped),
            server_name,
            "; ".join(_sanitize_metadata_text(s, 128) for s in skipped[:20]),
        )
    return validated


def validate_stdio_launch(command: str, args: Optional[list[str]]) -> Optional[str]:
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
    for token in arg_list:
        if _is_inline_exec_arg(launcher_kind, token):
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


def _parse_ip_literal(host: str) -> Optional[Any]:
    """Parse normal and legacy IPv4/IPv6 URL literals, else return ``None``.

    HTTP stacks commonly accept legacy integer/short IPv4 spellings such as
    ``2130706433`` and ``127.1``. Treating those as DNS names would let private
    address checks be bypassed.
    """
    import ipaddress
    import socket

    candidate = (host or "").strip().lower()
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    if ":" in candidate:
        return None
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(candidate)))
    except OSError:
        return None


def validate_remote_mcp_url(url: str) -> Optional[str]:
    """Validate a remote MCP/OAuth endpoint URL.

    Returns an error string when *url* is not an acceptable remote endpoint, or
    ``None`` when it is. Requires ``https://`` for every remote host; plaintext
    ``http://`` is allowed only for loopback dev hosts (``127.0.0.1``/``localhost``).
    This is the single scheme gate shared by ``connect_http``/``connect_sse``, the
    ``coderAI mcp add`` CLI, and the OAuth discovery/token calls, so an untrusted
    ``mcp_servers.json`` cannot downgrade a connection or an OAuth token exchange
    onto the network in cleartext. Non-loopback private, link-local, reserved,
    and legacy-encoded IP literals are rejected even when they use HTTPS.
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
    try:
        _port = parsed.port
    except ValueError:
        return f"MCP endpoint URL has an invalid port: {url!r}"
    if parsed.username is not None or parsed.password is not None:
        return "MCP endpoint URLs must not contain embedded credentials"
    host = parsed.hostname or ""
    literal_ip = _parse_ip_literal(host)
    if literal_ip is not None:
        if literal_ip.is_loopback and not _is_loopback_host(host):
            return f"Refusing ambiguous legacy loopback address {host!r}"
        if not (literal_ip.is_global or literal_ip.is_loopback):
            return f"Refusing non-public MCP/OAuth endpoint address {host!r}"
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


def _shape_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shape a ``tools/call`` result into the tool-facing dict.

    Concatenates text content parts and appends bounded placeholders for
    non-text parts (image/audio/resource) plus a short ``structuredContent``
    summary so multimodal MCP replies are not silently dropped.
    """
    parts: list[str] = []
    for part in result.get("content", []) or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            parts.append(str(part.get("text", "")))
        elif ptype == "image":
            mime = part.get("mimeType") or "image"
            data = part.get("data") or ""
            size = len(data) if isinstance(data, str) else 0
            parts.append(f"[MCP image: {mime}; {size} chars base64 omitted]")
        elif ptype == "audio":
            mime = part.get("mimeType") or "audio"
            data = part.get("data") or ""
            size = len(data) if isinstance(data, str) else 0
            parts.append(f"[MCP audio: {mime}; {size} chars base64 omitted]")
        elif ptype in ("resource", "resource_link"):
            uri = part.get("uri") or (part.get("resource") or {}).get("uri") or ""
            parts.append(f"[MCP resource: {uri}]")
        else:
            parts.append(f"[MCP content type={ptype!r}]")
    structured = result.get("structuredContent")
    if structured is not None:
        try:
            summary = json.dumps(structured, default=str)
        except (TypeError, ValueError):
            summary = str(structured)
        if len(summary) > 2_000:
            summary = summary[:1_997] + "..."
        parts.append(f"[MCP structuredContent: {summary}]")
    text_content = "".join(parts)
    is_error = bool(result.get("isError"))
    out: dict[str, Any] = {"success": not is_error, "content": text_content, "raw": result}
    if is_error:
        out["error"] = text_content or "MCP tool returned an error."
    return out


def _reject_reserved_server_name(server_name: str) -> Optional[dict[str, Any]]:
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


def bundled_mcp_servers() -> dict[str, dict[str, Any]]:
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
            "args": ["-I", "-m", "coderAI.mcp_servers.git_extended"],
            "bundled": True,
        }
    }


def load_mcp_servers() -> dict[str, Any]:
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


def effective_mcp_servers(
    *,
    project_root: Optional[str | Path] = None,
    workspace_trusted: Optional[bool] = None,
) -> dict[str, Any]:
    """Merged MCP config across bundled / user / project / local scopes.

    Precedence (same name): local → project → user → bundled.
    Project ``.mcp.json`` servers require workspace trust and approval.
    See :mod:`coderAI.tools.mcp_config`.
    """
    from coderAI.tools.mcp_config import effective_mcp_servers as _effective

    return _effective(project_root=project_root, workspace_trusted=workspace_trusted)


def persist_mcp_server(name: str, entry: dict[str, Any]) -> None:
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


def set_mcp_server_disabled(
    name: str,
    disabled: bool,
    *,
    project_root: Optional[str | Path] = None,
) -> bool:
    """Flip the ``disabled`` flag on a configured MCP server, in its own scope.

    A disabled server stays in its config file but is skipped by
    ``ExecutionLoop._autoconnect_mcp_servers`` on startup, so it does not
    auto-reconnect until re-enabled. Enabling simply removes the flag (absence
    means enabled) to keep the on-disk config tidy.

    The scope that owns the entry has to be the one written, because the merge
    in :func:`effective_mcp_servers` runs local → project → user → bundled: a
    flag written to ``mcp_servers.json`` for a name that a higher-precedence
    scope also defines is simply overwritten and the toggle appears to do
    nothing. Local entries are edited in place; project ``.mcp.json`` is a
    shared repo file, so its servers are toggled through the per-user approval
    store instead (rejected servers are skipped by autoconnect).

    Bundled servers (e.g. ``git_extended``) may not have an on-disk entry yet;
    disabling them writes a stub so the override sticks. Returns ``False`` only
    when the name is configured in no scope and is not bundled.
    """
    from coderAI.tools.mcp_config import (
        load_local_mcp_servers,
        load_project_mcp_servers,
        set_local_mcp_disabled,
        set_project_approval,
    )

    root = Path(project_root or ".").resolve()

    if name in load_local_mcp_servers(root):
        return set_local_mcp_disabled(name, disabled, project_root=root)

    project_servers = load_project_mcp_servers(root)
    if name in project_servers:
        set_project_approval(
            name,
            approved=not disabled,
            entry=project_servers[name],
            project_root=root,
        )
        return True

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


def save_mcp_servers(data: dict[str, Any]) -> None:
    """Write the MCP server config as pretty-printed JSON.

    Writes to a temp file in the same directory and ``os.replace``s it into
    place so a crash or a concurrent writer can never leave a truncated file.
    ``load_mcp_servers`` silently treats a corrupt file as empty, so a
    non-atomic write would risk wiping every configured server. Mirrors the
    atomic-save pattern in ``system.config.ConfigManager.save``.
    """
    atomic_write_json(mcp_servers_path(), data)


from coderAI.tools.mcp_discovery import (  # noqa: E402
    MCPCatalog,
    _normalize_parameters_schema as _normalize_parameters_schema,
    normalize_parameters_schema_ex as normalize_parameters_schema_ex,
)
from coderAI.tools.mcp_remote_transport import MCPRemoteTransport  # noqa: E402
from coderAI.tools.mcp_session import MCPSession  # noqa: E402
from coderAI.tools.mcp_stdio_transport import MCPStdioTransport  # noqa: E402


class MCPClient(MCPSession, MCPCatalog, MCPStdioTransport, MCPRemoteTransport):
    """Client for connecting to MCP servers and discovering tools.

    Supports stdio, SSE, and Streamable HTTP transports for connecting to
    MCP-compatible servers. Discovered tools are registered in the CoderAI tool
    registry.
    """

    PREFERRED_PROTOCOL_VERSION = "2025-03-26"
    FALLBACK_PROTOCOL_VERSION = "2024-11-05"

    # Other SSE fields (event:, id:, retry:) are not needed here.


# Global MCP client instance
mcp_client = MCPClient()


def _cleanup_mcp_servers():
    """Synchronous cleanup of MCP servers on exit.

    Only stdio servers own a child process to reap here; http/sse servers hold
    an async session that can't be awaited from an atexit hook (the loop is
    gone) and is released when the interpreter tears down, so they're skipped.
    """
    for _name, info in list(mcp_client.servers.items()):
        proc = info.get("process")
        if proc is None:
            continue
        try:
            if proc.returncode is None:
                kill_process_group(proc)
        except Exception:
            logger.debug("Failed to kill MCP server process during atexit cleanup", exc_info=True)
    mcp_client.servers.clear()


atexit.register(_cleanup_mcp_servers)

from coderAI.tools.mcp_native_tools import (  # noqa: E402, F401
    MCPConnectParams,
    MCPConnectTool,
    MCPDisconnectParams,
    MCPDisconnectTool,
    MCPGetPromptParams,
    MCPGetPromptTool,
    MCPListParams,
    MCPListPromptsParams,
    MCPListPromptsTool,
    MCPListResourcesParams,
    MCPListResourcesTool,
    MCPListTool,
    MCPReadResourceParams,
    MCPReadResourceTool,
)


# ---------------------------------------------------------------------------
# MCP disconnect
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP resources
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP prompts
# ---------------------------------------------------------------------------

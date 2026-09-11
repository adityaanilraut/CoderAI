# Ported from coderai/cli/mcp_cmd.py - kimi structure (cli/mcp.py).
"""``coderai mcp`` subcommand (Kimi ``cli/mcp.py`` parity, stdlib only).

Manages the global ``~/.coderai/mcp.json`` registry (Kimi: ``~/.kimi/mcp.json``):

- ``add --transport stdio <name> -- <command> [args...] [--env KEY=VALUE ...]``
- ``add --transport http <name> <url> [--header 'K: V' ...]``
- ``remove <name>``
- ``list``
- ``test <name>`` — validates the entry shape (live connect stays interactive)
- ``login <name> [--token T | --token-env VAR | --token-file PATH]``
- ``logout <name>``

Remote OAuth servers (``"auth": "oauth"``) authenticate with a pre-obtained
bearer token: ``login`` stores it under ``~/.coderai/mcp-oauth/`` (0600) and
points the server entry at it. No browser flow is vendored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coderai.mcp.files import (
    get_global_mcp_config_file,
    merge_server_cfg,
    try_load_mcp_servers_file,
)


def _read_global() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return ``(raw_config, servers)`` for the global file (tolerates absence)."""
    path = get_global_mcp_config_file()
    if not path.is_file():
        return {"mcpServers": {}}, {}
    servers, error = try_load_mcp_servers_file(path)
    if error:
        print(f"Warning: {error}", flush=True)
        return {"mcpServers": {}}, {}
    return {"mcpServers": servers}, servers


def _write_global(servers: dict[str, dict[str, Any]]) -> Path:
    """Persist ``servers`` to the global file (atomic tmp → replace)."""
    from coderai.utils.io import atomic_json_write

    path = get_global_mcp_config_file()
    atomic_json_write({"mcpServers": servers}, path)
    return path


def _parse_kv(items: list[str], *, separator: str, label: str) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    for item in items:
        if separator not in item:
            print(f"Invalid {label} format: {item!r} (expected KEY{separator}VALUE).")
            return None
        key, value = item.split(separator, 1)
        if label == "header":
            key, value = key.strip(), value.strip()
        if not key:
            print(f"Invalid {label} format: {item!r} (empty key).")
            return None
        parsed[key] = value
    return parsed


def cmd_mcp_add(argv: list[str]) -> int:
    """Implement ``coderai mcp add``. Returns a process exit code."""
    transport = "stdio"
    env_items: list[str] = []
    header_items: list[str] = []
    positional: list[str] = []
    idx = 0
    while idx < len(argv):
        tok = argv[idx]
        if tok in ("--transport", "-t") and idx + 1 < len(argv):
            transport = argv[idx + 1].lower()
            idx += 2
        elif tok.startswith("--transport="):
            transport = tok.split("=", 1)[1].lower()
            idx += 1
        elif tok in ("--env", "-e") and idx + 1 < len(argv):
            env_items.append(argv[idx + 1])
            idx += 2
        elif tok.startswith("--env="):
            env_items.append(tok.split("=", 1)[1])
            idx += 1
        elif tok in ("--header", "-H") and idx + 1 < len(argv):
            header_items.append(argv[idx + 1])
            idx += 2
        elif tok.startswith("--header="):
            header_items.append(tok.split("=", 1)[1])
            idx += 1
        elif tok == "--":
            positional.extend(argv[idx + 1 :])
            break
        elif tok.startswith("-"):
            print(f"Unknown option for 'mcp add': {tok}")
            return 2
        else:
            positional.append(tok)
            idx += 1

    if transport not in ("stdio", "http"):
        print(f"Unsupported transport: {transport} (expected 'stdio' or 'http').")
        return 2
    if not positional:
        print("Usage: coderai mcp add [--transport stdio|http] <name> [--] <target...>")
        return 2
    name, targets = positional[0], positional[1:]

    if transport == "stdio":
        if not targets:
            print("For stdio transport, provide the command after `--` (e.g. `-- npx server`).")
            return 2
        if header_items:
            print("--header is only valid for http transport.")
            return 2
        env = _parse_kv(env_items, separator="=", label="env")
        if env is None:
            return 2
        server_config: dict[str, Any] = {"command": targets[0], "args": targets[1:]}
        if env:
            server_config["env"] = env
    else:
        if env_items:
            print("--env is only supported for stdio transport.")
            return 2
        if len(targets) != 1:
            print("For http transport, provide exactly one URL.")
            return 2
        headers = _parse_kv(header_items, separator=":", label="header")
        if headers is None:
            return 2
        server_config = {"url": targets[0], "transport": "http"}
        if headers:
            server_config["headers"] = headers

    _raw, servers = _read_global()
    servers[name] = (
        merge_server_cfg(servers[name], server_config) if name in servers else server_config
    )
    path = _write_global(servers)
    print(f"Added MCP server '{name}' to {path}.")
    return 0


def cmd_mcp_remove(argv: list[str]) -> int:
    """Implement ``coderai mcp remove <name>``."""
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai mcp remove <name>")
        return 2
    name = argv[0]
    _raw, servers = _read_global()
    if name not in servers:
        print(f"MCP server '{name}' not found.")
        return 1
    del servers[name]
    path = _write_global(servers)
    print(f"Removed MCP server '{name}' from {path}.")
    return 0


def cmd_mcp_list(argv: list[str]) -> int:
    """Implement ``coderai mcp list`` (``--json`` emits the registry)."""
    as_json = argv and argv[0] == "--json"
    path = get_global_mcp_config_file()
    _raw, servers = _read_global()
    if as_json:
        print(json.dumps({"config_file": str(path), "mcpServers": servers}, indent=2))
        return 0
    print(f"MCP config file: {path}")
    if not servers:
        print("No MCP servers configured.")
        return 0
    for name in sorted(servers):
        server = servers[name]
        if "command" in server:
            line = f"{name} (stdio): {server['command']} {' '.join(server.get('args') or [])}".rstrip()
        elif "url" in server:
            line = f"{name} ({server.get('transport') or 'http'}): {server['url']}"
            if server.get("auth") == "oauth":
                from coderai.mcp_oauth import has_server_token

                line += (
                    " [oauth token stored]"
                    if has_server_token(name, server)
                    else " [oauth: no token — run 'coderai mcp login NAME']"
                )
        else:
            line = f"{name}: {server}"
        print(f"  {line}")
    return 0


def cmd_mcp_test(argv: list[str]) -> int:
    """Validate an MCP entry shape (no live connection without fastmcp)."""
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai mcp test <name>")
        return 2
    name = argv[0]
    _raw, servers = _read_global()
    server = servers.get(name)
    if server is None:
        print(f"MCP server '{name}' not found.")
        return 1
    if "command" in server:
        print(f"✓ '{name}' stdio entry looks valid: {server['command']}")
        return 0
    if "url" in server:
        print(f"✓ '{name}' http entry looks valid: {server['url']}")
        if server.get("auth") == "oauth":
            from coderai.mcp_oauth import has_server_token

            if has_server_token(name, server):
                print("  ✓ bearer token configured for OAuth.")
            else:
                print(f"  Note: no bearer token stored; run 'coderai mcp login {name}'.")
        return 0
    print(f"✗ '{name}' has no 'command' or 'url'; entry is invalid.")
    return 1


def run_mcp(argv: list[str]) -> int:
    """Dispatch ``coderai mcp ...``. Returns a process exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "Usage: coderai mcp <add|remove|list|test|login|logout> [options]\n"
            "\n"
            "  add     Add a server to ~/.coderai/mcp.json\n"
            "  remove  Remove a server from ~/.coderai/mcp.json\n"
            "  list    List configured servers [--json]\n"
            "  test    Validate a server entry\n"
            "  login   Store a bearer token for an OAuth server\n"
            "  logout  Drop the stored bearer token for a server\n"
            "\n"
            "Examples:\n"
            "  coderai mcp add --transport stdio my-tools -- npx my-mcp-server\n"
            "  coderai mcp add --transport http ctx7 https://mcp.context7.com/mcp\n"
            "  coderai mcp login ctx7\n"
            "  coderai mcp list\n"
        )
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "add":
        return cmd_mcp_add(rest)
    if sub == "remove":
        return cmd_mcp_remove(rest)
    if sub == "list":
        return cmd_mcp_list(rest)
    if sub == "test":
        return cmd_mcp_test(rest)
    if sub in ("auth", "reset-auth"):
        print(
            f"'coderai mcp {sub}' was renamed: use 'coderai mcp login <name>' "
            "or 'coderai mcp logout <name>'."
        )
        return 2
    if sub == "login":
        return cmd_mcp_login(rest)
    if sub == "logout":
        return cmd_mcp_logout(rest)
    print(f"Unknown 'mcp' subcommand: {sub} (expected add|remove|list|test|login|logout).")
    return 2


def _login_usage() -> int:
    print(
        "Usage: coderai mcp login <name> [--token TOKEN | --token-env VAR | --token-file PATH]\n"
        "  Stores a bearer token for an OAuth-protected server (no browser flow)."
    )
    return 2


def cmd_mcp_login(argv: list[str]) -> int:
    """Store a bearer token for an ``auth: oauth`` server."""
    from coderai.mcp_oauth import has_server_token, store_server_token

    token: str | None = None
    token_env: str | None = None
    token_file: str | None = None
    positional: list[str] = []
    idx = 0
    while idx < len(argv):
        tok = argv[idx]
        if tok == "--token" and idx + 1 < len(argv):
            token = argv[idx + 1]
            idx += 2
        elif tok.startswith("--token="):
            token = tok.split("=", 1)[1]
            idx += 1
        elif tok == "--token-env" and idx + 1 < len(argv):
            token_env = argv[idx + 1]
            idx += 2
        elif tok.startswith("--token-env="):
            token_env = tok.split("=", 1)[1]
            idx += 1
        elif tok == "--token-file" and idx + 1 < len(argv):
            token_file = argv[idx + 1]
            idx += 2
        elif tok.startswith("--token-file="):
            token_file = tok.split("=", 1)[1]
            idx += 1
        elif tok.startswith("-"):
            print(f"Unknown option for 'mcp login': {tok}")
            return 2
        else:
            positional.append(tok)
            idx += 1
    if len(positional) != 1:
        return _login_usage()
    if sum(x is not None for x in (token, token_env, token_file)) > 1:
        print("Pick one of --token, --token-env, --token-file.")
        return 2
    name = positional[0]
    _raw, servers = _read_global()
    server = servers.get(name)
    if server is None:
        print(f"MCP server '{name}' not found.")
        return 1
    if "url" not in server:
        print(f"MCP server '{name}' is not a remote (url) server.")
        return 1
    if token_env:
        server.pop("token", None)
        server.pop("tokenFile", None)
        server["tokenEnv"] = token_env
        server["auth"] = "oauth"
        _write_global(servers)
        print(f"Server '{name}' will read its bearer token from ${token_env}.")
        return 0
    if token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            print(f"Token file not found: {path}")
            return 1
        server.pop("token", None)
        server.pop("tokenEnv", None)
        server["tokenFile"] = str(path)
        server["auth"] = "oauth"
        _write_global(servers)
        print(f"Server '{name}' will read its bearer token from {path}.")
        return 0
    if token is None:
        import getpass

        try:
            token = getpass.getpass(f"Bearer token for '{name}': ").strip()
        except (EOFError, KeyboardInterrupt):
            print("Cancelled.")
            return 1
        if not token:
            print("Empty token; nothing stored.")
            return 1
    stored = store_server_token(name, token)
    server.pop("token", None)
    server.pop("tokenEnv", None)
    server["tokenFile"] = str(stored)
    server["auth"] = "oauth"
    _write_global(servers)
    if has_server_token(name, server):
        print(f"Stored bearer token for '{name}' ({stored}).")
        return 0
    print(f"Failed to verify the stored token for '{name}'.")
    return 1


def cmd_mcp_logout(argv: list[str]) -> int:
    """Drop the stored bearer token for a server (config keys preserved)."""
    from coderai.mcp_oauth import clear_server_token, oauth_token_dir

    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai mcp logout <name>")
        return 2
    name = argv[0]
    _raw, servers = _read_global()
    server = servers.get(name)
    if server is None:
        print(f"MCP server '{name}' not found.")
        return 1
    removed = clear_server_token(name)
    token_file = server.get("tokenFile", "")
    if token_file:
        try:
            managed = Path(token_file).expanduser().resolve().is_relative_to(
                oauth_token_dir().resolve()
            )
        except OSError:
            managed = False
        if managed:
            server.pop("tokenFile", None)
    if server.pop("token", None) is not None:
        removed = True
    _write_global(servers)
    print(f"Cleared bearer token for '{name}'." if removed else f"No stored token for '{name}'.")
    return 0

"""CLI subcommands for managing MCP (Model Context Protocol) servers.

Reads and writes ``~/.coderAI/mcp_servers.json`` — the same file the ``coderAI
setup`` wizard writes and that ``coderAI chat`` auto-connects on startup
(``ExecutionLoop._autoconnect_mcp_servers``). Servers added here become
available the next time you start a chat.
"""

import sys
from collections.abc import Sequence
from typing import Any, cast

import click

from coderAI.tools.mcp import (
    ALLOWED_MCP_LAUNCHERS,
    load_mcp_servers,
    mcp_servers_path,
    save_mcp_servers,
)
from coderAI.ui.display import Display


def _launcher_allowed(command: str) -> bool:
    """Mirror ``MCPConnectTool``'s launcher check (bare name or ``/path/to/name``)."""
    cmd_lower = command.lower()
    return any(
        cmd_lower == launcher or cmd_lower.endswith("/" + launcher)
        for launcher in ALLOWED_MCP_LAUNCHERS
    )


@click.group(invoke_without_command=True)
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Manage MCP (Model Context Protocol) servers."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _stdio_entry(launcher: str, args: Sequence[str], display: Display) -> dict[str, Any]:
    """Build a validated stdio server entry, exiting with an error if disallowed."""
    if not _launcher_allowed(launcher):
        display.print_error(
            f"Launcher '{launcher}' is not allowed. Use one of: "
            f"{', '.join(sorted(ALLOWED_MCP_LAUNCHERS))}"
        )
        sys.exit(2)
    return {"command": launcher, "args": list(args)}


def _parse_headers(headers: Sequence[str], display: Display) -> dict[str, str]:
    """Parse repeated ``--header 'Key: Value'`` flags into a dict.

    Exits with an error on a malformed header so the user gets immediate
    feedback instead of a silently-dropped auth token.
    """
    out: dict[str, str] = {}
    for raw in headers:
        if ":" not in raw:
            display.print_error(f"Invalid --header {raw!r}; expected 'Key: Value'.")
            sys.exit(2)
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key:
            display.print_error(f"Invalid --header {raw!r}; empty header name.")
            sys.exit(2)
        out[key] = value.strip()
    return out


@mcp.command("add", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "sse", "http"]),
    default=None,
    help="Transport type (default: stdio when a command is given; sse/http with --sse/--http).",
)
@click.option(
    "--command",
    "-c",
    help="Launcher for stdio transport (e.g. npx). Alternative to passing it after '--'.",
)
@click.option(
    "--args",
    "args_str",
    default="",
    help="Comma-separated arguments for --command (stdio transport).",
)
@click.option(
    "--sse",
    "sse_url",
    help="SSE endpoint URL — selects SSE transport instead of stdio.",
)
@click.option(
    "--http",
    "http_url",
    help="Streamable HTTP endpoint URL (e.g. https://host/mcp) — selects HTTP transport.",
)
@click.option(
    "--header",
    "-H",
    "header_list",
    multiple=True,
    help="Header 'Key: Value' for HTTP transport (e.g. -H 'Authorization: Bearer …'). Repeatable.",
)
def mcp_add(
    name: str,
    command_parts: tuple[str, ...],
    transport: str | None,
    command: str | None,
    args_str: str,
    sse_url: str | None,
    http_url: str | None,
    header_list: tuple[str, ...],
) -> None:
    """Add (or overwrite) an MCP server named NAME.

    \b
    Pass the launcher (and its args), or a remote URL, after ``--``:
        coderAI mcp add fetch --transport stdio -- npx -y @scope/server
        coderAI mcp add remote --transport sse -- https://example.com/sse
        coderAI mcp add strava --transport http -- https://mcp.strava.com/mcp

    \b
    Or use explicit flags:
        coderAI mcp add fetch --command npx --args "-y,@scope/server"
        coderAI mcp add remote --sse https://example.com/sse
        coderAI mcp add api --http https://host/mcp -H "Authorization: Bearer TOKEN"
    """
    from coderAI.ui.display import display

    # ``__`` is reserved for the ``mcp__<server>__<tool>`` id encoding.
    if "__" in name:
        display.print_error(
            f"Server name must not contain '__' (reserved for MCP tool ids): {name!r}"
        )
        sys.exit(2)

    explicit_targets = [t for t in (command, sse_url, http_url) if t]
    if command_parts and explicit_targets:
        display.print_error(
            "Pass the command/URL after '--' OR use --command/--sse/--http, not both."
        )
        sys.exit(2)
    if len(explicit_targets) > 1:
        display.print_error("Use exactly one of --command (stdio), --sse, or --http.")
        sys.exit(2)

    header_dict = _parse_headers(header_list, display)

    entry: dict[str, Any]
    if command_parts:
        # Claude-Code-style: `... --transport http -- https://host/mcp`
        effective = transport or "stdio"
        if effective in ("sse", "http"):
            if len(command_parts) != 1:
                display.print_error(
                    f"{effective.upper()} transport takes a single URL after '--', got: "
                    f"{' '.join(command_parts)}"
                )
                sys.exit(2)
            entry = {"transport": effective, "url": command_parts[0]}
        else:
            entry = _stdio_entry(command_parts[0], command_parts[1:], display)
    elif http_url:
        if transport and transport != "http":
            display.print_error("--http implies HTTP transport; remove --transport.")
            sys.exit(2)
        entry = {"transport": "http", "url": http_url}
    elif sse_url:
        if transport and transport != "sse":
            display.print_error("--sse implies SSE transport; remove --transport.")
            sys.exit(2)
        entry = {"transport": "sse", "url": sse_url}
    elif command:
        if transport and transport != "stdio":
            display.print_error("--command implies stdio transport; remove --transport.")
            sys.exit(2)
        args = [a.strip() for a in args_str.split(",") if a.strip()]
        entry = _stdio_entry(command, args, display)
    else:
        display.print_error(
            "Provide a command after '--' (e.g. -- npx -y @scope/server), "
            "or use --command <launcher> / --sse <url> / --http <url>."
        )
        sys.exit(2)

    if header_dict:
        if entry.get("transport") != "http":
            display.print_error("--header is only supported for the 'http' transport.")
            sys.exit(2)
        entry["headers"] = header_dict

    data = load_mcp_servers()
    servers = data.setdefault("mcpServers", {})
    if name in servers:
        display.print_warning(f"Overwriting existing MCP server '{name}'")
    servers[name] = entry
    save_mcp_servers(data)

    display.print_success(f"Added MCP server '{name}' to {mcp_servers_path()}")
    display.print_info("It will connect on the next `coderAI chat`.")


@mcp.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    from coderAI.ui.display import display

    servers = load_mcp_servers().get("mcpServers", {})
    if not servers:
        display.print_info(f"No MCP servers configured ({mcp_servers_path()}).")
        return

    from coderAI.tools.mcp_oauth import has_credentials

    rows = []
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        if transport in ("sse", "http"):
            target = cfg.get("url", "")
            args = ""
        else:
            target = cfg.get("command", "")
            args = " ".join(cfg.get("args", []) or [])
        if transport == "http":
            auth = "logged in" if has_credentials(name) else "—"
        else:
            auth = "n/a"
        rows.append(
            {
                "Name": name,
                "Transport": transport,
                "Command/URL": target,
                "Args": args,
                "Auth": auth,
            }
        )

    display.print_table(rows, "Configured MCP servers")
    display.print_info(
        "These are configured servers; live connection status is shown inside a chat session."
    )


@mcp.command("remove")
@click.argument("name")
def mcp_remove(name: str) -> None:
    """Remove the MCP server named NAME."""
    from coderAI.ui.display import display

    data = load_mcp_servers()
    servers = data.get("mcpServers", {})
    if name not in servers:
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    del servers[name]
    save_mcp_servers(data)
    # Drop any saved OAuth credentials so a removed server leaves nothing behind.
    from coderAI.tools.mcp_oauth import delete_credentials

    delete_credentials(name)
    display.print_success(f"Removed MCP server '{name}'.")


@mcp.command("login")
@click.argument("name")
@click.option("--client-id", help="Pre-issued OAuth client id (for servers without registration).")
@click.option("--client-secret", help="OAuth client secret, if the client is confidential.")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="OAuth scope to request (repeatable). Defaults to the server's advertised scopes.",
)
def mcp_login(
    name: str,
    client_id: str | None,
    client_secret: str | None,
    scopes: tuple[str, ...],
) -> None:
    """Authorize an HTTP MCP server via OAuth and save the credentials.

    Opens your browser to the provider's login page, then stores the access and
    refresh tokens in ~/.coderAI/mcp_credentials.json (0600). After this, every
    `coderAI chat` reconnects silently — no browser — until you `mcp logout`.
    """
    from coderAI.ui.display import display
    from coderAI.tools import mcp_oauth

    servers = load_mcp_servers().get("mcpServers", {})
    entry = servers.get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'. Add it first with `mcp add`.")
        sys.exit(1)
    if entry.get("transport") != "http":
        display.print_error(
            f"OAuth login only applies to HTTP MCP servers; '{name}' uses "
            f"{entry.get('transport', 'stdio')!r}."
        )
        sys.exit(2)
    url = entry.get("url")
    if not url:
        display.print_error(f"Server '{name}' has no url configured.")
        sys.exit(2)

    display.print_info(f"Discovering authorization server for '{name}' …")
    try:
        record = mcp_oauth.login(
            name,
            url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=list(scopes) or None,
        )
    except mcp_oauth.OAuthError as e:
        display.print_error(f"Login failed: {e}")
        sys.exit(1)
    except Exception as e:  # network / browser / unexpected
        display.print_error(f"Login failed: {e}")
        sys.exit(1)

    scope_note = f" (scopes: {record['scope']})" if record.get("scope") else ""
    display.print_success(f"Authorized '{name}'{scope_note}.")
    display.print_info("It will connect automatically on the next `coderAI chat`.")


@mcp.command("logout")
@click.argument("name")
def mcp_logout(name: str) -> None:
    """Revoke and delete saved OAuth credentials for an MCP server."""
    from coderAI.ui.display import display
    from coderAI.tools.mcp_oauth import logout

    if logout(name):
        display.print_success(f"Logged out of MCP server '{name}'.")
    else:
        display.print_info(f"No saved credentials for '{name}'.")


async def _connect_and_list(name: str, entry: dict[str, Any], kind: str) -> dict[str, Any]:
    """Connect transiently to a configured server, list resources/prompts, disconnect."""
    from coderAI.tools.mcp import mcp_client

    transport = entry.get("transport", "stdio")
    if transport == "sse":
        conn = await mcp_client.connect_sse(name, entry.get("url", ""))
    elif transport == "http":
        conn = await mcp_client.connect_http(
            name, entry.get("url", ""), entry.get("headers") or None
        )
    else:
        conn = await mcp_client.connect_stdio(
            name, entry.get("command", ""), entry.get("args") or []
        )
    if not conn.get("success"):
        return conn
    try:
        if kind == "resources":
            return await mcp_client.list_resources(name)
        return await mcp_client.list_prompts(name)
    finally:
        await mcp_client.disconnect(name)


def _require_server(name: str, display: Display) -> dict[str, Any]:
    """Return the configured entry for NAME or exit with an error."""
    entry = load_mcp_servers().get("mcpServers", {}).get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'. Add it first with `mcp add`.")
        sys.exit(1)
    return cast("dict[str, Any]", entry)


@mcp.command("resources")
@click.argument("name")
def mcp_resources(name: str) -> None:
    """List resources exposed by the MCP server named NAME."""
    import asyncio

    from coderAI.ui.display import display

    entry = _require_server(name, display)
    result = asyncio.run(_connect_and_list(name, entry, "resources"))
    if not result.get("success"):
        display.print_error(f"Could not list resources for '{name}': {result.get('error')}")
        sys.exit(1)

    resources = result.get("resources", [])
    if not resources:
        display.print_info(f"'{name}' exposes no resources.")
        return
    rows = [
        {
            "URI": r.get("uri", ""),
            "Name": r.get("name", ""),
            "Type": r.get("mimeType", ""),
            "Description": (r.get("description", "") or "")[:60],
        }
        for r in resources
    ]
    display.print_table(rows, f"Resources on '{name}'")


@mcp.command("prompts")
@click.argument("name")
def mcp_prompts(name: str) -> None:
    """List prompt templates exposed by the MCP server named NAME."""
    import asyncio

    from coderAI.ui.display import display

    entry = _require_server(name, display)
    result = asyncio.run(_connect_and_list(name, entry, "prompts"))
    if not result.get("success"):
        display.print_error(f"Could not list prompts for '{name}': {result.get('error')}")
        sys.exit(1)

    prompts = result.get("prompts", [])
    if not prompts:
        display.print_info(f"'{name}' exposes no prompts.")
        return
    rows = [
        {
            "Name": p.get("name", ""),
            "Arguments": ", ".join(
                a.get("name", "") for a in (p.get("arguments") or []) if isinstance(a, dict)
            ),
            "Description": (p.get("description", "") or "")[:60],
        }
        for p in prompts
    ]
    display.print_table(rows, f"Prompts on '{name}'")

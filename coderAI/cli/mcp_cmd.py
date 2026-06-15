"""CLI subcommands for managing MCP (Model Context Protocol) servers.

Reads and writes ``~/.coderAI/mcp_servers.json`` — the same file the ``coderAI
setup`` wizard writes and that ``coderAI chat`` auto-connects on startup
(``ExecutionLoop._autoconnect_mcp_servers``). Servers added here become
available the next time you start a chat.
"""

import sys

import click

from coderAI.tools.mcp import (
    ALLOWED_MCP_LAUNCHERS,
    load_mcp_servers,
    mcp_servers_path,
    save_mcp_servers,
)


def _launcher_allowed(command: str) -> bool:
    """Mirror ``MCPConnectTool``'s launcher check (bare name or ``/path/to/name``)."""
    cmd_lower = command.lower()
    return any(
        cmd_lower == launcher or cmd_lower.endswith("/" + launcher)
        for launcher in ALLOWED_MCP_LAUNCHERS
    )


@click.group(invoke_without_command=True)
@click.pass_context
def mcp(ctx):
    """Manage MCP (Model Context Protocol) servers."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@mcp.command("add")
@click.argument("name")
@click.option(
    "--command",
    "-c",
    help="Launcher command for stdio transport (e.g. npx, python3, uvx).",
)
@click.option(
    "--args",
    "args_str",
    default="",
    help="Comma-separated arguments for the command (stdio transport).",
)
@click.option(
    "--sse",
    "sse_url",
    help="SSE endpoint URL — selects SSE transport instead of stdio.",
)
def mcp_add(name, command, args_str, sse_url):
    """Add (or overwrite) an MCP server named NAME."""
    from coderAI.ui.display import display

    # ``__`` is reserved for the ``mcp__<server>__<tool>`` id encoding.
    if "__" in name:
        display.print_error(
            f"Server name must not contain '__' (reserved for MCP tool ids): {name!r}"
        )
        sys.exit(2)

    if sse_url and command:
        display.print_error("Pass either --sse (SSE) or --command (stdio), not both.")
        sys.exit(2)

    if sse_url:
        entry = {"transport": "sse", "url": sse_url}
    elif command:
        if not _launcher_allowed(command):
            display.print_error(
                f"Launcher '{command}' is not allowed. Use one of: "
                f"{', '.join(sorted(ALLOWED_MCP_LAUNCHERS))}"
            )
            sys.exit(2)
        args = [a.strip() for a in args_str.split(",") if a.strip()]
        entry = {"command": command, "args": args}
    else:
        display.print_error("Provide --command <launcher> (stdio) or --sse <url> (SSE).")
        sys.exit(2)

    data = load_mcp_servers()
    servers = data.setdefault("mcpServers", {})
    if name in servers:
        display.print_warning(f"Overwriting existing MCP server '{name}'")
    servers[name] = entry
    save_mcp_servers(data)

    display.print_success(f"Added MCP server '{name}' to {mcp_servers_path()}")
    display.print_info("It will connect on the next `coderAI chat`.")


@mcp.command("list")
def mcp_list():
    """List configured MCP servers."""
    from coderAI.ui.display import display

    servers = load_mcp_servers().get("mcpServers", {})
    if not servers:
        display.print_info(f"No MCP servers configured ({mcp_servers_path()}).")
        return

    rows = []
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        if transport == "sse":
            target = cfg.get("url", "")
            args = ""
        else:
            target = cfg.get("command", "")
            args = " ".join(cfg.get("args", []) or [])
        rows.append({"Name": name, "Transport": transport, "Command/URL": target, "Args": args})

    display.print_table(rows, "Configured MCP servers")
    display.print_info(
        "These are configured servers; live connection status is shown inside a chat session."
    )


@mcp.command("remove")
@click.argument("name")
def mcp_remove(name):
    """Remove the MCP server named NAME."""
    from coderAI.ui.display import display

    data = load_mcp_servers()
    servers = data.get("mcpServers", {})
    if name not in servers:
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    del servers[name]
    save_mcp_servers(data)
    display.print_success(f"Removed MCP server '{name}'.")

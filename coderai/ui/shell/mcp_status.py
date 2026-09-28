from __future__ import annotations

from rich.console import Group, RenderableType
from rich.spinner import Spinner
from rich.text import Text

from coderai.ui.shell.console import no_color_enabled
from coderai.utils.rich.columns import BulletColumns
from coderai.wire.types import MCPStatusSnapshot


def render_mcp_console(snapshot: MCPStatusSnapshot) -> RenderableType:
    plain = no_color_enabled()
    header_text = Text.assemble(
        ("MCP Servers: ", "bold" if not plain else ""),
        f"{snapshot.connected}/{snapshot.total} connected, {snapshot.tools} tools",
    )
    header: RenderableType = (
        header_text
        if (snapshot.loading and plain)
        else (Spinner("dots", header_text) if snapshot.loading else header_text)
    )

    renderables: list[RenderableType] = [BulletColumns(header)]
    for server in snapshot.servers:
        color = "" if plain else _status_color(server.status)
        dim = "" if plain else "grey50"
        if plain:
            server_text = server.name
        else:
            server_text = f"[{color}]{server.name}[/{color}]"
        if server.status == "unauthorized":
            server_text += (
                f" [{dim}](unauthorized - run: coderai mcp auth {server.name})[/{dim}]"
                if not plain
                else f" (unauthorized - run: coderai mcp auth {server.name})"
            )
        elif server.status != "connected":
            server_text += (
                f" [{dim}]({server.status})[/{dim}]" if not plain else f" ({server.status})"
            )

        lines: list[RenderableType] = [Text.from_markup(server_text)]
        for tool_name in server.tools:
            lines.append(
                BulletColumns(
                    Text.from_markup(f"[{dim}]{tool_name}[/{dim}]" if not plain else tool_name),
                    bullet_style=color if not plain else None,
                )
            )
        renderables.append(BulletColumns(Group(*lines), bullet_style=color or None))

    return Group(*renderables)


def _status_color(status: str) -> str:
    return {
        "connected": "green",
        "connecting": "cyan",
        "pending": "yellow",
        "failed": "red",
        "unauthorized": "red",
    }.get(status, "red")

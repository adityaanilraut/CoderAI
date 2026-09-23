from __future__ import annotations

from rich.console import Group, RenderableType
from rich.spinner import Spinner
from rich.text import Text

from coderai.utils.rich.columns import BulletColumns
from coderai.wire.types import MCPStatusSnapshot


def render_mcp_console(snapshot: MCPStatusSnapshot) -> RenderableType:
    header_text = Text.assemble(
        ("MCP Servers: ", "bold"),
        f"{snapshot.connected}/{snapshot.total} connected, {snapshot.tools} tools",
    )
    header: RenderableType = Spinner("dots", header_text) if snapshot.loading else header_text

    renderables: list[RenderableType] = [BulletColumns(header)]
    for server in snapshot.servers:
        color = _status_color(server.status)
        server_text = f"[{color}]{server.name}[/{color}]"
        if server.status == "unauthorized":
            server_text += f" [grey50](unauthorized - run: coderai mcp auth {server.name})[/grey50]"
        elif server.status != "connected":
            server_text += f" [grey50]({server.status})[/grey50]"

        lines: list[RenderableType] = [Text.from_markup(server_text)]
        for tool_name in server.tools:
            lines.append(
                BulletColumns(
                    Text.from_markup(f"[grey50]{tool_name}[/grey50]"),
                    bullet_style="grey50",
                )
            )
        renderables.append(BulletColumns(Group(*lines), bullet_style=color))

    return Group(*renderables)


def _status_color(status: str) -> str:
    return {
        "connected": "green",
        "connecting": "cyan",
        "pending": "yellow",
        "failed": "red",
        "unauthorized": "red",
    }.get(status, "red")

"""Neutral Rich display helpers (usable from CLI, tools, and system).

Kept out of ``cli/`` so tools (e.g. MCP OAuth) do not depend on the Click
presentation layer.
"""

from __future__ import annotations

from typing import Any, Dict, List

from rich.console import Console
from rich.table import Table
from rich.tree import Tree


class Display:
    """Display manager using Rich for terminal output."""

    def __init__(self) -> None:
        self.console = Console()

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.console.print(*args, **kwargs)

    def print_error(self, message: str) -> None:
        self.console.print(f"[bold red]Error:[/bold red] {message}")

    def print_success(self, message: str) -> None:
        self.console.print(f"[bold green]✓[/bold green] {message}")

    def print_warning(self, message: str) -> None:
        self.console.print(f"[bold yellow]⚠[/bold yellow] {message}")

    def print_info(self, message: str) -> None:
        self.console.print(f"[bold blue]ℹ[/bold blue] {message}")

    def print_table(self, data: List[Dict[str, Any]], title: str = "") -> None:
        if not data:
            return
        table = Table(title=title, show_header=True, header_style="bold magenta")
        keys = list(dict.fromkeys(k for d in data for k in d))
        for key in keys:
            table.add_column(key.replace("_", " ").title())
        for row in data:
            table.add_row(*[str(row.get(k, "")) for k in keys])
        self.console.print(table)

    def print_tree(self, data: Dict[str, Any], title: str = "Tree") -> None:
        tree = Tree(f"[bold]{title}[/bold]")
        self._add_tree_items(tree, data)
        self.console.print(tree)

    def _add_tree_items(self, tree: Any, data: Any) -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    branch = tree.add(f"[cyan]{key}[/cyan]")
                    self._add_tree_items(branch, value)
                else:
                    tree.add(f"[cyan]{key}[/cyan]: {value}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    branch = tree.add(f"[yellow]Item {i}[/yellow]")
                    self._add_tree_items(branch, item)
                else:
                    tree.add(f"[yellow]{item}[/yellow]")

    def print_header(self, text: str) -> None:
        self.console.rule(f"[bold blue]{text}[/bold blue]")


display = Display()

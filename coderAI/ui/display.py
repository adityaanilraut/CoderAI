"""Rich display utilities for beautiful terminal output."""

from typing import Any, Dict, List

from rich.console import Console
from rich.table import Table
from rich.tree import Tree


class Display:
    """Display manager using Rich for beautiful terminal output."""

    def __init__(self):
        """Initialize the display manager."""
        self.console = Console()

    def print(self, *args, **kwargs):
        """Print to console."""
        self.console.print(*args, **kwargs)

    def print_error(self, message: str):
        """Print an error message."""
        self.console.print(f"[bold red]Error:[/bold red] {message}")

    def print_success(self, message: str):
        """Print a success message."""
        self.console.print(f"[bold green]✓[/bold green] {message}")

    def print_warning(self, message: str):
        """Print a warning message."""
        self.console.print(f"[bold yellow]⚠[/bold yellow] {message}")

    def print_info(self, message: str):
        """Print an info message."""
        self.console.print(f"[bold blue]ℹ[/bold blue] {message}")

    def print_table(self, data: List[Dict[str, Any]], title: str = ""):
        """Print data as a table."""
        if not data:
            return

        table = Table(title=title, show_header=True, header_style="bold magenta")

        # Add columns
        if data:
            for key in data[0].keys():
                table.add_column(key.replace("_", " ").title())

            # Add rows
            for row in data:
                table.add_row(*[str(v) for v in row.values()])

        self.console.print(table)

    def print_tree(self, data: Dict[str, Any], title: str = "Tree"):
        """Print hierarchical data as a tree."""
        tree = Tree(f"[bold]{title}[/bold]")
        self._add_tree_items(tree, data)
        self.console.print(tree)

    def _add_tree_items(self, tree, data):
        """Recursively add items to tree."""
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

    def print_header(self, text: str):
        """Print a header."""
        self.console.rule(f"[bold blue]{text}[/bold blue]")

    def status(self, text: str):
        """Create a status context for showing progress."""
        return self.console.status(text)

    def clear(self):
        """Clear the console."""
        self.console.clear()


# Global display instance
display = Display()

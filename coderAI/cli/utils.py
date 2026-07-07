"""Shared CLI utilities."""

from typing import Any, Dict, List, Optional, Set

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

_NO_API_KEY_MESSAGE = (
    "No API key configured. Run `coderAI setup` to add one, or set a "
    "provider env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, "
    "DEEPSEEK_API_KEY, GEMINI_API_KEY). For local models, run `coderAI config set "
    "default_model lmstudio` (or ollama)."
)


class Display:
    """Display manager using Rich for beautiful terminal output."""

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


def missing_api_key_message() -> Optional[str]:
    """Return an error string if no usable provider is configured, else ``None``.

    A provider is usable when any cloud API key is set, or the default model is
    a local provider (lmstudio/ollama). Shared by ``chat`` and ``run`` so both
    entry points apply the same precheck (with their own output formatting).
    """
    from coderAI.system.config import config_manager

    cfg = config_manager.load()
    has_cloud_key = any(
        [
            getattr(cfg, "openai_api_key", None),
            getattr(cfg, "anthropic_api_key", None),
            getattr(cfg, "groq_api_key", None),
            getattr(cfg, "deepseek_api_key", None),
            getattr(cfg, "gemini_api_key", None),
        ]
    )
    local_default = (cfg.default_model or "").lower() in ("lmstudio", "ollama")
    if has_cloud_key or local_default:
        return None
    return _NO_API_KEY_MESSAGE


def valid_models() -> Set[str]:
    """Return the set of valid default-model values accepted by setup()."""
    from coderAI.llm.factory import get_all_model_ids

    return get_all_model_ids()


def valid_endpoint(url: str) -> bool:
    """Loose URL check: must start with http:// or https:// and have a host."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)

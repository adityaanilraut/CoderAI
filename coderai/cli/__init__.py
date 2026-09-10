"""coderai.cli — thin UI layer over coderai.core."""

from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    # Lazy so importing any coderai.cli.* submodule (e.g. coderai.cli.elapsed
    # via coderai.ui.shell.visualize._blocks) doesn't eagerly import the
    # interactive app monolith and create import cycles through moved shims.
    if name == "main":
        from coderai.cli.app import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

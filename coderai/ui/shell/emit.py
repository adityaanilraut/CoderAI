"""Plain-text terminal emission for untrusted content (UI-A2/A3/A4/A13/A14).

Rich markup is opt-in styling for our own chrome. Anything the model, a
tool, a web page, an MCP server, or a saved session wrote must go through
here so ``[/x]``, ``[link=y]`` and friends render literally instead of
crashing the display or hiding text.
"""

from __future__ import annotations

from typing import Any


def _emit_plain(console: Any | None, text: Any) -> None:
    """Print ``text`` with Rich markup disabled (plain ``print`` without console)."""
    rendered = str(text) if not isinstance(text, str) else text
    if console is not None:
        try:
            from rich.text import Text

            console.print(Text(rendered), markup=False)
            return
        except Exception:
            pass
    print(rendered)

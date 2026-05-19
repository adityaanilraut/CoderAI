"""Rich-based display helpers for one-shot CLI subcommands.

Used by ``coderAI status``, ``coderAI setup``, ``coderAI models``, etc.
The interactive chat UI lives in :mod:`coderAI.tui` (Textual).
"""

from coderAI.ui.display import Display, display

__all__ = ["Display", "display"]

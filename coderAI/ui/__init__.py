"""Rich-based display helpers for one-shot CLI commands.

The interactive chat UI now lives in the TypeScript Ink frontend (``ui/``)
driven by ``coderAI.ipc``. Only the ``Display`` helpers are kept here for
utility commands like ``coderAI status`` and ``coderAI setup``.
"""

from .display import Display, display

__all__ = ["Display", "display"]

# Ported from kimi_cli/acp/tools.py - kimi structure (acp/tools.py).
"""ACP-side tool adapters (Kimi ``acp/tools.py`` parity).

Phase 1: :class:`HideOutputDisplayBlock` only (consumed by
:mod:`coderai.acp.convert`). The ``Terminal`` tool + ``replace_tools()``
need the ``KimiToolset``/``Runtime``/kosong-``Shell`` engine pieces that
land with Phase 2 (soul/toolset) and Phase 3 (ACP server) — they are
ported then, not here.
"""

from __future__ import annotations

from kosong.tooling import DisplayBlock


class HideOutputDisplayBlock(DisplayBlock):
    """A special DisplayBlock that indicates output should be hidden in ACP clients."""

    type: str = "acp/hide_output"


__all__ = ["HideOutputDisplayBlock"]

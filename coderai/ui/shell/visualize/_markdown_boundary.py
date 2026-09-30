"""Markdown commitment boundaries shared by live and fallback renderers.

The parser contract returns None until two top-level blocks exist. The
streaming contract maps that to zero, leaving the final block in the live tail.
Markdown-it is required by Rich; parsing failures keep the text uncommitted.
"""

from __future__ import annotations

from functools import lru_cache

from markdown_it import MarkdownIt

_SELF_CLOSING_BLOCKS = frozenset(("fence", "code_block", "hr", "html_block"))


@lru_cache(maxsize=1)
def _get_md_parser() -> MarkdownIt:
    return MarkdownIt().enable("strikethrough").enable("table")


def find_parser_boundary(text: str) -> int | None:
    """Parser-aware boundary. None if <2 blocks."""
    md = _get_md_parser()
    try:
        tokens = md.parse(text)
    except Exception:
        return None
    block_maps: list[list[int]] = []
    depth = 0
    for t in tokens:
        if t.nesting == 1:
            if depth == 0 and t.map is not None:
                block_maps.append(t.map)
            depth += 1
        elif t.nesting == -1:
            depth -= 1
        elif depth == 0 and t.type in _SELF_CLOSING_BLOCKS and t.map is not None:
            block_maps.append(t.map)
    if len(block_maps) < 2:
        return None
    target_line = block_maps[-2][1]
    offset = 0
    try:
        for _ in range(target_line):
            offset = text.index("\n", offset) + 1
    except ValueError:
        return None
    return offset


def find_committed_boundary(text: str) -> int:
    """Return the safe prefix length, or zero when no block can be committed."""
    return find_parser_boundary(text) or 0

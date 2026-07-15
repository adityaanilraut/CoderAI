"""Selectable RichLog that supports mouse drag text selection."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from textual.geometry import Size
from textual.strip import Strip
from textual.widgets._rich_log import RichLog

if TYPE_CHECKING:
    from textual.selection import Selection


@lru_cache(maxsize=10000)
def _get_offset_style(style: Style, offset_x: int, offset_y: int) -> Style:
    """Memoize style creation to avoid massive GC pressure on every render tick."""
    return style + Style(meta={"offset": (offset_x, offset_y)})


class SelectableRichLog(RichLog):
    """RichLog subclass that injects offset metadata so mouse text selection works.

    Textual's Screen mouse-selection mechanism requires the rendered segments
    to carry ``{"offset": (x, y)}`` metadata. The stock RichLog does not set
    this, so selection silently fails. This widget adds it.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract selected text from stored log lines.

        Stock ``RichLog.get_selection`` delegates to ``Widget.get_selection``,
        which only works for widgets whose ``_render()`` returns ``Text`` /
        ``Content``. RichLog stores pre-rendered ``Strip`` lines instead, so
        we rebuild plain text from ``self.lines`` and let ``Selection.extract``
        slice the range.
        """
        if not self.lines:
            return None
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y

        line = self._render_line(content_y, scroll_x, self.scrollable_content_region.width)

        new_segments: list = []
        offset_x = scroll_x

        for segment in line:
            text = segment.text
            seg_len = len(text)

            style = segment.style
            if style is None:
                style = Style()

            new_style = _get_offset_style(style, offset_x, content_y)

            new_segments.append(Segment(text, new_style))
            offset_x += seg_len

        strip = Strip(new_segments)
        return strip

    # ── Strip-blit fast path (relies on RichLog private internals) ──────
    # The methods below read and mutate RichLog private state
    # (``_size_known``, ``lines``, ``_widest_line_width``, ``virtual_size``)
    # so the app can blit pre-rendered Strips straight into the log without
    # re-rendering through Rich/Markdown — the cost the strip cache avoids.
    # Verified against Textual 8.2.4 / 8.2.8; re-verify these internals on
    # any Textual upgrade (see the pinned `textual>=8.0,<9` in pyproject).

    def sized_for_blit(self) -> bool:
        """True once the widget knows its width, so blitting strips is valid.

        Before the size is known, RichLog *defers* every write and replays
        them once sized, so ``lines`` is empty — blitting cached strips then
        would capture nothing.
        """
        return bool(self._size_known)

    def line_count(self) -> int:
        """Number of rendered strips currently in the log (blit start index)."""
        return len(self.lines)

    def blit_strips(self, strips: list[Strip]) -> None:
        """Append pre-rendered ``strips`` and refresh the scroll extent.

        Mirrors the tail of ``RichLog.write`` (widest-width bump +
        ``virtual_size``) without going through Rich, since the strips are
        already rendered.
        """
        self.lines.extend(strips)
        if strips:
            self._widest_line_width = max(
                self._widest_line_width, max(s.cell_length for s in strips)
            )
        self.virtual_size = Size(self._widest_line_width, len(self.lines))

    def strips_since(self, start: int) -> list[Strip]:
        """Strips appended since index ``start`` (for render-cache capture)."""
        return self.lines[start:]

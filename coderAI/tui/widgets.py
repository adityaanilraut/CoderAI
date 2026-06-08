"""Selectable RichLog that supports mouse drag text selection."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widgets._rich_log import RichLog


class SelectableRichLog(RichLog):
    """RichLog subclass that injects offset metadata so mouse text selection works.

    Textual's Screen mouse-selection mechanism requires the rendered segments
    to carry ``{"offset": (x, y)}`` metadata. The stock RichLog does not set
    this, so selection silently fails. This widget adds it.
    """

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y

        line = self._render_line(
            content_y, scroll_x, self.scrollable_content_region.width
        )

        new_segments: list = []
        offset_x = scroll_x

        for segment in line:
            text = segment.text
            seg_len = len(text)

            style = segment.style
            if style is None:
                style = Style()

            new_style = style + Style(meta={"offset": (offset_x, content_y)})

            new_segments.append(Segment(text, new_style))
            offset_x += seg_len

        strip = Strip(new_segments)
        return strip

"""Selectable RichLog that supports mouse drag text selection."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from textual.geometry import Size
from textual.strip import Strip
from textual.widgets import RichLog

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

        When ``_markdown_sources`` is populated (set by CoderAIApp from the
        raw timeline items), selection is extracted from the raw markdown so
        fences and original formatting are preserved rather than the
        Rich-rendered plain text.
        """
        # Prefer raw markdown sources for faithful copy (preserves fences)
        raw_sources: list[str] | None = getattr(self, "_markdown_sources", None)
        if raw_sources:
            raw_text = "\n".join(raw_sources)
            if raw_text:
                try:
                    return selection.extract(raw_text), "\n"
                except Exception:
                    pass
        if not self.lines:
            return None
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"

    def set_markdown_sources(self, sources: list[str]) -> None:
        """Store raw markdown sources for selection copy that preserves fences."""
        self._markdown_sources = sources  # type: ignore[attr-defined]

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y

        line = self._render_line(content_y, scroll_x, self.scrollable_content_region.width)
        # Match stock RichLog: apply the widget style so crop_extend padding and
        # content share one background. Skipping this left short lines with a
        # tinted rich_style pad → full-width "horizontal bar" behind every message.
        line = line.apply_style(self.rich_style)

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

    # ── Strip-blit fast path (Textual-version resilient) ──────────────
    # RichLog's fast path historically poked private state (``_size_known``,
    # ``lines``, ``_widest_line_width``, ``virtual_size``).  Textual 9 may
    # rename those.  Every method here probes multiple attribute names and
    # degrades to a style-preserving public ``write`` fallback that keeps
    # colors (unlike the old ``strip.text`` fallback which stripped styles).

    def sized_for_blit(self) -> bool:
        """True once the widget knows its width, so blitting strips is valid.

        Before the size is known, RichLog *defers* every write and replays
        them once sized, so ``lines`` is empty — blitting cached strips then
        would capture nothing.
        """
        # Probe known private flags then fall back to public mounted/size.
        for attr in ("_size_known", "_is_size_known", "_size_known_flag"):
            val = getattr(self, attr, None)
            if val is not None:
                return bool(val)
        try:
            if getattr(self, "is_mounted", False):
                w = self.scrollable_content_region.width  # type: ignore[attr-defined]
                if w and w > 0:
                    return True
                return self.size.width > 0  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            return self.size.width > 0  # type: ignore[attr-defined]
        except Exception:
            return False

    def line_count(self) -> int:
        """Number of rendered strips currently in the log (blit start index)."""
        lines = getattr(self, "lines", None)
        if lines is not None:
            try:
                return len(lines)  # type: ignore[arg-type]
            except Exception:
                return 0
        try:
            return int(getattr(self, "virtual_size").height)  # type: ignore[attr-defined]
        except Exception:
            return 0

    def blit_strips(self, strips: list[Strip]) -> None:
        """Append pre-rendered ``strips`` and refresh the scroll extent."""
        if not strips:
            return
        has_private = hasattr(self, "lines") and hasattr(self, "_widest_line_width")
        if has_private:
            try:
                self.lines.extend(strips)  # type: ignore[attr-defined]
                current_width = getattr(self, "_widest_line_width", 0)
                widest = current_width
                for s in strips:
                    try:
                        widest = max(widest, s.cell_length)
                    except Exception:
                        widest = max(widest, len(s.text))
                self._widest_line_width = widest  # type: ignore[attr-defined]
                try:
                    self.virtual_size = Size(widest, len(self.lines))  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    cache = getattr(self, "_line_cache", None)
                    if cache is not None:
                        cache.clear()
                except Exception:
                    pass
                self.refresh()
                return
            except Exception:
                pass
        from rich.text import Text as _RichText

        for strip in strips:
            try:
                txt = _RichText()
                has_segments = False
                for seg in strip:
                    has_segments = True
                    txt.append(seg.text, style=seg.style or None)
                if not has_segments:
                    txt.append(strip.text)
                self.write(txt)  # type: ignore[attr-defined]
            except Exception:
                try:
                    self.write(strip.text)  # type: ignore[attr-defined]
                except Exception:
                    pass

    def strips_since(self, start: int) -> list[Strip]:
        """Strips appended since index ``start`` (for render-cache capture)."""
        lines = getattr(self, "lines", None)
        if lines is not None:
            try:
                return list(lines[start:])  # type: ignore[return-value, no-any-return]
            except Exception:
                return []
        return []

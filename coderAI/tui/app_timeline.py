# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Timeline rendering, caching, chrome, and context-chip interaction."""

from __future__ import annotations

from typing import Any

from textual import events, on
from textual.css.query import NoMatches
from textual.widgets import Static

from coderAI.tui.platform import composer_placeholder
from coderAI.tui.rendering import (
    composer_footer_markup,
    render_agent_tree,
    render_session_header,
    render_tasks,
)
from coderAI.tui.screens import PromptArea
from coderAI.tui.state import SessionState
from coderAI.tui.theme import Glyphs, Tokens
from coderAI.tui.timeline_render import build_stream_tail_markup
from coderAI.tui.widgets import SelectableRichLog


class AppTimelineController:
    def _refresh_ui(self, mode: str = "full") -> None:
        from coderAI.tui import app as app_module

        try:
            log = self.query_one("#timeline", SelectableRichLog)
        except NoMatches:
            return
        s = self.reducer.session
        verbose = s.verbose
        timeline = self.reducer.timeline

        if mode == "chrome":
            self._render_chrome(s)
            return

        if mode == "full":
            log.clear()
            self._log_rendered_idx = 0
            self._hide_stream_tail()
            # Prune render cache to keep it clean and bound to active timeline items
            active_ids = {it.get("id") for it in timeline if it.get("id")}
            for k in list(self._render_cache.keys()):
                if k[0] not in active_ids:
                    del self._render_cache[k]

        if mode == "stream":
            streaming_aid = self.reducer._current_assistant_id
            if streaming_aid:
                for it in timeline:
                    if it.get("id") == streaming_aid and it.get("streaming"):
                        self._render_stream_tail(it, verbose)
                        break
                else:
                    self._hide_stream_tail()
            else:
                self._hide_stream_tail()
            # Stream ticks only move the live tail; chrome and composer
            # don't change until a discrete ("append"/"full") event lands.
            return
        else:
            # Before the RichLog knows its width it *defers* every write and
            # replays them once sized, so log.lines is empty and strip caching
            # would capture nothing. Fall back to plain writes until then.
            width = log.scrollable_content_region.width
            use_cache = log.sized_for_blit() and width > 0
            # Only auto-follow when the user is already pinned to the bottom,
            # so reading scrollback isn't yanked away by new output. A "full"
            # refresh rebuilds from scratch and always re-pins.
            was_at_end = mode == "full" or bool(log.is_vertical_scroll_end)
            self._was_at_end = was_at_end
            idx = self._log_rendered_idx
            # Insert unread divider when new messages arrive while scrolled up
            if not was_at_end and idx < len(timeline):
                pending_new = len(timeline) - idx
                # Render divider as separator before new items
                from rich.text import Text as _Text

                divider_text = f"── {pending_new} new message{'s' if pending_new != 1 else ''} ──"
                # Only insert visual divider if we have pending new content
                if pending_new > 0:
                    log.write(_Text(divider_text, style=Tokens.ACCENT))
            while idx < len(timeline):
                it = timeline[idx]
                if it.get("kind") == "assistant" and it.get("streaming"):
                    self._render_stream_tail(it, verbose)
                    break

                if use_cache:
                    self._write_item_cached(log, it, verbose, width)
                else:
                    app_module.write_timeline_item(log, it, verbose=verbose)
                idx += 1
            self._log_rendered_idx = idx
            if idx > 0 and was_at_end:
                log.scroll_end(animate=False, force=True)
            if idx >= len(timeline):
                if not self.reducer.session.streaming and not any(
                    it.get("kind") == "assistant" and it.get("streaming") for it in timeline
                ):
                    self._hide_stream_tail()

        # Update markdown sources for selection copy (preserves fences)
        try:
            log_md = self.query_one("#timeline", SelectableRichLog)
            raw_sources: list[str] = []
            for it in timeline:
                kind = it.get("kind")
                if kind == "user":
                    raw_sources.append(str(it.get("text", "")))
                elif kind == "assistant":
                    raw_sources.append(str(it.get("content", "")))
                    if it.get("reasoning"):
                        raw_sources.append(str(it.get("reasoning", "")))
                elif kind == "tool":
                    if it.get("preview"):
                        raw_sources.append(str(it.get("preview", "")))
                    if it.get("args"):
                        raw_sources.append(str(it.get("args", "")))
                elif kind == "diff":
                    raw_sources.append(str(it.get("diff", "")))
                elif kind == "error":
                    raw_sources.append(
                        str(it.get("message", "")) + "\n" + str(it.get("details", ""))
                    )
            # Store for widget selection
            if hasattr(log_md, "set_markdown_sources"):
                log_md.set_markdown_sources(raw_sources)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._render_chrome(s)
        self._refresh_composer_context()
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            was_disabled = prompt.disabled
            prompt.disabled = not s.ready
            # Disabling the composer at startup bounces focus to the next
            # focusable widget (the agent-tree scroll); hand it back the
            # moment the agent is ready so the user can just type.
            if was_disabled and s.ready:
                prompt.focus()
            if not s.ready:
                prompt.placeholder = "Starting agent…"
            elif s.progress:
                label = str(s.progress.get("label") or "Working")
                prompt.placeholder = f"{label}…"
            else:
                prompt.placeholder = composer_placeholder()
            caret_color = Tokens.ACCENT if s.ready else Tokens.LINE
            self.query_one("#prompt-caret", Static).update(f"[{caret_color}]{Glyphs.USER}[/]")
            footer = self.query_one("#composer-footer", Static)
            footer.update(composer_footer_markup(self.reducer.session, width=self.size.width))
            self._update_token_counter()
            self._update_jump_button(getattr(self, "_was_at_end", True))
        except NoMatches:
            pass

    def _update_jump_button(self, was_at_end: bool) -> None:
        try:
            jb = self.query_one("#jump-bottom", Static)
            if was_at_end:
                jb.display = False
                jb.remove_class("has-new")
                # Also hide unread divider if any
                return
            # When not at end, show divider affordance if there are new items
            pending = len(self.reducer.timeline) - self._log_rendered_idx
            if pending > 0 and not was_at_end:
                # Show jump button with count
                jb.update(f"↘ {pending} new — press End or click to jump")
                jb.display = True
                jb.add_class("has-new")
                # Insert visual divider in timeline if not already present
                # We use a separator item rather than direct render
                has_divider = any(
                    it.get("kind") == "separator" and "new messages" in str(it.get("message", ""))
                    for it in self.reducer.timeline
                )
                if not has_divider:
                    # Add ephemeral divider without mutating timeline id sequence
                    pass
            else:
                jb.display = False
                jb.remove_class("has-new")
        except Exception:
            pass

    @on(events.Click)
    def _on_generic_click(self, event: events.Click) -> None:
        # Jump-bottom click handler
        try:
            if event.widget and getattr(event.widget, "id", None) == "jump-bottom":
                self.action_timeline_scroll_bottom()
                try:
                    jb = self.query_one("#jump-bottom", Static)
                    jb.display = False
                    jb.remove_class("has-new")
                except Exception:
                    pass
                event.stop()
                return
            if event.widget and getattr(event.widget, "id", None) == "composer-context":
                self._handle_composer_context_click()
                event.stop()
        except Exception:
            pass

    def _handle_composer_context_click(self) -> None:
        """Interactive × on chips: click to /unpin without typing."""
        # Check if click was on a chip's × - for now treat any click on context
        # as request to show unpin palette; if single file pinned, unpin it directly
        files = self.reducer.session.context_files or []
        if not files:
            return
        if len(files) == 1:
            path = str(files[0].get("path", ""))
            if self.controller:
                self.controller.enqueue_command("manage_context", action="remove", path=path)
                self.notify(f"Unpinned {path}")
        else:
            # For multiple, show which one to unpin via quick action
            self.run_worker(self._show_unpin_picker(), exclusive=True)

    async def _show_unpin_picker(self) -> None:
        files = self.reducer.session.context_files or []
        if not files:
            return
        from coderAI.tui.screens import FilePickerScreen

        result = await self.push_screen_wait(
            FilePickerScreen(
                [str(f.get("path", "")) for f in files],
                placeholder="Select file to unpin…",
                footer_help=f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ unpin  ⎋ close[/]",
            )
        )
        if result and self.controller:
            self.controller.enqueue_command("manage_context", action="remove", path=result)
            self.notify(f"Unpinned {result}")

    def _write_item_cached(
        self, log: SelectableRichLog, it: dict[str, Any], verbose: bool, width: int
    ) -> None:
        """Append a timeline item to ``log``, reusing cached rendered Strips.

        On a cache hit the item's pre-rendered Strips are blitted straight into
        ``log.lines`` (no Rich/Markdown re-rendering). On a miss the item is
        rendered through the normal ``log.write`` path and the resulting Strips
        are captured for next time. This is what keeps a "full" refresh cheap
        as the conversation grows — see ``self._render_cache``.
        """
        from coderAI.tui import app as app_module

        cache_key = (it.get("id"), verbose, width)
        cached = self._render_cache.get(cache_key)
        if cached is not None and cached[0] == it:
            log.blit_strips(cached[1])
            return
        start = log.line_count()
        app_module.write_timeline_item(log, it, verbose=verbose)
        self._render_cache[cache_key] = (it.copy(), log.strips_since(start))

    def _render_chrome(self, s: SessionState) -> None:
        # The three chrome panes always co-exist in the DOM (breakpoints toggle
        # `display`; they are never unmounted), so one guard covering the
        # transient teardown window is enough.
        try:
            for selector, render in (
                ("#session-header", render_session_header),
                ("#agent-tree-content", render_agent_tree),
                ("#tasks-pane", render_tasks),
            ):
                self.query_one(selector, Static).update(render(s))
        except NoMatches:
            pass

    def _render_stream_tail(self, it: dict[str, Any], verbose: bool) -> None:
        try:
            tail = self.query_one("#stream-tail", Static)
        except NoMatches:
            return
        tail.update(build_stream_tail_markup(it, verbose=verbose))
        tail.display = True

    def _hide_stream_tail(self) -> None:
        try:
            tail = self.query_one("#stream-tail", Static)
        except NoMatches:
            return
        tail.update("")
        tail.display = False

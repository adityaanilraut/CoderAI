"""Textual chat application."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Dict, List, Optional

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.strip import Strip
from textual.widgets import Footer, Static

from coderAI.tui.widgets import SelectableRichLog

from coderAI.tui.diff_render import format_diff_gutter
from coderAI.tui.listeners import EventReducer, RefreshMode
from coderAI.tui.project import async_scan_project_files
from coderAI.tui.rendering import (
    composer_footer_markup,
    render_agent_tree,
    render_session_header,
    render_tasks,
)
from coderAI.tui.screens import (
    AgentEventMsg,
    ApprovalScreen,
    CommandPaletteScreen,
    FilePickerScreen,
    FullContentScreen,
    PromptArea,
    SearchScreen,
    SessionPickerScreen,
)
from coderAI.core.agent_tracker import agent_tracker
from coderAI.system.history import history_manager
from coderAI.tui.session_setup import create_agent_session
from coderAI.tui.slash import handle_slash_command
from coderAI.tui.state import SessionState
from coderAI.tui.platform import composer_placeholder
from coderAI.tui.theme import Glyphs, Tokens
from coderAI.tui.timeline_render import (
    build_stream_tail_markup,
    write_timeline_item,
    calculate_item_lines,
)

STREAM_TICK_S = 0.12

# Responsive breakpoints (terminal columns). Below PANE_RIGHT_MIN_COLS the
# right (tasks) pane auto-hides; below PANE_LEFT_MIN_COLS the left
# (agents) pane hides too, leaving the full width to the conversation.
# Ctrl+B / Ctrl+G override a pane until toggled back to its auto state.
PANE_LEFT_MIN_COLS = 100
PANE_RIGHT_MIN_COLS = 130


def _build_coderai_css() -> str:
    return f"""
Screen {{
    layout: vertical;
    background: {Tokens.BG};
    color: {Tokens.TEXT};
}}
#main {{
    height: 1fr;
    layout: horizontal;
}}
#center {{
    width: 1fr;
    height: 1fr;
    layout: vertical;
    layers: base tail;
    background: {Tokens.BG};
}}
#session-header {{
    height: auto;
    min-height: 1;
    padding: 0 2;
    background: {Tokens.BG};
    border-bottom: solid {Tokens.LINE};
    color: {Tokens.TEXT_DIM};
}}
#timeline {{
    height: 1fr;
    padding: 1 2;
    background: {Tokens.BG};
    scrollbar-background: {Tokens.BG};
    scrollbar-color: {Tokens.LINE};
}}
#stream-tail {{
    layer: tail;
    dock: bottom;
    height: auto;
    max-height: 40%;
    padding: 0 2 1 2;
    background: {Tokens.BG};
    display: none;
}}
#composer-box {{
    height: auto;
    margin: 1 2;
    background: {Tokens.COMPOSER_BG};
    border: round {Tokens.COMPOSER_LINE};
    padding: 1 2;
}}
#prompt-row {{
    height: auto;
    background: {Tokens.COMPOSER_BG};
}}
#prompt-caret {{
    width: 2;
    height: auto;
    color: {Tokens.ACCENT};
    background: {Tokens.COMPOSER_BG};
}}
#prompt-area {{
    width: 1fr;
    height: auto;
    min-height: 2;
    max-height: 8;
    background: {Tokens.COMPOSER_BG};
    color: {Tokens.TEXT};
    border: none;
}}
#composer-footer {{
    height: 1;
    padding: 1 0 0 0;
    color: {Tokens.TEXT_MUTED};
    background: {Tokens.COMPOSER_BG};
    border-top: none;
}}
#left-pane {{
    width: 32;
    height: 1fr;
    background: {Tokens.BG_SUNK};
    border-right: solid {Tokens.LINE_SOFT};
    layout: vertical;
}}
#agent-tree-scroll {{
    height: 1fr;
    padding: 1 1;
    background: {Tokens.BG_SUNK};
    scrollbar-background: {Tokens.BG_SUNK};
    scrollbar-color: {Tokens.LINE};
}}
#right-pane {{
    width: 35;
    height: 1fr;
    background: {Tokens.BG_SUNK};
    border-left: solid {Tokens.LINE_SOFT};
    layout: vertical;
}}
#tasks-scroll {{
    height: 1fr;
    padding: 1 2;
    background: {Tokens.BG_SUNK};
    scrollbar-background: {Tokens.BG_SUNK};
    scrollbar-color: {Tokens.LINE};
}}
#tasks-pane {{
    height: auto;
    color: {Tokens.TEXT_DIM};
}}
Footer {{
    background: {Tokens.COMPOSER_BG};
    color: {Tokens.TEXT_DIM};
}}
"""


class CoderAIApp(App[None]):
    """CoderAI Textual chat — three-column IDE layout."""

    TITLE = "CoderAI"
    CSS = _build_coderai_css()

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True, priority=False),
        Binding("ctrl+c", "ctrl_c", "Exit", show=False),
        Binding("ctrl+shift+c, super+c", "copy_selection", "Copy", show=False),
        Binding("ctrl+k,super+k", "command_palette", "Commands", show=True),
        Binding("ctrl+t", "toggle_collapse", "Collapse", show=True),
        Binding("ctrl+o", "expand_full", "Expand", show=True),
        Binding("ctrl+b", "toggle_left_pane", "Agents pane", show=False),
        Binding("ctrl+g", "toggle_right_pane", "Tasks pane", show=False),
        Binding("pageup", "timeline_page_up", "Scroll up", show=False),
        Binding("pagedown", "timeline_page_down", "Scroll down", show=False),
        Binding("ctrl+home", "timeline_scroll_top", "Top", show=False),
        Binding("ctrl+end", "timeline_scroll_bottom", "Bottom", show=False),
    ]

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        resume: Optional[str] = None,
        continue_: bool = False,
        auto_approve: bool = False,
        persona: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._resume = resume
        self._continue = continue_
        self._auto_approve = auto_approve
        self._persona = persona
        self.reducer = EventReducer()
        self.agent: Optional[Any] = None
        self.controller: Optional[Any] = None
        self._exit_armed_at: Optional[float] = None
        self._search_filter = ""
        self._log_rendered_idx = 0
        self.project_files: List[str] = []
        self._scan_in_progress = False
        self._agent_retry_count = 0
        # None = follow the responsive auto-hide; True/False = user override
        # via Ctrl+B (left) / Ctrl+G (right).
        self._left_pane_pref: Optional[bool] = None
        self._right_pane_pref: Optional[bool] = None
        # True while a send_message turn is in flight; drives the unfocused
        # "finished" notification on the next ready event.
        self._awaiting_response = False
        # Set when /resume retires the current agent: swallows the old
        # controller's goodbye so it doesn't toast "session ended" over the
        # freshly resumed one.
        self._suppress_goodbye = False
        # Rendered-Strip cache, keyed by (item id, verbose, render width). The
        # expensive part of any refresh is Rich rendering each renderable
        # (Markdown bubbles especially) into terminal Strips; caching the Strips
        # means a "full" refresh only re-renders items whose content changed and
        # blits everything else, instead of re-parsing every message each time a
        # tool finishes. Width is in the key so a terminal resize self-heals.
        self._render_cache: Dict[
            tuple[str | None, bool, int], tuple[Dict[str, Any], list[Strip]]
        ] = {}
        # Height side cache for Ctrl-collapse targeting. Keyed on the fields that
        # change an item's rendered height so a stale height never lingers, and
        # kept OFF the timeline dicts themselves (mutating them would poison
        # ``_render_cache``'s ``cached[0] == it`` comparison).
        self._line_count_cache: Dict[tuple[str | None, bool, bool, int], int] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                with VerticalScroll(id="agent-tree-scroll"):
                    yield Static("", id="agent-tree-content", markup=True)
            with Vertical(id="center"):
                yield Static("", id="session-header", markup=True)
                # auto_scroll off: _refresh_ui pins to the bottom explicitly,
                # and only when the user was already there (sticky follow).
                yield SelectableRichLog(
                    id="timeline", highlight=True, markup=True, wrap=True, auto_scroll=False
                )
                yield Static("", id="stream-tail", markup=True)
            with Vertical(id="right-pane"):
                with VerticalScroll(id="tasks-scroll"):
                    yield Static("", id="tasks-pane", markup=True)
        with Vertical(id="composer-box"):
            with Horizontal(id="prompt-row"):
                yield Static(f"[{Tokens.ACCENT}]{Glyphs.USER}[/]", id="prompt-caret", markup=True)
                yield PromptArea(id="prompt-area")
            yield Static("", id="composer-footer", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.reducer.on_change = self._on_reducer_change
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.show_line_numbers = False
        prompt.placeholder = composer_placeholder()
        prompt.focus()
        # Pass the callable so a fast shutdown cannot strand an already-created coroutine.
        self.run_worker(self._scan_project_files)  # type: ignore[arg-type]
        footer = self.query_one("#composer-footer", Static)
        footer.update(composer_footer_markup(self.reducer.session))
        self._apply_responsive_layout()
        self._refresh_ui("full")
        self._start_agent_worker()
        self._stream_timer = self.set_interval(STREAM_TICK_S, self.reducer.tick)

    def on_resize(self, event: events.Resize) -> None:
        # self.size still reports the pre-resize value while this handler
        # runs; the event carries the new terminal size.
        self._apply_responsive_layout(event.size.width)

    def on_unmount(self) -> None:
        timer = getattr(self, "_stream_timer", None)
        if timer is not None:
            timer.stop()
        if self.controller:
            self.controller.enqueue_command("exit")
        self.reducer.on_change = None
        for worker in list(self._workers):
            worker.cancel()

    def _on_reducer_change(self, mode: RefreshMode) -> None:
        self.post_message(AgentEventMsg("__refresh__", {"mode": mode}))

    def _toast(self, level: str, message: str) -> None:
        """Push a toast notification to the timeline."""
        self.reducer.toast(level, message)

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        if msg.event == "__refresh__":
            self._refresh_ui(str(msg.data.get("mode", "full")))
            return
        if msg.event == "goodbye" and self._suppress_goodbye:
            self._suppress_goodbye = False
            return
        self.reducer.handle(msg.event, msg.data)
        if msg.event == "tool" and msg.data.get("phase") == "awaiting_approval":
            payload = msg.data.get("payload") or {}
            self._notify_attention(f"CoderAI: approval needed — {payload.get('name') or 'tool'}")
            self.run_worker(self._maybe_show_approval())
        elif msg.event == "tool" and msg.data.get("phase") == "cancelled":
            payload = msg.data.get("payload") or {}
            self._dismiss_cancelled_approval(
                str(msg.data.get("id") or ""), str(payload.get("reason") or "cancelled")
            )
        elif msg.event == "ready" and self._awaiting_response:
            self._awaiting_response = False
            self._notify_attention("CoderAI: finished — ready for your next message")

    def _emit_bridge(self, event: str, data: Dict[str, Any]) -> None:
        try:
            self.call_from_thread(self.post_message, AgentEventMsg(event, data))
        except RuntimeError:
            self.post_message(AgentEventMsg(event, data))

    async def _run_agent(self) -> None:
        try:
            self.agent, self.controller = create_agent_session(
                model=self._model,
                resume=self._resume,
                continue_=self._continue,
                auto_approve=self._auto_approve,
                persona=self._persona,
                on_event=self._emit_bridge,
            )
        except Exception as exc:
            self._emit_bridge(
                "error",
                {
                    "category": "internal",
                    "message": f"Failed to start agent: {exc}",
                    "hint": "Run `coderAI doctor` and `coderAI setup` to verify config.",
                },
            )
            self._emit_bridge("goodbye", {"reason": "startup_failed"})
            return
        try:
            await self.controller.start()
        except Exception as exc:
            self._emit_bridge(
                "error",
                {"category": "internal", "message": f"Agent loop crashed: {exc}"},
            )
            if self._agent_retry_count < 1:
                self._agent_retry_count += 1
                self._emit_bridge(
                    "info",
                    {"message": "Auto-restarting agent…"},
                )
                self._start_agent_worker()
            else:
                self._emit_bridge(
                    "info",
                    {"message": "Agent crashed. Type /retry to restart."},
                )
                self._emit_bridge("goodbye", {"reason": "loop_crashed"})

    # ── Responsive layout ────────────────────────────────────────────

    def _auto_pane_visibility(self, width: Optional[int] = None) -> tuple[bool, bool]:
        """(left, right) pane visibility earned by the terminal width."""
        w = self.size.width if width is None else width
        return w >= PANE_LEFT_MIN_COLS, w >= PANE_RIGHT_MIN_COLS

    def _apply_responsive_layout(self, width: Optional[int] = None) -> None:
        w = self.size.width if width is None else width
        if w <= 0:
            return
        auto_left, auto_right = self._auto_pane_visibility(w)
        for selector, pref, auto in (
            ("#left-pane", self._left_pane_pref, auto_left),
            ("#right-pane", self._right_pane_pref, auto_right),
        ):
            try:
                pane = self.query_one(selector)
            except NoMatches:
                continue
            pane.display = auto if pref is None else pref

    def _toggle_pane(self, selector: str, auto: bool, label: str) -> Optional[bool]:
        """Flip a pane and return the new user override (None = back to auto)."""
        try:
            pane = self.query_one(selector)
        except NoMatches:
            return None
        show = not pane.display
        pane.display = show
        self.notify(f"{label} pane {'shown' if show else 'hidden'}")
        # An override that matches the auto state is no override at all —
        # drop it so future resizes keep auto-hiding/showing as expected.
        return None if show == auto else show

    def action_toggle_left_pane(self) -> None:
        auto_left, _ = self._auto_pane_visibility()
        self._left_pane_pref = self._toggle_pane("#left-pane", auto_left, "Agents")

    def action_toggle_right_pane(self) -> None:
        _, auto_right = self._auto_pane_visibility()
        self._right_pane_pref = self._toggle_pane("#right-pane", auto_right, "Tasks")

    # ── Desktop notifications ────────────────────────────────────────

    def _notify_attention(self, message: str) -> None:
        """Bell + OSC 9 desktop notification when the terminal is unfocused."""
        if self.app_focus:
            return
        cfg = getattr(self.agent, "config", None)
        if not getattr(cfg, "tui_notifications", True):
            return
        self.bell()
        driver = self._driver
        if driver is None:
            return
        safe = "".join(ch for ch in message if ch.isprintable())[:120]
        try:
            # OSC 9 desktop notification (iTerm2/WezTerm/kitty/ghostty);
            # terminals without support ignore the sequence.
            driver.write(f"\x1b]9;{safe}\x07")
            driver.flush()
        except Exception:
            # Best-effort decoration; never break the UI loop over it.
            pass

    # ── UI refresh ───────────────────────────────────────────────────

    def _refresh_ui(self, mode: str = "full") -> None:
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
            idx = self._log_rendered_idx
            while idx < len(timeline):
                it = timeline[idx]
                if it.get("kind") == "assistant" and it.get("streaming"):
                    self._render_stream_tail(it, verbose)
                    break

                if use_cache:
                    self._write_item_cached(log, it, verbose, width)
                else:
                    write_timeline_item(log, it, verbose=verbose)
                idx += 1
            self._log_rendered_idx = idx
            if idx > 0 and was_at_end:
                log.scroll_end(animate=False, force=True)
            if idx >= len(timeline):
                if not self.reducer.session.streaming and not any(
                    it.get("kind") == "assistant" and it.get("streaming") for it in timeline
                ):
                    self._hide_stream_tail()

        self._render_chrome(s)
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
            footer.update(composer_footer_markup(self.reducer.session))
        except NoMatches:
            pass

    def _write_item_cached(
        self, log: SelectableRichLog, it: Dict[str, Any], verbose: bool, width: int
    ) -> None:
        """Append a timeline item to ``log``, reusing cached rendered Strips.

        On a cache hit the item's pre-rendered Strips are blitted straight into
        ``log.lines`` (no Rich/Markdown re-rendering). On a miss the item is
        rendered through the normal ``log.write`` path and the resulting Strips
        are captured for next time. This is what keeps a "full" refresh cheap
        as the conversation grows — see ``self._render_cache``.
        """
        cache_key = (it.get("id"), verbose, width)
        cached = self._render_cache.get(cache_key)
        if cached is not None and cached[0] == it:
            log.blit_strips(cached[1])
            return
        start = log.line_count()
        write_timeline_item(log, it, verbose=verbose)
        self._render_cache[cache_key] = (it.copy(), log.strips_since(start))

    # ── Chrome (delegates to rendering.py) ───────────────────────────

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

    def _render_stream_tail(self, it: Dict[str, Any], verbose: bool) -> None:
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

    # ── Approval flow ────────────────────────────────────────────────

    async def _maybe_show_approval(self) -> None:
        pending = self.reducer.pending_approval()
        if not pending:
            return
        result = await self.push_screen_wait(ApprovalScreen(pending))
        if result is None:
            return
        approve, remember = result
        if self.controller:
            # Remember only the reviewed tool/path/command scope advertised by
            # the backend. Session-wide unsafe auto-approve remains an explicit
            # /yolo action and is never enabled from a routine approval prompt.
            #
            # Prefer enqueue_command: the agent loop owns the approval Future on
            # a worker thread, and UI-thread submit_command historically stalled
            # the turn until the next user message woke the loop.
            if approve and remember and pending.get("rememberMode"):
                self.controller.enqueue_command(
                    "allow_tool",
                    tool=str(pending.get("tool") or ""),
                    scope=str(pending.get("rememberScope") or ""),
                )
            self.controller.enqueue_command(
                "tool_approval_resp",
                toolId=pending["id"],
                approve=approve,
            )
        pending["decided"] = "approved" if approve else "denied"
        self._refresh_ui("full")

    def _dismiss_cancelled_approval(self, tool_id: str, reason: str) -> None:
        """Close a modal whose backend waiter has timed out or been cancelled."""
        active = self.screen
        if not isinstance(active, ApprovalScreen) or active.approval_id != tool_id:
            return
        active.dismiss(None)
        if reason == "timeout":
            self.notify("Approval timed out and was denied", severity="warning")

    # ── Keybindings ──────────────────────────────────────────────────

    def action_cancel_turn(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if self.controller:
            self.controller.enqueue_command("cancel")

    def action_ctrl_c(self) -> None:
        # _confirm_exit arms on first press (returns False) and exits on a
        # second press within the window (returns True).
        if not self._confirm_exit():
            self.notify("Press Ctrl+C again within 5s to exit")

    def _timeline_log(self) -> Optional[SelectableRichLog]:
        try:
            return self.query_one("#timeline", SelectableRichLog)
        except NoMatches:
            return None

    def _scroll_timeline(self, scroll: Callable[[SelectableRichLog], None]) -> None:
        log = self._timeline_log()
        if log is not None:
            scroll(log)

    def action_timeline_page_up(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_page_up())

    def action_timeline_page_down(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_page_down())

    def action_timeline_scroll_top(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_home(animate=False))

    def action_timeline_scroll_bottom(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_end(animate=False))

    def action_copy_selection(self) -> None:
        from coderAI.tui.clipboard import copy_text

        text = self.screen.get_selected_text()
        if not text:
            self.notify("Nothing selected", severity="warning")
            return
        copy_text(
            text,
            write_osc52=self._osc52_writer(),
            notify_fn=self.notify,
        )

    def _osc52_writer(self):
        """Return a writer that sends OSC-52 through Textual's terminal driver."""
        driver = self._driver

        def write(sequence: str) -> None:
            if driver is not None:
                driver.write(sequence)
            else:
                import sys

                sys.stdout.write(sequence)
                sys.stdout.flush()

        return write

    def _find_last_content_item(self) -> Optional[tuple[int, Dict[str, Any]]]:
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            if it.get("kind") in ("user", "assistant", "tool", "diff"):
                return i, it
        return None

    def _find_visible_content_item(self) -> Optional[tuple[int, Dict[str, Any]]]:
        try:
            log = self.query_one("#timeline", SelectableRichLog)
        except NoMatches:
            return self._find_last_content_item()
        scroll_y = log.scroll_offset.y if log.scroll_offset else 0
        if scroll_y == 0:
            return self._find_last_content_item()

        width = log.scrollable_content_region.width
        verbose = self.reducer.session.verbose
        estimated_lines = 0
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            # Accumulate every item's height so the running offset matches what
            # is actually rendered; only content items can be the return target.
            if it.get("streaming", False):
                lines = calculate_item_lines(it, verbose, width)
            else:
                key = (it.get("id"), verbose, bool(it.get("collapsed")), width)
                cached = self._line_count_cache.get(key)
                if cached is None:
                    cached = calculate_item_lines(it, verbose, width)
                    self._line_count_cache[key] = cached
                lines = cached
            estimated_lines += lines
            if it.get("kind") not in ("user", "assistant", "tool", "diff"):
                continue
            if estimated_lines >= scroll_y:
                return i, it
        return self._find_last_content_item()

    def action_toggle_collapse(self) -> None:
        found = self._find_visible_content_item()
        if found is None:
            self.notify("No item to collapse")
            return
        idx, it = found
        it["collapsed"] = not it.get("collapsed", False)
        state = "collapsed" if it["collapsed"] else "expanded"
        self.notify(f"{it.get('kind', 'item').capitalize()} {state}")
        self._refresh_ui("full")

    def action_expand_full(self) -> None:
        found = self._find_visible_content_item()
        if found is None:
            self.notify("No item to expand")
            return
        _, it = found
        kind = it.get("kind", "")
        if kind == "diff":
            title = f"Full Diff — {it.get('path', '')}"
            content = format_diff_gutter(str(it.get("diff", "")), max_lines=10_000)
        elif kind in ("assistant",):
            title = "Full Assistant Response"
            content = str(it.get("content", ""))
        elif kind == "user":
            title = "Full User Message"
            content = str(it.get("text", ""))
        else:
            self.notify(f"Cannot expand {kind} items")
            return
        if not content.strip():
            self.notify("No content to show")
            return
        self.run_worker(self._show_full_content(title, content), exclusive=True)

    async def _show_full_content(self, title: str, content: str) -> None:
        await self.push_screen_wait(FullContentScreen(title, content))

    # ── Project file scanning ────────────────────────────────────────

    async def _scan_project_files(self) -> None:
        if self._scan_in_progress:
            return
        self._scan_in_progress = True
        try:
            root = getattr(self.agent.config, "project_root", None) if self.agent else None
            if not root:
                root = self.reducer.session.cwd or os.getcwd()
            self.project_files = await async_scan_project_files(root)
        except Exception as e:
            import logging as _log

            _log.getLogger(__name__).warning("Project file scan failed: %s", e)
        finally:
            self._scan_in_progress = False

    def action_file_mention(self) -> None:
        self.run_worker(self._show_file_mention(), exclusive=True)

    async def _show_file_mention(self) -> None:
        if self.project_files:
            # Serve the cached scan immediately; refresh it in the background
            # for the next invocation (_scan_in_progress guards concurrency).
            self.run_worker(self._scan_project_files)  # type: ignore[arg-type]
        else:
            await self._scan_project_files()
        result = await self.push_screen_wait(
            FilePickerScreen(
                self.project_files,
                placeholder="🔍 Type to search files to mention and pin…",
                footer_help=(f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ mention + pin  ⎋ close[/]"),
            )
        )
        prompt = self.query_one("#prompt-area", PromptArea)
        if result:
            prompt.insert(f"@{result} ")
            if self.controller:
                self.controller.enqueue_command("manage_context", action="add", path=result)
        prompt.focus()

    def action_command_palette(self) -> None:
        self.run_worker(self._show_palette(), exclusive=True)

    async def _show_palette(self, only_section: Optional[str] = None) -> None:
        result = await self.push_screen_wait(
            CommandPaletteScreen(self.reducer.session, only_section)
        )
        if result is None or not self.controller:
            return
        r = result.strip()
        if r.startswith("/"):
            if r in ("/help", "/?"):
                return
            self._submit(r)
        else:
            self.query_one("#prompt-area", PromptArea).text = r
            self.query_one("#prompt-area", PromptArea).focus()

    @on(PromptArea.Submitted)
    def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        if not self.reducer.session.ready:
            return
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.text = ""
        text = event.text.strip()
        if not text:
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        if not self.controller:
            return
        if text.startswith("/"):
            handled = handle_slash_command(
                text,
                self.controller,
                self.reducer,
                show_palette=self._show_palette_section,
                show_search=self._show_search,
                show_context=self._show_context,
                clear_context=self._clear_context,
                toggle_verbose=self._toggle_verbose,
                reveal_reasoning=self._reveal_reasoning,
                confirm_exit=self._confirm_exit,
                set_search_filter=lambda q: setattr(self, "_search_filter", q),
                retry_agent=self._retry_agent,
                rewind_timeline=self._rewind_timeline,
                resume_session=self._resume_session,
                copy_to_clipboard=self._osc52_writer(),
            )
            if handled:
                return
        self.reducer._push({"kind": "user", "id": self.reducer.next_id(), "text": text})
        self.reducer._bump_refresh("append")
        self.reducer._notify()
        self._awaiting_response = True
        self.controller.enqueue_command("send_message", text=text)

    def _show_palette_section(self, section: str | None = None) -> None:
        self.run_worker(self._show_palette(section), exclusive=True)

    def _start_agent_worker(self) -> None:
        """Run the backend agent loop on the exclusive background worker thread."""
        self.run_worker(
            self._run_agent,  # type: ignore[arg-type]
            exclusive=True,
            thread=True,
            name="agent-loop",
        )

    def _retry_agent(self) -> None:
        self._agent_retry_count = 0
        self.reducer.session.ready = False
        self._toast("info", "Restarting agent…")
        self._start_agent_worker()

    def _resume_session(self, session_id: Optional[str]) -> None:
        """/resume entry point: with an id, resume it; without, open the picker."""
        if session_id:
            self._start_resumed_agent(session_id)
        else:
            self.run_worker(self._show_session_picker(), exclusive=True)

    async def _show_session_picker(self) -> None:
        # list_sessions hits the filesystem (index rebuild, expiry cleanup) —
        # keep it off the UI loop.
        sessions = await asyncio.to_thread(history_manager.list_sessions)
        if not sessions:
            self._toast("info", "No saved sessions to resume.")
            return
        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        result = await self.push_screen_wait(SessionPickerScreen(sessions, current_id=current_id))
        if result:
            self._start_resumed_agent(result)

    def _start_resumed_agent(self, session_id: str) -> None:
        """Swap the live agent for one resumed from a saved session.

        The old controller is asked to exit (its goodbye is suppressed), the
        local timeline is cleared, and the agent worker restarts with the
        resume id — same lifecycle as /retry, plus session selection.
        """
        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        if session_id == current_id:
            self._toast("info", "That session is already active.")
            return
        if self.controller:
            self._suppress_goodbye = True
            self.controller.enqueue_command("exit")
        # Retire the old session's agents from the tracker before the new
        # agent registers — the new controller's bootstrap re-emits every
        # tracked agent, so stale entries would reappear in the tree.
        agent_tracker.clear_except()
        self.reducer.session.agents.clear()
        self.reducer.timeline.clear()
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._resume = session_id
        self._continue = False
        self._agent_retry_count = 0
        self.reducer.session.ready = False
        self._awaiting_response = False
        self._toast("info", f"Resuming session {session_id}…")
        self._refresh_ui("full")
        self._start_agent_worker()

    def _show_search(self) -> None:
        # push_screen is synchronous; no worker/async wrapper needed.
        self.push_screen(SearchScreen(self.reducer.timeline, self._search_filter))

    def _show_context(self) -> None:
        files = self.reducer.session.context_files or []
        msg = "\n".join(f"  {f.get('path')} ({f.get('size', 0)} B)" for f in files) or "(none)"
        self._toast("info", f"Pinned context:\n{msg}")

    def _reset_timeline_view(self) -> None:
        """Repaint after clearing/truncating the timeline and re-pin to bottom."""
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._refresh_ui("full")
        try:
            self.query_one("#timeline", SelectableRichLog).scroll_end(animate=False)
        except NoMatches:
            pass

    def _clear_context(self) -> None:
        if self.controller:
            self.controller.enqueue_command("clear_context")
        self.reducer.timeline.clear()
        self._reset_timeline_view()

    def _rewind_timeline(self, turn: int) -> None:
        """Truncate the local timeline to before the Nth user message.

        Mirrors ``_clear_context`` but stops at a turn boundary instead of
        wiping everything; the backend ``rewind`` command truncates the
        session history in parallel.
        """
        count = 0
        cut_idx: Optional[int] = None
        for i, it in enumerate(self.reducer.timeline):
            if it.get("kind") == "user":
                count += 1
                if count == turn:
                    cut_idx = i
                    break
        if cut_idx is None:
            return
        del self.reducer.timeline[cut_idx:]
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._refresh_ui("full")
        try:
            self.query_one("#timeline", SelectableRichLog).scroll_end(animate=False)
        except NoMatches:
            pass

    def _toggle_verbose(self) -> None:
        self.reducer.session.verbose = not self.reducer.session.verbose
        level = "verbose" if self.reducer.session.verbose else "normal"
        if self.controller:
            self.controller.enqueue_command("set_verbosity", level=level)
        self.notify(f"Verbose {'on' if self.reducer.session.verbose else 'off'}")

    def _reveal_reasoning(self) -> None:
        for it in reversed(self.reducer.timeline):
            if it.get("kind") == "assistant" and (it.get("reasoning") or "").strip():
                self._toast("info", it["reasoning"][:4000])
                return
        self.notify("No reasoning to reveal")

    def _confirm_exit(self) -> bool:
        now = time.monotonic()
        if self._exit_armed_at and now - self._exit_armed_at < 5:
            if self.controller:
                self.controller.enqueue_command("exit")
            self.exit()
            return True
        self._exit_armed_at = now
        return False


def run_chat_app(
    *,
    model: Optional[str] = None,
    resume: Optional[str] = None,
    continue_: bool = False,
    auto_approve: bool = False,
    persona: Optional[str] = None,
) -> None:
    """Entry point for ``coderAI chat``."""
    import logging

    from coderAI.system.logging_setup import setup_logging

    # Route logs to a file while Textual owns the terminal — any stderr
    # write would corrupt the display. Restore stderr logging on exit.
    root_level = logging.getLogger().level or None
    setup_logging(root_level, tui_mode=True)
    app = CoderAIApp(
        model=model,
        resume=resume,
        continue_=continue_,
        auto_approve=auto_approve,
        persona=persona,
    )
    try:
        app.run()
    finally:
        setup_logging(root_level, tui_mode=False)

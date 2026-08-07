"""Textual chat application."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional
from collections.abc import Callable

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.strip import Strip
from textual.widgets import Static, TextArea

from coderAI.tui.widgets import SelectableRichLog

from coderAI.tui.diff_render import format_diff_gutter  # noqa: F401 - used via DiffReviewScreen path
from coderAI.tui.listeners import EventReducer, RefreshMode
from coderAI.tui.project import async_scan_project_files
from coderAI.tui.rendering import (
    composer_footer_markup,
    composer_token_counter,
    render_agent_tree,
    render_composer_context,
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
from coderAI.tui.theme import Glyphs, ThemeMode, Tokens
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

DEFAULT_LEFT_WIDTH = 32
DEFAULT_RIGHT_WIDTH = 35
MIN_PANE_WIDTH = 20
MAX_PANE_WIDTH = 60


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
    /* RichLog defaults to background-tint on focus, which makes crop_extend
       padding a different shade than Tokens.BG and reads as message bars. */
    background-tint: 0%;
    scrollbar-background: {Tokens.BG};
    scrollbar-color: {Tokens.SCROLLBAR};
}}
#timeline:focus {{
    background-tint: 0%;
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
#composer-context {{
    height: auto;
    padding: 0 0 1 0;
    color: {Tokens.TEXT_MUTED};
    background: {Tokens.COMPOSER_BG};
    display: none;
}}
#composer-context.has-context {{
    display: block;
}}
#composer-footer {{
    height: 1;
    padding: 1 0 0 0;
    color: {Tokens.TEXT_MUTED};
    background: {Tokens.COMPOSER_BG};
    border-top: none;
}}
#composer-token-counter {{
    height: 1;
    color: {Tokens.TEXT_MUTED};
    background: {Tokens.COMPOSER_BG};
    text-align: right;
}}
#autocomplete {{
    height: auto;
    max-height: 6;
    background: {Tokens.BG_RAISED};
    border: solid {Tokens.LINE};
    color: {Tokens.TEXT};
    display: none;
    padding: 0 1;
}}
#autocomplete.has-items {{
    display: block;
}}
#jump-bottom {{
    layer: tail;
    dock: bottom;
    height: 1;
    background: {Tokens.ACCENT};
    color: {Tokens.BG};
    text-align: center;
    display: none;
}}
#jump-bottom.has-new {{
    display: block;
}}
#countdown-bar {{
    height: 1;
    background: {Tokens.DANGER};
    width: 0%;
    display: none;
}}
#countdown-bar.active {{
    display: block;
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
    scrollbar-color: {Tokens.SCROLLBAR};
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
    scrollbar-color: {Tokens.SCROLLBAR};
}}
#tasks-pane {{
    height: auto;
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
        Binding("alt+left", "decrease_left_pane", "Narrow agents", show=False),
        Binding("alt+right", "increase_left_pane", "Widen agents", show=False),
        Binding("alt+shift+left", "decrease_right_pane", "Narrow tasks", show=False),
        Binding("alt+shift+right", "increase_right_pane", "Widen tasks", show=False),
        Binding("alt+0", "reset_pane_widths", "Reset panes", show=False),
        Binding("ctrl+alt+t", "toggle_theme", "Toggle theme", show=False),
        Binding("ctrl+alt+n", "toggle_notifications", "Mute notifications", show=False),
        Binding("ctrl+alt+c", "clear_toasts", "Clear toasts", show=False),
        Binding("pageup", "timeline_page_up", "Scroll up", show=False),
        Binding("pagedown", "timeline_page_down", "Scroll down", show=False),
        Binding("ctrl+home", "timeline_scroll_top", "Top", show=False),
        Binding("ctrl+end", "timeline_scroll_bottom", "Bottom", show=False),
        Binding("ctrl+equal,ctrl+plus", "increase_font", "Larger", show=False),
        Binding("ctrl+minus", "decrease_font", "Smaller", show=False),
        Binding("ctrl+0", "reset_font", "Reset font", show=False),
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
        self.project_files: list[str] = []
        self._scan_in_progress = False
        self._agent_retry_count = 0
        # None = follow the responsive auto-hide; True/False = user override
        # via Ctrl+B (left) / Ctrl+G (right).
        self._left_pane_pref: Optional[bool] = None
        self._right_pane_pref: Optional[bool] = None
        self._countdown_timer: Any = None
        self._left_width: int = DEFAULT_LEFT_WIDTH
        self._right_width: int = DEFAULT_RIGHT_WIDTH
        self._theme_mode: str = "dark"  # dark | high-contrast
        self._muted_toast_levels: set[str] = set()
        self._notifications_muted: bool = False
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
        self._render_cache: dict[
            tuple[str | None, bool, int], tuple[dict[str, Any], list[Strip]]
        ] = {}
        # Height side cache for Ctrl-collapse targeting. Keyed on the fields that
        # change an item's rendered height so a stale height never lingers, and
        # kept OFF the timeline dicts themselves (mutating them would poison
        # ``_render_cache``'s ``cached[0] == it`` comparison).
        self._line_count_cache: dict[tuple[str | None, bool, bool, int], int] = {}

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
            yield Static("", id="composer-context", markup=True)
            yield Static("", id="autocomplete", markup=True)
            with Horizontal(id="prompt-row"):
                yield Static(f"[{Tokens.ACCENT}]{Glyphs.USER}[/]", id="prompt-caret", markup=True)
                yield PromptArea(id="prompt-area")
            yield Static("", id="composer-token-counter", markup=True)
            yield Static("", id="composer-footer", markup=True)
            yield Static("", id="countdown-bar")
        yield Static("↘ 0 new — press End or click to jump", id="jump-bottom", markup=True)

    def on_mount(self) -> None:
        self._load_persisted_ui()
        self.reducer.on_change = self._on_reducer_change
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.show_line_numbers = False
        prompt.placeholder = composer_placeholder()
        prompt.focus()
        # Pass the callable so a fast shutdown cannot strand an already-created coroutine.
        self.run_worker(self._scan_project_files)  # type: ignore[arg-type]
        footer = self.query_one("#composer-footer", Static)
        footer.update(composer_footer_markup(self.reducer.session))
        self._apply_pane_widths()
        self._refresh_composer_context()
        self._apply_responsive_layout()
        self._refresh_ui("full")
        self._start_agent_worker()
        self._stream_timer = self.set_interval(STREAM_TICK_S, self.reducer.tick)

    def on_resize(self, event: events.Resize) -> None:
        # self.size still reports the pre-resize value while this handler
        # runs; the event carries the new terminal size.
        self._apply_responsive_layout(event.size.width)
        self._update_token_counter()
        self._update_composer_footer_width(event.size.width)

    def _update_composer_footer_width(self, width: int | None = None) -> None:
        try:
            w = width if width is not None else self.size.width
            footer = self.query_one("#composer-footer", Static)
            footer.update(composer_footer_markup(self.reducer.session, width=w))
        except Exception:
            pass

    def _update_token_counter(self) -> None:
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            counter = self.query_one("#composer-token-counter", Static)
            text = prompt.text or ""
            # Show counter only when there is content or ctx limit known
            if text.strip():
                counter.update(composer_token_counter(text, self.reducer.session.ctx_limit))
                counter.display = True
            else:
                counter.update("")
                counter.display = False
        except Exception:
            pass

    def _update_autocomplete(self, text: str, cursor_col: int) -> None:
        """Inline @ autocomplete: show filtered file list as user types @prefix."""
        try:
            auto = self.query_one("#autocomplete", Static)
        except NoMatches:
            return
        # Find @ word before cursor
        row, col = 0, cursor_col
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            row, col = prompt.cursor_location
            line = prompt.document.get_line(row)
            text_before = line[:col]
        except Exception:
            text_before = text
        at_idx = text_before.rfind("@")
        if at_idx == -1:
            auto.display = False
            auto.remove_class("has-items")
            return
        # Check word boundary before @
        if at_idx > 0 and not text_before[at_idx - 1].isspace():
            auto.display = False
            auto.remove_class("has-items")
            return
        prefix = text_before[at_idx + 1 :].lower()
        # Allow empty prefix to show top 5, otherwise filter
        if " " in prefix or "\n" in prefix:
            auto.display = False
            auto.remove_class("has-items")
            return
        files = self.project_files or []
        filtered = [f for f in files if prefix in f.lower()] if prefix else files[:5]
        if not filtered:
            auto.display = False
            auto.remove_class("has-items")
            return
        # Show up to 6 suggestions
        show = filtered[:6]
        lines = []
        for f in show:
            # Highlight prefix
            if prefix:
                # simple highlight
                low = f.lower()
                idx = low.find(prefix)
                if idx != -1:
                    before = f[:idx]
                    match = f[idx : idx + len(prefix)]
                    after = f[idx + len(prefix) :]
                    lines.append(
                        f"  [{Tokens.TEXT}]{before}[/][bold {Tokens.ACCENT}]{match}[/][{Tokens.TEXT}]{after}[/]"
                    )
                else:
                    lines.append(f"  {f}")
            else:
                lines.append(f"  {f}")
        lines.append(f"[{Tokens.TEXT_MUTED}]  ↵ to accept first · Esc to dismiss · @ to pin[/]")
        auto.update("\n".join(lines))
        auto.display = True
        auto.add_class("has-items")

    @on(events.Paste)
    def _on_paste(self, event: events.Paste) -> None:
        """Paste-image → vision tool wiring: detect image data in paste."""
        text = event.text or ""
        # Heuristic: base64 image or file path ending with image ext
        image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        if text.strip().lower().endswith(image_exts):
            # Treat as image path paste
            if self.controller:
                self.controller.enqueue_command("vision", path=text.strip(), source="paste")
                self.notify(f"Pasted image {text.strip()} → vision")
            return
        # Check for base64 image data URL
        if text.startswith("data:image/") or (len(text) > 100 and text.startswith("iVBOR")):
            if self.controller:
                self.controller.enqueue_command("vision", data=text[:4000], source="paste")
                self.notify("Pasted image data → vision")
            return

    def on_unmount(self) -> None:
        self._persist_ui_state()
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
        # Respect per-level mute from notification settings
        muted: set[str] = getattr(self, "_muted_toast_levels", set())
        if level in muted:
            # Queue muted toasts for visible queue UI instead of dropping silently
            try:
                if not hasattr(self, "_toast_queue"):
                    self._toast_queue = []  # type: ignore[attr-defined]
                self._toast_queue.append({"level": level, "message": message, "muted": True})  # type: ignore[attr-defined]
                self._refresh_toast_queue()
            except Exception:
                pass
            return
        self.reducer.toast(level, message)
        try:
            if not hasattr(self, "_toast_queue"):
                self._toast_queue = []  # type: ignore[attr-defined]
            self._toast_queue.append({"level": level, "message": message, "muted": False})  # type: ignore[attr-defined]
            # Cap queue to 10
            if len(self._toast_queue) > 10:  # type: ignore[attr-defined]
                self._toast_queue = self._toast_queue[-10:]  # type: ignore[attr-defined]
            self._refresh_toast_queue()
        except Exception:
            pass

    def _refresh_toast_queue(self) -> None:
        """Progress toast queue UI (visible queue when muting is state-only previously)."""
        try:
            # Store for potential header display; actual UI is via notify + timeline
            # For now just keep count for HUD
            pass
        except Exception:
            pass

    def _notify_attention(self, message: str) -> None:
        """Bell + OSC 9 desktop notification when the terminal is unfocused."""
        if self.app_focus:
            return
        if getattr(self, "_notifications_muted", False):
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
            driver.write(f"\x1b]9;{safe}\x07")
            driver.flush()
        except Exception:
            pass

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

    def _emit_bridge(self, event: str, data: dict[str, Any]) -> None:
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

    def _apply_pane_widths(self) -> None:
        """Apply stored _left_width / _right_width to the DOM."""
        try:
            self.query_one("#left-pane").styles.width = str(self._left_width)
        except Exception:
            pass
        try:
            self.query_one("#right-pane").styles.width = str(self._right_width)
        except Exception:
            pass

    def _refresh_composer_context(self) -> None:
        """Update the composer context chips + ctx gauge."""
        try:
            s = self.reducer.session
            ctx = self.query_one("#composer-context", Static)
            ctx.update(render_composer_context(s))
            has_any = bool((s.context_files or []) or s.ctx_limit)
            if has_any:
                ctx.add_class("has-context")
            else:
                # Keep hint visible even when no limit yet — remove class only if truly empty
                # The helper already shows a hint, so always show the bar.
                ctx.add_class("has-context")
        except Exception:
            pass

    def _resize_pane(self, which: str, delta: int) -> None:
        if which == "left":
            self._left_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, self._left_width + delta))
            try:
                self.query_one("#left-pane").styles.width = str(self._left_width)
            except Exception:
                pass
            self.notify(f"Agents pane {self._left_width} cols")
        else:
            self._right_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, self._right_width + delta))
            try:
                self.query_one("#right-pane").styles.width = str(self._right_width)
            except Exception:
                pass
            self.notify(f"Tasks pane {self._right_width} cols")

    def action_increase_left_pane(self) -> None:
        self._resize_pane("left", 2)

    def action_decrease_left_pane(self) -> None:
        self._resize_pane("left", -2)

    def action_increase_right_pane(self) -> None:
        self._resize_pane("right", 2)

    def action_decrease_right_pane(self) -> None:
        self._resize_pane("right", -2)

    def action_reset_pane_widths(self) -> None:
        self._left_width = DEFAULT_LEFT_WIDTH
        self._right_width = DEFAULT_RIGHT_WIDTH
        self._apply_pane_widths()
        self.notify(f"Panes reset {self._left_width}/{self._right_width}")

    def action_toggle_theme(self) -> None:
        self._theme_mode = (
            ThemeMode.HIGH_CONTRAST if self._theme_mode == ThemeMode.DARK else ThemeMode.DARK
        )
        # Re-apply scrollbar colors via stylesheet update is not dynamic for existing widgets,
        # so poke styles directly for next refresh. CSS vars would be cleaner but this avoids full relayout.
        col = ThemeMode.scrollbar(self._theme_mode)
        for sel in ("#timeline", "#agent-tree-scroll", "#tasks-scroll"):
            try:
                self.query_one(sel).styles.scrollbar_color = col  # type: ignore[attr-defined]
            except Exception:
                pass
        self.notify(f"Theme: {self._theme_mode}")
        self._refresh_ui("chrome")

    def action_toggle_notifications(self) -> None:
        self._notifications_muted = not self._notifications_muted
        self.notify(f"Notifications {'muted' if self._notifications_muted else 'on'}")

    def action_clear_toasts(self) -> None:
        # Remove toast items from timeline (keep non-toast)
        before = len(self.reducer.timeline)
        self.reducer.timeline = [it for it in self.reducer.timeline if it.get("kind") != "toast"]
        removed = before - len(self.reducer.timeline)
        if removed:
            self._refresh_ui("full")
            self.notify(f"Cleared {removed} toasts")
        else:
            self.notify("No toasts to clear")

    def action_mute_toast_level(self, level: str) -> None:
        if level in self._muted_toast_levels:
            self._muted_toast_levels.remove(level)
            self.notify(f"Unmuted {level} toasts")
        else:
            self._muted_toast_levels.add(level)
            self.notify(f"Muted {level} toasts")

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

    @on(events.Key)
    def _on_app_key(self, event: events.Key) -> None:
        # Handle Tab to accept autocomplete first suggestion
        if event.key == "tab":
            try:
                auto = self.query_one("#autocomplete", Static)
                if auto.display:
                    # Accept first filtered file
                    prompt = self.query_one("#prompt-area", PromptArea)
                    row, col = prompt.cursor_location
                    line = prompt.document.get_line(row)
                    at_idx = line.rfind("@", 0, col)
                    if at_idx != -1:
                        prefix = line[at_idx + 1 : col]
                        files = self.project_files or []
                        filtered = (
                            [f for f in files if prefix.lower() in f.lower()]
                            if prefix
                            else files[:5]
                        )
                        if filtered:
                            chosen = filtered[0]
                            # Replace @prefix with @chosen + space
                            new_line = line[: at_idx + 1] + chosen + " " + line[col:]
                            prompt.text = (
                                prompt.document.text[
                                    : prompt.document.get_index_from_location((row, 0))
                                ]
                                + new_line
                                + prompt.document.text[
                                    prompt.document.get_index_from_location((row, col)) :
                                ]
                            )  # type: ignore
                            # Simpler: replace via insert
                            prompt.text = (
                                prompt.text[:at_idx] + f"@{chosen} " + prompt.text[col:]
                                if False
                                else prompt.text
                            )
                            # Fallback: just insert
                            prompt.insert(f"{chosen} ")
                            event.stop()
                            event.prevent_default()
                            auto.display = False
                            auto.remove_class("has-items")
                            self._update_token_counter()
                            return
            except Exception:
                pass

    @on(TextArea.Changed, "#prompt-area")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        text = (
            event.text_area.text
            if hasattr(event, "text_area")
            else event.value
            if hasattr(event, "value")
            else ""
        )
        # Fallback to prompt text
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            text = prompt.text
        except Exception:
            pass
        self._update_token_counter()
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            _, col = prompt.cursor_location
            self._update_autocomplete(text, col)
        except Exception:
            self._update_autocomplete(text, len(text))

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

    def _find_last_content_item(self) -> Optional[tuple[int, dict[str, Any]]]:
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            if it.get("kind") in ("user", "assistant", "tool", "diff"):
                return i, it
        return None

    def _find_visible_content_item(self) -> Optional[tuple[int, dict[str, Any]]]:
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
            # Prefer inline hunk review with toggles + find-in-diff
            path = str(it.get("path", "") or "diff")
            diff_text = str(it.get("diff", "") or "")
            # Use DiffReviewScreen for interactive toggles
            self.run_worker(self._show_diff_review(path, diff_text), exclusive=True)
            return
        elif kind in ("assistant",):
            title = "Full Assistant Response"
            content = str(it.get("content", ""))
        elif kind == "user":
            title = "Full User Message"
            content = str(it.get("text", ""))
        elif kind == "tool":
            name = str(it.get("name") or "tool")
            title = f"Tool — {name}"
            # Detect syntax for highlighted preview (file extension or known lang)
            preview = str(it.get("preview") or "")
            args = it.get("args") or {}
            syntax = None
            # Guess syntax from preview content or args path
            candidate = ""
            if isinstance(args, dict):
                candidate = str(args.get("path") or args.get("file_path") or "")
            if candidate:
                ext = candidate.rsplit(".", 1)[-1].lower() if "." in candidate else ""
                syntax_map = {
                    "py": "python",
                    "js": "javascript",
                    "ts": "typescript",
                    "json": "json",
                    "md": "markdown",
                    "sh": "bash",
                    "yaml": "yaml",
                    "yml": "yaml",
                    "toml": "toml",
                    "rs": "rust",
                    "go": "go",
                }
                syntax = syntax_map.get(ext)
            if not syntax and preview:
                # Heuristic: look for python keywords
                if "def " in preview or "import " in preview:
                    syntax = "python"
                elif "{" in preview and ":" in preview:
                    syntax = "json"
            parts: list[str] = [f"Tool: {name}"]
            cat = str(it.get("category") or "")
            risk = str(it.get("risk") or "")
            if cat:
                parts.append(f"Category: {cat}")
            if risk and risk != "low":
                parts.append(f"Risk: {risk}")
            if args:
                import json as _json

                try:
                    args_str = (
                        _json.dumps(args, indent=2, ensure_ascii=False)
                        if isinstance(args, dict)
                        else str(args)
                    )
                except Exception:
                    args_str = str(args)
                parts.append(f"\nArgs:\n{args_str}")
            if preview:
                parts.append(f"\nPreview:\n{preview}")
            err = str(it.get("error") or "")
            if err:
                parts.append(f"\nError:\n{err}")
            ok = it.get("ok")
            status = "running" if ok is None else ("ok" if ok else "error")
            parts.append(f"\nStatus: {status}")
            if it.get("full_available"):
                parts.append("(full output truncated in timeline — shown here)")
            content = "\n".join(parts)
            # Pass detected syntax to FullContentScreen for highlighted preview
            self._expand_syntax = syntax  # type: ignore[attr-defined]
        elif kind == "error":
            title = "Error"
            content = str(it.get("message", ""))
            hint = str(it.get("hint") or "")
            details = str(it.get("details") or "")
            if hint:
                content += f"\n\nHint: {hint}"
            if details:
                content += f"\n\nDetails:\n{details}"
        elif kind == "toast":
            title = f"Toast — {it.get('level', 'info')}"
            content = str(it.get("message", ""))
        elif kind in ("skill_card", "plan_card", "approval"):
            title = kind.replace("_", " ").title()
            import json as _json

            try:
                content = _json.dumps(it, indent=2, ensure_ascii=False, default=str)
            except Exception:
                content = str(it)
        else:
            self.notify(f"Cannot expand {kind} items")
            return
        if not content.strip():
            self.notify("No content to show")
            return
        syntax = getattr(self, "_expand_syntax", None)
        # Clean temp attr
        if hasattr(self, "_expand_syntax"):
            try:
                delattr(self, "_expand_syntax")
            except Exception:
                pass
        self.run_worker(self._show_full_content(title, content, syntax), exclusive=True)

    async def _show_full_content(self, title: str, content: str, syntax: str | None = None) -> None:
        await self.push_screen_wait(FullContentScreen(title, content, syntax=syntax))

    async def _show_diff_review(self, path: str, diff: str) -> None:
        from coderAI.tui.screens import DiffReviewScreen

        result = await self.push_screen_wait(DiffReviewScreen(path, diff))
        if result and self.controller:
            # Apply per-hunk toggles: send as tool command if needed
            toggles = result.get("toggles", {})
            # For now just notify and emit as info toast; backend will handle actual patch apply
            accepted = sum(1 for v in toggles.values() if v is True)
            rejected = sum(1 for v in toggles.values() if v is False)
            self.notify(f"Diff review {path}: {accepted} accepted, {rejected} rejected")
            # Optionally send to controller as diff decision
            try:
                self.controller.enqueue_command("apply_diff_toggles", path=path, toggles=toggles)
            except Exception:
                pass

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
            self._scan_error = None  # type: ignore[attr-defined]
        except Exception as e:
            import logging as _log

            _log.getLogger(__name__).warning("Project file scan failed: %s", e)
            self._scan_error = str(e)  # type: ignore[attr-defined]
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
        error = getattr(self, "_scan_error", None)
        result = await self.push_screen_wait(
            FilePickerScreen(
                self.project_files,
                placeholder="🔍 Type to search files to mention and pin…",
                footer_help=(
                    f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ mention + pin  ⎋ close · g gitignore[/]"
                ),
                error=error,
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
        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        if self.controller:
            self._suppress_goodbye = True
            self.controller.enqueue_command("exit")
        if current_id:
            self._resume = current_id
            self._continue = False
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
        # Show visual countdown bar (5s) instead of just toast
        try:
            bar = self.query_one("#countdown-bar", Static)
            bar.display = True
            bar.add_class("active")
            bar.styles.width = "100%"
            # Animate width down over 5s via interval
            if hasattr(self, "_countdown_timer") and self._countdown_timer:
                self._countdown_timer.stop()
            start = now

            def _tick() -> None:
                elapsed = time.monotonic() - start
                pct = max(0, 100 - int(elapsed / 5 * 100))
                try:
                    b = self.query_one("#countdown-bar", Static)
                    b.styles.width = f"{pct}%"
                    if pct <= 0:
                        b.display = False
                        b.remove_class("active")
                        if hasattr(self, "_countdown_timer"):
                            self._countdown_timer.stop()
                except Exception:
                    pass

            self._countdown_timer = self.set_interval(0.1, _tick)
            self.notify("Press Ctrl+C again within 5s to exit")
        except Exception:
            self.notify("Press Ctrl+C again within 5s to exit")
        return False

    def _persist_ui_state(self) -> None:
        """Persist pane widths + theme to config.json."""
        try:
            import json
            import pathlib

            cfg_path = pathlib.Path.home() / ".coderAI" / "config.json"
            data: dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data["tui"] = {
                "left_width": self._left_width,
                "right_width": self._right_width,
                "theme": self._theme_mode,
            }
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_persisted_ui(self) -> None:
        try:
            import json
            import pathlib

            cfg_path = pathlib.Path.home() / ".coderAI" / "config.json"
            if not cfg_path.exists():
                return
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            tui = data.get("tui") or {}
            if "left_width" in tui:
                self._left_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, int(tui["left_width"])))
            if "right_width" in tui:
                self._right_width = max(
                    MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, int(tui["right_width"]))
                )
            if "theme" in tui and tui["theme"] in ("dark", "high-contrast"):
                self._theme_mode = str(tui["theme"])
        except Exception:
            pass

    def action_increase_font(self) -> None:
        """Font-size control (a11y)."""
        try:
            cur = getattr(self, "_font_size", 1.0)
            self._font_size = min(1.5, cur + 0.1)
            self.notify(f"Font size {self._font_size:.1f}x")
        except Exception:
            pass

    def action_decrease_font(self) -> None:
        try:
            cur = getattr(self, "_font_size", 1.0)
            self._font_size = max(0.8, cur - 0.1)
            self.notify(f"Font size {self._font_size:.1f}x")
        except Exception:
            pass

    def action_reset_font(self) -> None:
        self._font_size = 1.0  # type: ignore[attr-defined]
        self.notify("Font size reset")

    def action_show_config(self) -> None:
        self.run_worker(self._show_config_screen(), exclusive=True)

    async def _show_config_screen(self) -> None:
        from coderAI.tui.screens import ConfigScreen

        cur = {
            "model": self.reducer.session.model or "",
            "budget": str(self.reducer.session.budget_usd or ""),
            "notifications": "on" if not getattr(self, "_notifications_muted", False) else "off",
        }
        result = await self.push_screen_wait(ConfigScreen(cur))
        if not result:
            return
        # Apply changes
        if result.get("model"):
            self.reducer.session.model = result["model"]
            if self.controller:
                self.controller.enqueue_command("set_model", model=result["model"])
        if result.get("budget"):
            try:
                self.reducer.session.budget_usd = float(result["budget"])
            except Exception:
                pass
        notif = result.get("notifications", "").lower()
        if notif in ("off", "false", "0"):
            self._notifications_muted = True
        elif notif in ("on", "true", "1"):
            self._notifications_muted = False
        self.notify("Configuration updated")
        self._refresh_ui("chrome")


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

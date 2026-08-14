# mypy: disable-error-code="has-type, misc, assignment"
"""Textual chat application."""

from __future__ import annotations

from typing import Any, Optional

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.strip import Strip
from textual.widgets import Static, TextArea

from coderAI.tui.widgets import SelectableRichLog

from coderAI.tui.diff_render import format_diff_gutter  # noqa: F401 - used via DiffReviewScreen path
from coderAI.tui.listeners import EventReducer
from coderAI.tui.project import async_scan_project_files  # noqa: F401 - compatibility patch point
from coderAI.tui.rendering import (
    composer_footer_markup,
)
from coderAI.tui.screens import (
    AgentEventMsg,
    PromptArea,
)
from coderAI.core.agent_tracker import agent_tracker  # noqa: F401 - compatibility patch point
from coderAI.system.history import history_manager  # noqa: F401 - compatibility patch point
from coderAI.tui.session_setup import create_agent_session  # noqa: F401 - compatibility patch point
from coderAI.tui.platform import composer_placeholder
from coderAI.tui.theme import Glyphs, Tokens
from coderAI.tui.timeline_render import (
    write_timeline_item,  # noqa: F401 - compatibility patch point
)
from coderAI.application.tui_app_lifecycle import AppLifecycleService
from coderAI.tui.app_events import AppEventController
from coderAI.tui.app_input import AppInputController
from coderAI.tui.app_layout import AppLayoutController
from coderAI.tui.app_timeline import AppTimelineController

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


class CoderAIApp(
    AppLifecycleService,
    AppLayoutController,
    AppTimelineController,
    AppEventController,
    AppInputController,
    App[None],
):
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

    @on(events.Paste)
    def _on_paste(self, event: events.Paste) -> None:
        AppLayoutController._on_paste(self, event)

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        await AppEventController._on_agent_event(self, msg)

    @on(events.Click)
    def _on_generic_click(self, event: events.Click) -> None:
        AppTimelineController._on_generic_click(self, event)

    @on(events.Key)
    def _on_app_key(self, event: events.Key) -> None:
        AppInputController._on_app_key(self, event)

    @on(TextArea.Changed, "#prompt-area")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        AppLayoutController._on_prompt_changed(self, event)

    @on(PromptArea.Submitted)
    def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        AppInputController._on_prompt_submitted(self, event)

    # ── Responsive layout ────────────────────────────────────────────

    # ── Desktop notifications ────────────────────────────────────────

    # ── UI refresh ───────────────────────────────────────────────────

    # ── Chrome (delegates to rendering.py) ───────────────────────────

    # ── Approval flow ────────────────────────────────────────────────

    # ── Keybindings ──────────────────────────────────────────────────

    # ── Project file scanning ────────────────────────────────────────


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

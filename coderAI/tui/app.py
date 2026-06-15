"""Textual chat application."""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Static

from coderAI.tui.widgets import SelectableRichLog

from coderAI.tui.diff_render import format_diff_gutter
from coderAI.tui.listeners import EventReducer, RefreshMode
from coderAI.tui.project import async_scan_project_files
from coderAI.tui.rendering import (
    composer_footer_markup,
    render_agent_tree,
    render_plan,
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
)
from coderAI.tui.session_setup import create_agent_session
from coderAI.tui.slash import handle_slash_command
from coderAI.tui.state import SessionState
from coderAI.tui.platform import composer_placeholder, supports_truecolor, truecolor_hint
from coderAI.tui.theme import Glyphs, Tokens
from coderAI.tui.timeline_render import (
    build_stream_tail_markup,
    write_timeline_item,
    calculate_item_lines,
)

STREAM_TICK_S = 0.12


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
    background: {Tokens.BG};
}}
#session-header {{
    height: auto;
    min-height: 2;
    padding: 1 2;
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
    height: auto;
    max-height: 40%;
    padding: 0 2 1 2;
    background: {Tokens.BG};
    display: none;
}}
#composer-box {{
    height: auto;
    margin: 1 2;
    background: {Tokens.BG_RAISED};
    border: round {Tokens.LINE};
    padding: 1 2;
}}
#prompt-row {{
    height: auto;
    background: {Tokens.BG_RAISED};
}}
#prompt-caret {{
    width: 2;
    height: auto;
    color: {Tokens.ACCENT};
    background: {Tokens.BG_RAISED};
}}
#prompt-area {{
    width: 1fr;
    height: auto;
    min-height: 2;
    max-height: 8;
    background: {Tokens.BG_RAISED};
    color: {Tokens.TEXT};
    border: none;
}}
#composer-footer {{
    height: 1;
    padding: 1 0 0 0;
    color: {Tokens.TEXT_MUTED};
    background: {Tokens.BG_RAISED};
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
#plan-scroll {{
    height: 1fr;
    padding: 1 2;
    background: {Tokens.BG_SUNK};
    border-bottom: solid {Tokens.LINE_SOFT};
    scrollbar-background: {Tokens.BG_SUNK};
    scrollbar-color: {Tokens.LINE};
}}
#tasks-scroll {{
    height: 1fr;
    padding: 1 2;
    background: {Tokens.BG_SUNK};
    scrollbar-background: {Tokens.BG_SUNK};
    scrollbar-color: {Tokens.LINE};
}}
#plan-pane {{
    height: auto;
    color: {Tokens.TEXT_DIM};
}}
#tasks-pane {{
    height: auto;
    color: {Tokens.TEXT_DIM};
}}
Footer {{
    background: {Tokens.BG_RAISED};
    color: {Tokens.TEXT_DIM};
}}
"""


class RecordingLog:
    """A duck-typed wrapper for RichLog to capture write calls during rendering."""

    def __init__(self) -> None:
        self.renderables: list[Any] = []

    def write(self, renderable: Any) -> None:
        self.renderables.append(renderable)


class CoderAIApp(App[None]):
    """CoderAI Textual chat — three-column IDE layout."""

    TITLE = "CoderAI"
    CSS = _build_coderai_css()

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True, priority=False),
        Binding("ctrl+c", "ctrl_c", "Exit", show=False),
        Binding("ctrl+shift+c, super+c", "copy_selection", "Copy", show=False),
        Binding("ctrl+k", "command_palette", "Commands", show=True),
        Binding("ctrl+t", "toggle_collapse", "Collapse", show=True),
        Binding("ctrl+o", "expand_full", "Expand", show=True),
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
        self._render_cache: Dict[tuple[str | None, bool], tuple[Dict[str, Any], list[Any]]] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left-pane"):
                with VerticalScroll(id="agent-tree-scroll"):
                    yield Static("", id="agent-tree-content", markup=True)
            with Vertical(id="center"):
                yield Static("", id="session-header", markup=True)
                yield SelectableRichLog(id="timeline", highlight=True, markup=True, wrap=True)
                yield Static("", id="stream-tail", markup=True)
            with Vertical(id="right-pane"):
                with VerticalScroll(id="plan-scroll"):
                    yield Static("", id="plan-pane", markup=True)
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
        self.run_worker(self._scan_project_files())
        footer = self.query_one("#composer-footer", Static)
        footer.update(self._composer_footer_markup())
        self._refresh_ui("full")
        if sys.stdout.isatty() and not supports_truecolor():
            self._toast("warning", truecolor_hint())
        self.run_worker(
            self._run_agent,  # type: ignore[arg-type]
            exclusive=True,
            thread=True,
            name="agent-loop",
        )
        self._stream_timer = self.set_interval(STREAM_TICK_S, self._stream_tick)

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
        self.reducer._push(
            {"kind": "toast", "id": self.reducer.next_id(), "level": level, "message": message}
        )
        self.reducer._bump_refresh("append")
        self.reducer._notify()

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        if msg.event == "__refresh__":
            self._refresh_ui(str(msg.data.get("mode", "full")))
            return
        self.reducer.handle(msg.event, msg.data)
        if msg.event == "tool" and msg.data.get("phase") == "awaiting_approval":
            self.run_worker(self._maybe_show_approval())

    def _stream_tick(self) -> None:
        flushed_stream = self.reducer._maybe_flush_stream()
        # When streaming ends, the time-gate may leave un-flushed content
        # in the buffers. Force one last flush so the user sees final output.
        if not flushed_stream and self.reducer._stream_flush_at is None:
            if self.reducer._stream_pending_content or self.reducer._stream_pending_reasoning:
                flushed_stream = self.reducer._flush_stream_buffers()
        if flushed_stream:
            self.reducer._bump_refresh("stream")
        flushed_status = self.reducer._maybe_flush_status()
        if flushed_stream or flushed_status:
            self.reducer._notify()

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
                self.run_worker(
                    self._run_agent,  # type: ignore[arg-type]
                    exclusive=True,
                    thread=True,
                    name="agent-loop",
                )
            else:
                self._emit_bridge(
                    "info",
                    {"message": "Agent crashed. Type /retry to restart."},
                )
                self._emit_bridge("goodbye", {"reason": "loop_crashed"})

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
        else:
            idx = self._log_rendered_idx
            while idx < len(timeline):
                it = timeline[idx]
                if it.get("kind") == "assistant" and it.get("streaming"):
                    self._render_stream_tail(it, verbose)
                    break

                item_id = it.get("id")
                cache_key = (item_id, verbose)
                cached = self._render_cache.get(cache_key)
                if cached is not None:
                    cached_it, renderables = cached
                    if cached_it == it:
                        for r in renderables:
                            log.write(r)
                        idx += 1
                        continue

                rec = RecordingLog()
                write_timeline_item(rec, it, verbose=verbose)
                self._render_cache[cache_key] = (it.copy(), rec.renderables)
                for r in rec.renderables:
                    log.write(r)
                idx += 1
            self._log_rendered_idx = idx
            if idx > 0:
                log.scroll_end(animate=False)
            if idx >= len(timeline):
                if not self.reducer.session.streaming and not any(
                    it.get("kind") == "assistant" and it.get("streaming") for it in timeline
                ):
                    self._hide_stream_tail()

        self._render_chrome(s)
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            prompt.disabled = not s.ready
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
            footer.update(self._composer_footer_markup())
        except NoMatches:
            pass

    # ── Chrome (delegates to rendering.py) ───────────────────────────

    def _render_chrome(self, s: SessionState) -> None:
        self._render_session_header(s)
        self._render_agent_tree(s)
        self._render_plan(s)
        self._render_tasks(s)

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

    def _render_session_header(self, s: SessionState) -> None:
        try:
            header = self.query_one("#session-header", Static)
        except NoMatches:
            return
        header.update(render_session_header(s))

    def _render_agent_tree(self, s: SessionState) -> None:
        try:
            tree = self.query_one("#agent-tree-content", Static)
        except NoMatches:
            return
        tree.update(render_agent_tree(s))

    def _render_plan(self, s: SessionState) -> None:
        try:
            pane = self.query_one("#plan-pane", Static)
        except NoMatches:
            return
        pane.update(render_plan(s))

    def _render_tasks(self, s: SessionState) -> None:
        try:
            pane = self.query_one("#tasks-pane", Static)
        except NoMatches:
            return
        pane.update(render_tasks(s))

    def _composer_footer_markup(self) -> str:
        return composer_footer_markup(self.reducer.session)

    # ── Approval flow ────────────────────────────────────────────────

    async def _maybe_show_approval(self) -> None:
        pending = self.reducer.pending_approval()
        if not pending:
            return
        result = await self.push_screen_wait(ApprovalScreen(pending))
        if result is None:
            return
        approve, always = result
        if self.controller:
            if approve and always and not self.reducer.session.auto_approve:
                self.controller.enqueue_command("toggle_auto_approve")
            self.controller.enqueue_command(
                "tool_approval_resp",
                toolId=pending["id"],
                approve=approve,
            )
        pending["decided"] = "approved" if approve else "denied"
        self._refresh_ui("full")

    # ── Keybindings ──────────────────────────────────────────────────

    def action_cancel_turn(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if self.controller:
            self.controller.enqueue_command("cancel")

    def action_ctrl_c(self) -> None:
        now = time.monotonic()
        if self._exit_armed_at and now - self._exit_armed_at < 5:
            if self.controller:
                self.controller.enqueue_command("exit")
            self.exit()
        else:
            self._exit_armed_at = now
            self.notify("Press Ctrl+C again within 5s to exit")

    def action_copy_selection(self) -> None:
        log = self.query_one("#timeline", SelectableRichLog)
        selection = log.text_selection
        if selection:
            self.copy_to_clipboard(str(selection))
            self.notify("Copied to clipboard")

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

        estimated_lines = 0
        verbose = self.reducer.session.verbose
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            if it.get("kind") not in ("user", "assistant", "tool", "diff"):
                continue
            cache_key = f"_line_count_{verbose}"
            if it.get("streaming", False) or cache_key not in it:
                it[cache_key] = calculate_item_lines(it, verbose)
            lines = it[cache_key]
            estimated_lines += lines
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
        await self._scan_project_files()
        result = await self.push_screen_wait(
            FilePickerScreen(
                self.project_files,
                placeholder="🔍 Type to search files to mention…",
                footer_help=(
                    f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ insert @path  ⎋ close[/]"
                ),
            )
        )
        prompt = self.query_one("#prompt-area", PromptArea)
        if result:
            prompt.insert(f"@{result} ")
            if self.controller:
                self.controller.enqueue_command(
                    "manage_context", action="add", path=result
                )
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
                show_help=lambda: self._show_palette_section(),
                show_model_menu=lambda: self._show_palette_section("models"),
                show_reasoning_menu=lambda: self._show_palette_section("reasoning"),
                show_persona_menu=lambda: self._show_palette_section("personas"),
                show_skills_menu=lambda: self._show_palette_section("skills"),
                show_search=self._show_search,
                show_context=self._show_context,
                clear_context=self._clear_context,
                toggle_verbose=self._toggle_verbose,
                reveal_reasoning=self._reveal_reasoning,
                confirm_exit=self._confirm_exit,
                set_search_filter=lambda q: setattr(self, "_search_filter", q),
                retry_agent=self._retry_agent,
                rewind_timeline=self._rewind_timeline,
            )
            if handled:
                return
        self.reducer._push({"kind": "user", "id": self.reducer.next_id(), "text": text})
        self.reducer._bump_refresh("append")
        self.reducer._notify()
        self.controller.enqueue_command("send_message", text=text)

    def _show_palette_section(self, section: str | None = None) -> None:
        self.run_worker(self._show_palette(section), exclusive=True)

    def _retry_agent(self) -> None:
        self._agent_retry_count = 0
        self.reducer.session.ready = False
        self._toast("info", "Restarting agent…")
        self.run_worker(
            self._run_agent,  # type: ignore[arg-type]
            exclusive=True,
            thread=True,
            name="agent-loop",
        )

    def _show_search(self) -> None:
        self.run_worker(self._show_search_async(), exclusive=True)

    async def _show_search_async(self) -> None:
        self.push_screen(SearchScreen(self.reducer.timeline, self._search_filter))

    def _show_context(self) -> None:
        files = self.reducer.session.context_files or []
        msg = "\n".join(f"  {f.get('path')} ({f.get('size', 0)} B)" for f in files) or "(none)"
        self._toast("info", f"Pinned context:\n{msg}")

    def _clear_context(self) -> None:
        if self.controller:
            self.controller.enqueue_command("clear_context")
        self.reducer.timeline.clear()
        self._log_rendered_idx = 0
        self._refresh_ui("full")
        try:
            self.query_one("#timeline", SelectableRichLog).scroll_end(animate=False)
        except NoMatches:
            pass

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

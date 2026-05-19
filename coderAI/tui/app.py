"""Textual chat application."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static, TextArea

from .diff_render import format_diff_compact
from .help_menu import HELP_MENU_ENTRIES
from .listeners import EventReducer
from .session_setup import create_agent_session
from .slash import handle_slash_command
from .state import AgentInfo, SessionState
from .theme import Glyphs, Styles, Tokens

STREAM_TICK_S = 0.12


class AgentEventMsg(Message):
    """Agent event delivered to the UI thread."""

    def __init__(self, event: str, data: Dict[str, Any]) -> None:
        super().__init__()
        self.event = event
        self.data = data


class PromptArea(TextArea):
    """TextArea that submits on Enter and inserts a newline on Shift/Alt+Enter."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class ApprovalScreen(ModalScreen[tuple[bool, bool]]):
    """Tool approval dialog. Dismisses with (approve, always)."""

    DEFAULT_CSS = f"""
    ApprovalScreen {{
        align: center middle;
    }}
    ApprovalScreen #approval-box {{
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round {Tokens.WARN};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    ApprovalScreen #approval-header {{
        color: {Tokens.WARN};
        text-style: bold;
        margin-bottom: 1;
    }}
    ApprovalScreen #approval-diff {{
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT};
        padding: 1;
        margin: 1 0;
    }}
    ApprovalScreen Label {{
        color: {Tokens.TEXT_DIM};
    }}
    ApprovalScreen Horizontal {{
        height: auto;
        align-horizontal: center;
        margin-top: 1;
    }}
    ApprovalScreen Button {{
        margin: 0 1;
    }}
    """

    def __init__(self, approval: Dict[str, Any]) -> None:
        super().__init__()
        self.approval = approval

    def compose(self) -> ComposeResult:
        diff = self.approval.get("diff")
        diff_text = format_diff_compact(diff) if diff else ""
        with Container(id="approval-box"):
            yield Label(
                f"{Glyphs.APPROVAL}  Approve [bold]{escape(str(self.approval.get('tool', '')))}[/] "
                f"· {self.approval.get('risk', 'low')} risk",
                id="approval-header",
            )
            if diff_text:
                yield Static(diff_text, id="approval-diff")
            yield Label(str(self.approval.get("args", {}))[:400])
            with Horizontal():
                yield Button("Apply (y)", id="approve-y", variant="success")
                yield Button("Reject (n)", id="approve-n", variant="error")
                yield Button("Always (a)", id="approve-a", variant="warning")

    @on(Button.Pressed, "#approve-y")
    def _yes(self) -> None:
        self.dismiss((True, False))

    @on(Button.Pressed, "#approve-n")
    def _no(self) -> None:
        self.dismiss((False, False))

    @on(Button.Pressed, "#approve-a")
    def _always(self) -> None:
        self.dismiss((True, True))


class ListPickerScreen(ModalScreen[Optional[str]]):
    """Simple list picker for help/model/persona menus."""

    DEFAULT_CSS = f"""
    ListPickerScreen {{
        align: center middle;
    }}
    ListPickerScreen #picker-box {{
        width: 80%;
        max-width: 80;
        height: auto;
        max-height: 80%;
        border: round {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    ListPickerScreen Label {{
        color: {Tokens.TEXT_DIM};
    }}
    ListPickerScreen VerticalScroll {{
        height: auto;
        max-height: 24;
    }}
    ListPickerScreen Button {{
        width: 100%;
        margin: 0;
    }}
    """

    def __init__(self, title: str, items: List[tuple[str, str]]) -> None:
        super().__init__()
        self._pick_title = title
        self.items = items

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Label(self._pick_title)
            with VerticalScroll():
                for i, (value, desc) in enumerate(self.items[:60]):
                    label = f"{value} — {desc}" if desc else value
                    yield Button(label, id=f"pick-{i}", variant="default")
            yield Button("Cancel", id="pick-cancel", variant="warning")

    @on(Button.Pressed, "#pick-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _pick(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "pick-cancel" or not bid.startswith("pick-"):
            return
        try:
            idx = int(bid.split("-", 1)[1])
        except (ValueError, IndexError):
            return
        if 0 <= idx < len(self.items):
            self.dismiss(self.items[idx][0])


class SearchScreen(ModalScreen[None]):
    DEFAULT_CSS = f"""
    SearchScreen {{
        align: center middle;
    }}
    SearchScreen #search-box {{
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    SearchScreen Label {{
        color: {Tokens.TEXT_DIM};
    }}
    SearchScreen Input {{
        margin-bottom: 1;
    }}
    SearchScreen VerticalScroll {{
        height: auto;
        max-height: 24;
    }}
    """

    def __init__(self, timeline: List[Dict[str, Any]], query: str = "") -> None:
        super().__init__()
        self.timeline = timeline
        self.query = query.lower()

    def compose(self) -> ComposeResult:
        with Container(id="search-box"):
            yield Label("Search Timeline:")
            yield Input(value=self.query, placeholder="Type to search...", id="search-input")
            with VerticalScroll():
                yield Static(self._build_matches(self.query), id="search-results")
            yield Button("Close", id="search-close")

    def _build_matches(self, query: str) -> str:
        matches = []
        q = query.lower()
        for i, it in enumerate(self.timeline):
            blob = ""
            if it.get("kind") == "user":
                blob = it.get("text", "")
            elif it.get("kind") == "assistant":
                blob = it.get("content", "")
            if q and q in blob.lower():
                matches.append(f"#{i}: {blob[:80]}…")
        return "\n".join(matches) if matches else ("(no matches)" if q else "(type to search)")

    @on(Input.Changed, "#search-input")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self.query = event.value
        self.query_one("#search-results", Static).update(self._build_matches(self.query))

    @on(Button.Pressed, "#search-close")
    def _close(self) -> None:
        self.dismiss(None)


class CoderAIApp(App[None]):
    """CoderAI Textual chat — Cockpit layout."""

    TITLE = "CoderAI"
    CSS = f"""
    Screen {{
        layout: vertical;
        background: {Tokens.BG};
        color: {Tokens.TEXT};
    }}
    #main {{
        height: 1fr;
        layout: horizontal;
    }}
    #rail {{
        width: 4;
        height: 1fr;
        background: {Tokens.BG_SUNK};
        border-right: solid {Tokens.LINE};
        padding: 1 0;
    }}
    #rail-content {{
        height: 1fr;
        background: {Tokens.BG_SUNK};
        content-align: center top;
        text-align: center;
        color: {Tokens.TEXT_DIM};
    }}
    #center {{
        width: 1fr;
        height: 1fr;
        layout: vertical;
        background: {Tokens.BG};
    }}
    #session-header {{
        height: 2;
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
    #composer-box {{
        height: auto;
        margin: 1 2;
        background: {Tokens.BG_RAISED};
        border: round {Tokens.LINE};
        padding: 0 1;
    }}
    #prompt-area {{
        height: auto;
        min-height: 2;
        max-height: 8;
        background: {Tokens.BG_RAISED};
        color: {Tokens.TEXT};
        border: none;
    }}
    #composer-footer {{
        height: 1;
        padding: 0 1;
        color: {Tokens.TEXT_MUTED};
        background: {Tokens.BG_RAISED};
        border-top: solid {Tokens.LINE_SOFT};
    }}
    #right-pane {{
        width: 44;
        height: 1fr;
        background: {Tokens.BG_SUNK};
        border-left: solid {Tokens.LINE};
        layout: vertical;
    }}
    #fleet-scroll {{
        height: 1fr;
        background: {Tokens.BG_SUNK};
        padding: 1 2;
        scrollbar-background: {Tokens.BG_SUNK};
        scrollbar-color: {Tokens.LINE};
    }}
    #context-pane {{
        height: auto;
        max-height: 12;
        padding: 1 2;
        background: {Tokens.BG_SUNK};
        border-top: solid {Tokens.LINE};
        color: {Tokens.TEXT_DIM};
    }}
    #cost-pane {{
        height: auto;
        padding: 1 2;
        background: {Tokens.BG_SUNK};
        border-top: solid {Tokens.LINE};
        color: {Tokens.TEXT_DIM};
    }}
    Footer {{
        background: {Tokens.BG_RAISED};
        color: {Tokens.TEXT_DIM};
    }}
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True),
        Binding("ctrl+c", "ctrl_c", "Exit", show=False),
        Binding("ctrl+shift+c, super+c", "copy_selection", "Copy", show=True),
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
        self.agent = None
        self.controller = None
        self._exit_armed_at: Optional[float] = None
        self._search_filter = ""
        self._stream_timer: Optional[float] = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="rail"):
                yield Static(self._rail_markup(), id="rail-content", markup=True)
            with Vertical(id="center"):
                yield Static("", id="session-header", markup=True)
                yield RichLog(id="timeline", highlight=True, markup=True, wrap=True)
                with Vertical(id="composer-box"):
                    yield PromptArea(id="prompt-area")
                    yield Static("", id="composer-footer", markup=True)
            with Vertical(id="right-pane"):
                with VerticalScroll(id="fleet-scroll"):
                    yield Static("", id="fleet-content", markup=True)
                yield Static("", id="context-pane", markup=True)
                yield Static("", id="cost-pane", markup=True)
        yield Footer()

    @staticmethod
    def _rail_markup() -> str:
        rows = [
            f"[{Tokens.AGENT}]{Glyphs.REASONING}[/]",
            f"[{Tokens.TEXT_DIM}]⊙[/]",
            f"[{Tokens.TEXT_DIM}]⌘[/]",
            f"[{Tokens.WARN}]{Glyphs.PINNED}[/]",
            f"[{Tokens.TEXT_DIM}]≡[/]",
        ]
        return "\n\n".join(rows)

    def on_mount(self) -> None:
        self.reducer.on_change = self._schedule_refresh
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.show_line_numbers = False
        prompt.placeholder = "Message CoderAI…   / commands   @ pin file   Enter to send"
        prompt.focus()
        footer = self.query_one("#composer-footer", Static)
        footer.update(self._composer_footer_markup())
        self._refresh_ui()
        # Run the agent loop on its own thread (with its own asyncio loop)
        # so blocking provider work doesn't freeze the UI. The agent's loop
        # is captured in IPCServer.start() so command dispatches from this
        # thread can hop over via call_soon_threadsafe.
        self.run_worker(
            self._run_agent,
            exclusive=True,
            thread=True,
            name="agent-loop",
        )
        self.set_interval(STREAM_TICK_S, self._stream_tick)

    def _schedule_refresh(self) -> None:
        self.post_message(AgentEventMsg("__refresh__", {}))

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        if msg.event == "__refresh__":
            self._refresh_ui()
            return
        self.reducer.handle(msg.event, msg.data)
        if msg.event == "tool" and msg.data.get("phase") == "awaiting_approval":
            self.run_worker(self._maybe_show_approval())
        if msg.event == "goodbye":
            self._refresh_ui()

    def _stream_tick(self) -> None:
        self.reducer._maybe_flush_stream()
        if self.reducer._stream_flush_at is None and (
            self.reducer._stream_pending_content or self.reducer._stream_pending_reasoning
        ):
            self.reducer._flush_stream_buffers()

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
            self._emit_bridge("goodbye", {"reason": "loop_crashed"})

    _TOAST_STYLES = {
        "info": Tokens.INFO,
        "success": Tokens.AGENT,
        "warning": Tokens.WARN,
        "error": Tokens.DANGER,
    }

    def _refresh_ui(self) -> None:
        try:
            log = self.query_one("#timeline", RichLog)
        except Exception:
            return
        log.clear()
        s = self.reducer.session
        verbose = s.verbose
        for it in self.reducer.timeline:
            kind = it.get("kind")
            if kind == "user":
                self._write_user(log, it)
            elif kind == "assistant":
                self._write_assistant(log, it, verbose)
            elif kind == "tool":
                self._write_tool(log, it)
            elif kind == "diff":
                self._write_diff(log, it, verbose)
            elif kind == "error":
                self._write_error(log, it)
            elif kind == "toast":
                self._write_toast(log, it)
            elif kind == "separator":
                log.write(Text(f"— {it.get('message')} —", style=Styles.TEXT_DIM))
            elif kind == "approval":
                self._write_approval(log, it)
        self._render_session_header(s)
        self._render_fleet(s)
        self._render_context(s)
        self._render_cost(s)
        try:
            footer = self.query_one("#composer-footer", Static)
            footer.update(self._composer_footer_markup())
        except Exception:
            pass

    # ── timeline row writers ───────────────────────────────────────────

    def _write_user(self, log: RichLog, it: Dict[str, Any]) -> None:
        header = Text()
        header.append(f"{Glyphs.USER} ", style=Styles.USER_GLYPH)
        header.append("you", style=Styles.USER)
        log.write(header)
        body = Text("  " + (it.get("text", "") or ""), style=Styles.TEXT)
        log.write(body)
        log.write("")

    def _write_assistant(self, log: RichLog, it: Dict[str, Any], verbose: bool) -> None:
        reasoning = (it.get("reasoning") or "").strip()
        if verbose and reasoning:
            head = Text()
            head.append(f"{Glyphs.REASONING} ", style=Styles.REASONING_GLYPH)
            head.append("reasoning", style=Styles.REASONING_LABEL)
            log.write(head)
            log.write(Text("  " + reasoning, style=Styles.REASONING))
            log.write("")
        head = Text()
        head.append(f"{Glyphs.ASSISTANT} ", style=Styles.ASSISTANT_GLYPH)
        head.append("assistant", style=Styles.ASSISTANT)
        log.write(head)
        content = it.get("content", "")
        if content:
            log.write(Text("  " + content, style=Styles.TEXT))
        if it.get("streaming"):
            log.write(Text("  ▌", style=f"blink {Tokens.AGENT}"))
        log.write("")

    def _write_tool(self, log: RichLog, it: Dict[str, Any]) -> None:
        ok = it.get("ok")
        if ok is True:
            glyph, glyph_style = Glyphs.TOOL_OK, Styles.TOOL_OK
        elif ok is False:
            glyph, glyph_style = Glyphs.ERROR, Styles.TOOL_ERR
        else:
            glyph, glyph_style = Glyphs.TOOL_RUN, Styles.TOOL_RUN

        name = str(it.get("name") or "")
        args = it.get("args") or {}
        # Show a compact summary of the arguments (first 60 chars).
        if isinstance(args, dict):
            argbits = []
            for k in ("path", "file_path", "command", "query", "url", "pattern", "target"):
                if k in args:
                    argbits.append(str(args[k]))
                    break
            args_str = argbits[0] if argbits else ""
        else:
            args_str = str(args)
        args_str = args_str.replace("\n", " ")[:60]

        preview = str(it.get("preview") or "")[:60]

        row = Text()
        row.append("  ")
        row.append(f"{glyph} ", style=glyph_style)
        row.append(f"{name:<14} ", style=Styles.TOOL_NAME)
        if args_str:
            row.append(f"{args_str}", style=Styles.TOOL_ARGS)
        if preview:
            row.append(f"   {preview}", style=Styles.TOOL_PREVIEW)
        log.write(row)
        if it.get("error"):
            log.write(Text(f"    → {it['error']}", style=Tokens.DANGER))

    def _write_diff(self, log: RichLog, it: Dict[str, Any], verbose: bool) -> None:
        head = Text()
        head.append(f"{Glyphs.TOOL_OK} ", style=Styles.TOOL_OK)
        head.append("diff", style=Styles.TOOL_NAME)
        head.append(f"  {it.get('path', '')}", style=Styles.TOOL_ARGS)
        log.write(head)
        body = it.get("diff", "")
        rendered = format_diff_compact(body) if not verbose else body[:8000]
        log.write(Text(rendered, style=Styles.TEXT_DIM))

    def _write_error(self, log: RichLog, it: Dict[str, Any]) -> None:
        head = Text()
        head.append(f"{Glyphs.ERROR} ", style=Tokens.DANGER)
        head.append("error", style=Styles.DANGER)
        log.write(head)
        log.write(Text("  " + str(it.get("message", "")), style=Styles.TEXT))
        if it.get("hint"):
            log.write(Text("  " + str(it["hint"]), style=Styles.TEXT_DIM))

    def _write_toast(self, log: RichLog, it: Dict[str, Any]) -> None:
        level = it.get("level", "info")
        color = self._TOAST_STYLES.get(level, Tokens.TEXT_DIM)
        log.write(Text("· " + str(it.get("message", "")), style=color))

    def _write_approval(self, log: RichLog, it: Dict[str, Any]) -> None:
        decided = it.get("decided", "pending")
        head = Text()
        head.append(f"{Glyphs.APPROVAL} ", style=Styles.APPROVAL_GLYPH)
        head.append("approval required", style=Styles.APPROVAL_LABEL)
        head.append(f"  {it.get('tool', '')}", style=Styles.TEXT)
        head.append(f"  [{decided}]", style=Styles.TEXT_MUTED)
        log.write(head)

    # ── session header + right pane ────────────────────────────────────

    def _render_session_header(self, s: SessionState) -> None:
        try:
            header = self.query_one("#session-header", Static)
        except Exception:
            return
        # Left: session dot + model · provider · context fill
        status_color = Tokens.AGENT if s.streaming or s.thinking else Tokens.TEXT_DIM
        ctx_used = f"{s.ctx_used:,}" if s.ctx_used else "0"
        ctx_lim = f"{s.ctx_limit // 1000}k" if s.ctx_limit else "?"
        model_label = s.model or "…"
        mode_tag = ""
        if s.thinking:
            mode_tag = f"  [{Tokens.THOUGHT}]thinking…[/]"
        elif s.streaming:
            mode_tag = f"  [{Tokens.AGENT}]streaming…[/]"

        flags = []
        if s.auto_approve:
            flags.append(f"[{Tokens.WARN}]{Glyphs.DOT} yolo[/]")
        if s.verbose:
            flags.append(f"[{Tokens.TEXT_DIM}]verbose[/]")
        flag_str = "  ".join(flags)

        left = (
            f"[{status_color}]{Glyphs.DOT}[/] "
            f"[{Tokens.TEXT}]{model_label}[/]  "
            f"[{Tokens.TEXT_MUTED}]{s.provider or ''}[/]  "
            f"[{Tokens.TEXT_DIM}]ctx[/] [{Tokens.TEXT}]{ctx_used}[/] / {ctx_lim}"
            f"{mode_tag}"
        )
        right = f"[{Tokens.TEXT_DIM}]${s.cost_usd:.4f}[/]"
        if flag_str:
            right = f"{flag_str}   {right}"

        # Compose as two lines so the header looks like a chip strip.
        # First line: model/ctx/cost. Second line: keyboard hints.
        hints = (
            f"[{Tokens.TEXT_MUTED}]⌘K commands · ⎋ cancel · @ pin · / slash[/]"
        )
        header.update(f"{left}  {right}\n{hints}")

    def _render_fleet(self, s: SessionState) -> None:
        try:
            fleet = self.query_one("#fleet-content", Static)
        except Exception:
            return
        active_count = sum(
            1 for a in s.agents.values()
            if a.status not in ("done", "error", "cancelled")
        )
        title = (
            f"[{Styles.SECTION}]AGENT FLEET[/]  "
            f"[{Tokens.TEXT_MUTED}]· {active_count} active[/]\n"
        )
        if not s.agents:
            fleet.update(title + f"[{Tokens.TEXT_MUTED}](no agents yet)[/]")
            return

        lines = [title]
        # Sort: main agent first, then by status priority, then by name.
        def sort_key(info: AgentInfo) -> tuple:
            is_root = 0 if info.parent_id is None else 1
            prio = {"streaming": 0, "thinking": 1, "idle": 2, "done": 3, "error": 4, "cancelled": 5}
            return (is_root, prio.get(info.status, 6), info.name)

        for info in sorted(s.agents.values(), key=sort_key):
            lines.append(self._format_agent_card(info))
            lines.append("")
        fleet.update("\n".join(lines).rstrip())

    def _format_agent_card(self, info: AgentInfo) -> str:
        status = info.status
        if status == "streaming":
            color = Tokens.AGENT
        elif status == "thinking":
            color = Tokens.THOUGHT
        elif status in ("done", "cancelled"):
            color = Tokens.TEXT_MUTED
        elif status == "error":
            color = Tokens.DANGER
        else:
            color = Tokens.WARN

        name = info.name or info.id
        role = info.role or info.model or ""
        task = (info.task or "")[:34]
        tool = (info.tool or "—")[:24]
        cost = f"${info.cost_usd:.4f}"
        ctx_k = f"{info.ctx_used // 1000}k" if info.ctx_used else "0"

        line1 = (
            f"[{color}]{Glyphs.DOT}[/] [{Tokens.TEXT}]{name}[/]  "
            f"[{Tokens.TEXT_MUTED}]{role}[/]"
        )
        status_label = f"[{color}]{status.upper()}[/]"
        line2 = (
            f"  [{Tokens.TEXT_DIM}]{task}[/]"
            if task else ""
        )
        parent_line = ""
        if info.parent_id:
            parent_line = f"  [{Tokens.TEXT_MUTED}]{Glyphs.PARENT} parent: {info.parent_id}[/]"
        line3 = (
            f"  [{Tokens.TEXT_MUTED}]{tool}[/]  "
            f"[{Tokens.TEXT_MUTED}]{cost}  {ctx_k}[/]  "
            f"{status_label}"
        )
        parts = [line1]
        if parent_line:
            parts.append(parent_line)
        if line2:
            parts.append(line2)
        parts.append(line3)
        return "\n".join(parts)

    def _render_context(self, s: SessionState) -> None:
        try:
            pane = self.query_one("#context-pane", Static)
        except Exception:
            return
        files = s.context_files or []
        title = (
            f"[{Styles.SECTION}]PINNED CONTEXT[/]  "
            f"[{Tokens.TEXT_MUTED}]· {len(files)} files[/]"
        )
        if not files:
            pane.update(f"{title}\n[{Tokens.TEXT_MUTED}](nothing pinned · /pin to add)[/]")
            return
        rows = [title]
        for f in files[:6]:
            path = str(f.get("path", ""))[:30]
            size_b = int(f.get("size") or 0)
            size_str = f"{size_b / 1024:.1f} kB" if size_b else ""
            rows.append(
                f"[{Tokens.WARN}]{Glyphs.PINNED}[/] [{Tokens.TEXT}]{path}[/]  "
                f"[{Tokens.TEXT_MUTED}]{size_str}[/]"
            )
        if len(files) > 6:
            rows.append(f"[{Tokens.TEXT_MUTED}]… and {len(files) - 6} more[/]")
        pane.update("\n".join(rows))

    def _render_cost(self, s: SessionState) -> None:
        try:
            pane = self.query_one("#cost-pane", Static)
        except Exception:
            return
        cost = s.cost_usd
        budget = s.budget_usd or 0.0
        if budget > 0:
            ratio = min(1.0, cost / budget)
            filled = max(0, min(30, int(ratio * 30)))
            color = Tokens.AGENT if ratio < 0.7 else Tokens.WARN if ratio < 0.95 else Tokens.DANGER
            bar = f"[{color}]" + ("█" * filled) + f"[/][{Tokens.LINE}]" + ("─" * (30 - filled)) + "[/]"
            tail = f"[{Tokens.TEXT}]${cost:.4f}[/] [{Tokens.TEXT_DIM}]/ ${budget:.2f}[/]"
        else:
            bar = f"[{Tokens.LINE}]" + ("─" * 30) + "[/]"
            tail = f"[{Tokens.TEXT}]${cost:.4f}[/] [{Tokens.TEXT_MUTED}](no budget set)[/]"

        prompt_tok = s.prompt_tokens
        comp_tok = s.completion_tokens
        title = f"[{Styles.SECTION}]COST[/]   {tail}"
        stats = (
            f"[{Tokens.TEXT_DIM}]prompt[/]      "
            f"[{Tokens.TEXT}]{prompt_tok / 1000:.1f}k tok[/]\n"
            f"[{Tokens.TEXT_DIM}]completion[/]  "
            f"[{Tokens.TEXT}]{comp_tok / 1000:.1f}k tok[/]"
        )
        pane.update(f"{title}\n{bar}\n{stats}")

    def _composer_footer_markup(self) -> str:
        s = self.reducer.session
        reasoning = s.reasoning or "none"
        return (
            f"[{Tokens.TEXT_MUTED}]↵ send · ⇧↵ newline · / commands · @ pin[/]   "
            f"[{Tokens.TEXT_DIM}]reasoning:[/] [{Tokens.THOUGHT}]{reasoning}[/]"
        )

    async def _maybe_show_approval(self) -> None:
        pending = self.reducer.pending_approval()
        if not pending:
            return
        result = await self.push_screen_wait(ApprovalScreen(pending))
        if result is None:
            return
        approve, always = result
        if approve and always and not self.reducer.session.auto_approve:
            self.controller.enqueue_command("toggle_auto_approve")
        self.controller.enqueue_command(
            "tool_approval_resp",
            toolId=pending["id"],
            approve=approve,
        )
        pending["decided"] = "approved" if approve else "denied"
        self._refresh_ui()

    def action_cancel_turn(self) -> None:
        if self.controller:
            self.controller.enqueue_command("cancel")

    def action_ctrl_c(self) -> None:
        now = time.time()
        if self._exit_armed_at and now - self._exit_armed_at < 5:
            if self.controller:
                self.controller.enqueue_command("exit")
            self.exit()
        else:
            self._exit_armed_at = now
            self.notify("Press Ctrl+C again within 5s to exit")

    def action_copy_selection(self) -> None:
        log = self.query_one("#timeline", RichLog)
        selection = log.text_selection
        if selection:
            self.copy_to_clipboard(selection)
            self.notify("Selection copied to clipboard")
        else:
            self.notify("No text selected — use mouse to select text first")

    @on(PromptArea.Submitted)
    def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.text = ""
        text = event.text.strip()
        if not text:
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        if not self.controller:
            return
        self.reducer._push({"kind": "user", "id": self.reducer.next_id(), "text": text})
        if text.startswith("/"):
            handled = handle_slash_command(
                text,
                self.controller,
                self.reducer,
                show_help=self._show_help,
                show_model_menu=self._show_model_menu,
                show_reasoning_menu=self._show_reasoning_menu,
                show_persona_menu=self._show_persona_menu,
                show_skills_menu=self._show_skills_menu,
                show_search=self._show_search,
                show_context=self._show_context,
                clear_context=self._clear_context,
                toggle_verbose=self._toggle_verbose,
                reveal_reasoning=self._reveal_reasoning,
                confirm_exit=self._confirm_exit,
                set_search_filter=lambda q: setattr(self, "_search_filter", q),
            )
            if handled:
                return
        self.controller.enqueue_command("send_message", text=text)

    def _show_help(self) -> None:
        self.run_worker(self._pick_list("Commands", HELP_MENU_ENTRIES), exclusive=True)

    def _show_model_menu(self) -> None:
        models = self.reducer.session.available_models or self._fallback_models()
        items: List[tuple[str, str]] = []
        for provider, names in models.items():
            for n in names:
                items.append((n, provider))
        if not items:
            self.notify("No models available")
            return
        self.run_worker(self._pick_list("Models", items), exclusive=True)

    def _show_reasoning_menu(self) -> None:
        items = [(e, "") for e in ("high", "medium", "low", "none")]
        self.run_worker(self._pick_list("Reasoning", items), exclusive=True)

    def _show_persona_menu(self) -> None:
        personas = self.reducer.session.available_personas
        if not personas:
            personas = self._fallback_personas()
        items = [(p, "") for p in personas]
        if not items:
            self.notify("No personas defined in .coderAI/agents/")
            return
        self.run_worker(self._pick_list("Personas", items), exclusive=True)

    def _show_skills_menu(self) -> None:
        skills = self.reducer.session.available_skills
        if not skills:
            skills = self._fallback_skills()
        items = [(s.get("name", ""), s.get("description", "")) for s in skills]
        if not items:
            self.notify("No skills defined in .coderAI/skills/")
            return
        self.run_worker(self._pick_list("Skills", items), exclusive=True)

    def _fallback_models(self) -> Dict[str, List[str]]:
        try:
            from ..llm.anthropic import MODEL_ALIASES
            from ..llm.deepseek import DeepSeekProvider
            from ..llm.groq import GroqProvider
            from ..llm.openai import OpenAIProvider
            return {
                "Anthropic": sorted(MODEL_ALIASES.keys()),
                "OpenAI": sorted(OpenAIProvider.SUPPORTED_MODELS.keys()),
                "DeepSeek": sorted(DeepSeekProvider.SUPPORTED_MODELS.keys()),
                "Groq": sorted(GroqProvider.SUPPORTED_MODELS.keys()),
                "Local": ["lmstudio", "ollama"],
            }
        except Exception:
            return {}

    def _fallback_personas(self) -> List[str]:
        try:
            from ..agents import get_available_personas
            pr = getattr(self.agent.config, "project_root", ".") if self.agent else "."
            return sorted(get_available_personas(pr))
        except Exception:
            return []

    def _fallback_skills(self) -> List[Dict[str, str]]:
        try:
            from ..tools.skills import get_available_skills
            pr = getattr(self.agent.config, "project_root", ".") if self.agent else "."
            return list(get_available_skills(pr))
        except Exception:
            return []

    async def _pick_list(self, title: str, items: List[tuple[str, str]]) -> None:
        result = await self.push_screen_wait(ListPickerScreen(title, items))
        if result and self.controller:
            if title == "Models":
                self.controller.enqueue_command("set_model", model=result)
            elif title == "Reasoning":
                self.controller.enqueue_command("set_reasoning", effort=result)
            elif title == "Personas":
                self.controller.enqueue_command("send_message", text=f"/persona {result}")
            elif title == "Skills":
                self.controller.enqueue_command("send_message", text=f"/skills {result}")
            elif title == "Commands":
                self.query_one("#prompt-area", TextArea).text = result + " "

    def _show_search(self) -> None:
        self.push_screen(SearchScreen(self.reducer.timeline, self._search_filter))

    def _show_context(self) -> None:
        files = self.reducer.session.context_files or []
        msg = "\n".join(f"  {f.get('path')} ({f.get('size')} B)" for f in files) or "(none)"
        self.reducer._push(
            {
                "kind": "toast",
                "id": self.reducer.next_id(),
                "level": "info",
                "message": f"Pinned context:\n{msg}",
            }
        )

    def _clear_context(self) -> None:
        if self.controller:
            self.controller.enqueue_command("clear_context")
            self.reducer.timeline.clear()
            self._refresh_ui()

    def _toggle_verbose(self) -> None:
        self.reducer.session.verbose = not self.reducer.session.verbose
        level = "verbose" if self.reducer.session.verbose else "normal"
        if self.controller:
            self.controller.enqueue_command("set_verbosity", level=level)
        self.notify(f"Verbose {'on' if self.reducer.session.verbose else 'off'}")

    def _reveal_reasoning(self) -> None:
        for it in reversed(self.reducer.timeline):
            if it.get("kind") == "assistant" and (it.get("reasoning") or "").strip():
                self.reducer._push(
                    {
                        "kind": "toast",
                        "id": self.reducer.next_id(),
                        "level": "info",
                        "message": it["reasoning"][:4000],
                    }
                )
                return
        self.notify("No reasoning to reveal")

    def _confirm_exit(self) -> bool:
        now = time.time()
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
    app = CoderAIApp(
        model=model,
        resume=resume,
        continue_=continue_,
        auto_approve=auto_approve,
        persona=persona,
    )
    app.run()

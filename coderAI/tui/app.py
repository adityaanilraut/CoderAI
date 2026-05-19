"""Textual chat application."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, RichLog, Static, TextArea

from .diff_render import format_diff_compact
from .help_menu import HELP_MENU_ENTRIES
from .listeners import EventReducer
from .session_setup import create_agent_session
from .slash import handle_slash_command
from .state import SessionState

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

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
    }
    ApprovalScreen #approval-box {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    ApprovalScreen Horizontal {
        height: auto;
        align-horizontal: center;
        margin-top: 1;
    }
    ApprovalScreen Button {
        margin: 0 1;
    }
    """

    def __init__(self, approval: Dict[str, Any]) -> None:
        super().__init__()
        self.approval = approval

    def compose(self) -> ComposeResult:
        diff = self.approval.get("diff")
        diff_text = format_diff_compact(diff) if diff else ""
        with Container(id="approval-box"):
            yield Label(
                f"Approve [bold]{escape(str(self.approval.get('tool', '')))}[/] "
                f"({self.approval.get('risk', 'low')} risk)?",
            )
            if diff_text:
                yield Static(diff_text, id="approval-diff")
            yield Label(str(self.approval.get("args", {}))[:400])
            with Horizontal():
                yield Button("Yes (y)", id="approve-y", variant="success")
                yield Button("No (n)", id="approve-n", variant="error")
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

    DEFAULT_CSS = """
    ListPickerScreen {
        align: center middle;
    }
    ListPickerScreen #picker-box {
        width: 80%;
        max-width: 80;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    ListPickerScreen VerticalScroll {
        height: auto;
        max-height: 24;
    }
    ListPickerScreen Button {
        width: 100%;
        margin: 0;
    }
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
    DEFAULT_CSS = """
    SearchScreen {
        align: center middle;
    }
    SearchScreen #search-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    SearchScreen VerticalScroll {
        height: auto;
        max-height: 24;
    }
    """

    def __init__(self, timeline: List[Dict[str, Any]], query: str = "") -> None:
        super().__init__()
        self.timeline = timeline
        self.query = query.lower()

    def compose(self) -> ComposeResult:
        matches = []
        for i, it in enumerate(self.timeline):
            blob = ""
            if it.get("kind") == "user":
                blob = it.get("text", "")
            elif it.get("kind") == "assistant":
                blob = it.get("content", "")
            if self.query and self.query in blob.lower():
                matches.append(f"#{i}: {blob[:80]}…")
        body = "\n".join(matches) if matches else "(no matches)"
        with Container(id="search-box"):
            yield Label(f"Search: {self.query or '(empty)'}")
            with VerticalScroll():
                yield Static(body)
            yield Button("Close", id="search-close")

    @on(Button.Pressed, "#search-close")
    def _close(self) -> None:
        self.dismiss(None)


class CoderAIApp(App[None]):
    """CoderAI Textual chat."""

    TITLE = "CoderAI"
    CSS = """
    Screen {
        layout: vertical;
    }
    #timeline {
        height: 1fr;
        border: solid $primary;
    }
    #agent-panel {
        height: auto;
        max-height: 6;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #prompt-area {
        height: auto;
        min-height: 3;
        max-height: 8;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel_turn", "Cancel", show=True),
        Binding("ctrl+c", "ctrl_c", "Exit", show=False),
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
        yield RichLog(id="timeline", highlight=True, markup=True, wrap=True)
        yield Static("", id="agent-panel")
        yield Static("", id="status-bar")
        yield PromptArea(id="prompt-area")
        yield Footer()

    def on_mount(self) -> None:
        self.reducer.on_change = self._schedule_refresh
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.show_line_numbers = False
        prompt.placeholder = "Message CoderAI… (/help · Enter to send · Shift+Enter for newline)"
        prompt.focus()
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
            await self._maybe_show_approval()
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
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red",
    }

    def _refresh_ui(self) -> None:
        log = self.query_one("#timeline", RichLog)
        log.clear()
        s = self.reducer.session
        verbose = s.verbose
        for it in self.reducer.timeline:
            kind = it.get("kind")
            if kind == "user":
                log.write(Text("You", style="bold cyan"))
                log.write(Text(it.get("text", "")))
            elif kind == "assistant":
                log.write(Text("Assistant", style="bold green"))
                if verbose and (it.get("reasoning") or "").strip():
                    log.write(Text(it["reasoning"], style="dim italic"))
                content = it.get("content", "")
                if content:
                    log.write(Text(content))
                if it.get("streaming"):
                    log.write(Text("▌", style="blink"))
            elif kind == "tool":
                ok = it.get("ok")
                mark = "✓" if ok else "✗" if ok is False else "…"
                log.write(
                    Text(f"⚙ {it.get('name')} {mark}", style="yellow"),
                )
                if it.get("preview"):
                    log.write(Text(str(it["preview"])[:500], style="dim"))
                if it.get("error"):
                    log.write(Text(f"  → {it['error']}", style="red"))
            elif kind == "diff":
                log.write(Text(f"diff {it.get('path')}", style="magenta"))
                body = it.get("diff", "")
                rendered = format_diff_compact(body) if not verbose else body[:8000]
                log.write(Text(rendered))
            elif kind == "error":
                log.write(Text(f"Error: {it.get('message')}", style="bold red"))
                if it.get("hint"):
                    log.write(Text(str(it["hint"]), style="dim"))
            elif kind == "toast":
                level = it.get("level", "info")
                style = self._TOAST_STYLES.get(level, "")
                log.write(Text(it.get("message", ""), style=style))
            elif kind == "separator":
                log.write(Text(f"— {it.get('message')} —", style="dim"))
            elif kind == "approval":
                st = it.get("decided", "pending")
                log.write(Text(f"Approval {it.get('tool')} [{st}]", style="bold yellow"))
        self._render_status(s)
        self._render_agents(s)

    def _render_status(self, s: SessionState) -> None:
        bar = self.query_one("#status-bar", Static)
        ctx = f"{s.ctx_used:,}/{s.ctx_limit:,}" if s.ctx_limit else "?"
        modes = []
        if s.auto_approve:
            modes.append("YOLO")
        if s.verbose:
            modes.append("verbose")
        if s.thinking:
            modes.append("thinking…")
        elif s.streaming:
            modes.append("streaming…")
        mode = " ".join(modes)
        bar.update(
            f"◆ {s.model or '…'}  {s.provider}  ctx {ctx}  ${s.cost_usd:.4f}  {mode}",
        )

    def _render_agents(self, s: SessionState) -> None:
        panel = self.query_one("#agent-panel", Static)
        lines = []
        for info in s.agents.values():
            if info.status in ("done", "error", "cancelled"):
                continue
            task = (info.task or "")[:40]
            lines.append(f"• {info.name} [{info.status}] {task}")
        panel.update("\n".join(lines) if lines else "")

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

    @on(PromptArea.Submitted)
    def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.text.strip()
        if not text:
            return
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.text = ""
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

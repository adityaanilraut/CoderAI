"""Textual chat application."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from rich.markup import escape
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static, TextArea

from coderAI.tui.diff_render import format_diff_compact
from coderAI.tui.help_menu import HELP_MENU_ENTRIES
from coderAI.tui.listeners import EventReducer, RefreshMode
from coderAI.tui.session_setup import create_agent_session
from coderAI.tui.slash import handle_slash_command
from coderAI.tui.state import AgentInfo, SessionState
from coderAI.tui.theme import Glyphs, Styles, Tokens
from coderAI.tui.timeline_render import build_stream_tail_markup, write_timeline_item

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
    """Enhanced tool approval dialog with risk breakdown."""

    DEFAULT_CSS = f"""
    ApprovalScreen {{
        align: center middle;
    }}
    ApprovalScreen #approval-box {{
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 85%;
        border: round {Tokens.WARN};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    ApprovalScreen #approval-header {{
        color: {Tokens.WARN};
        text-style: bold;
        margin-bottom: 1;
    }}
    ApprovalScreen #approval-meta {{
        color: {Tokens.TEXT_DIM};
        margin-bottom: 1;
    }}
    ApprovalScreen #approval-command {{
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT};
        padding: 1;
        margin: 1 0;
        border: solid {Tokens.LINE_SOFT};
    }}
    ApprovalScreen #approval-diff {{
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT};
        padding: 1;
        margin: 1 0;
    }}
    ApprovalScreen #approval-risk {{
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

    _RISK_FACTORS: Dict[str, List[tuple[str, bool]]] = {
        "run_command": [
            ("No network access by default", True),
            ("Could spawn child processes", False),
            ("Writes to filesystem", False),
        ],
        "run_background": [
            ("No network access by default", True),
            ("Long-running process", False),
            ("Could consume resources", False),
        ],
        "write_file": [
            ("Runs in project sandbox", True),
            ("Writes to filesystem", False),
            ("Could overwrite existing files", False),
        ],
        "search_replace": [
            ("Runs in project sandbox", True),
            ("Modifies files in place", False),
            ("Could leave dirty working tree", False),
        ],
        "apply_diff": [
            ("Runs in project sandbox", True),
            ("Modifies files in place", False),
            ("Could leave dirty working tree", False),
        ],
        "delete_file": [
            ("Runs in project sandbox", True),
            ("Permanently deletes files", False),
            ("Irreversible without git", False),
        ],
        "git_commit": [
            ("Runs in project sandbox", True),
            ("Creates permanent git history", False),
            ("Could push on next sync", False),
        ],
        "git_push": [
            ("Runs in project sandbox", True),
            ("Transmits data to remote", False),
            ("Affects shared repository", False),
        ],
        "git_checkout": [
            ("Runs in project sandbox", True),
            ("Switches working tree", False),
            ("Could cause merge conflicts", False),
        ],
        "git_reset": [
            ("Runs in project sandbox", True),
            ("Destroys uncommitted work", False),
            ("Irreversible without reflog", False),
        ],
    }
    _DEFAULT_RISK_FACTORS: List[tuple[str, bool]] = [
        ("Runs in project sandbox", True),
        ("May modify project files", False),
    ]

    def __init__(self, approval: Dict[str, Any]) -> None:
        super().__init__()
        self.approval = approval
        self._started = time.time()

    def compose(self) -> ComposeResult:
        a = self.approval
        tool_name = str(a.get("tool", ""))
        risk = str(a.get("risk", "low"))
        args = a.get("args") or {}
        diff = a.get("diff")
        req_by = str(a.get("requestedBy", ""))
        parent_id = a.get("parentId")
        iteration = int(a.get("iteration") or 0)

        with Container(id="approval-box"):
            yield Label(
                f"[bold {Tokens.WARN}]▲[/] Approve [bold {Tokens.TEXT}]{escape(tool_name)}[/]"
                f" · [{Tokens.WARN}]▲ {risk.upper()}[/] risk",
                id="approval-header",
            )

            # Metadata line
            meta_parts = []
            if req_by:
                meta_parts.append(f"requested by [{Tokens.TEXT}]{escape(req_by)}[/]")
            if parent_id:
                meta_parts.append(f"sub-agent of [{Tokens.TEXT_MUTED}]{parent_id[-8:]}[/]")
            if iteration:
                meta_parts.append(f"iteration [{Tokens.TEXT_DIM}]{iteration}[/]")
            if meta_parts:
                yield Label(" · ".join(meta_parts), id="approval-meta")

            # Command preview for run_command
            if tool_name == "run_command" and args:
                cmd_str = str(args.get("command", args.get("cmd", "")))
                if cmd_str:
                    yield Static(
                        f"[{Tokens.AGENT}]$[/] [{Tokens.TEXT}]{escape(cmd_str)}[/]",
                        id="approval-command",
                    )

            # Diff
            if diff:
                diff_text = format_diff_compact(diff, max_lines=14)
                yield Static(diff_text, id="approval-diff")

            # Arguments summary
            args_text = str(args)[:400]
            if not diff:
                yield Label(args_text)

            # Risk breakdown
            risk_items = self._RISK_FACTORS.get(tool_name, self._DEFAULT_RISK_FACTORS)
            risk_lines = []
            for label, ok in risk_items[:6]:
                g, c = (Glyphs.TOOL_OK, Tokens.AGENT) if ok else (Glyphs.APPROVAL, Tokens.WARN)
                risk_lines.append(f"  [{c}]{g}[/] [{Tokens.TEXT}]{label}[/]")
            if risk_lines:
                yield Label(
                    f"[{Tokens.TEXT_MUTED}]WHY IT'S {risk.upper()} RISK[/]\n"
                    + "\n".join(risk_lines),
                    id="approval-risk",
                )

            # Buttons
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


class CommandPaletteScreen(ModalScreen[Optional[str]]):
    """Fuzzy-searchable command palette with grouped sections."""

    DEFAULT_CSS = f"""
    CommandPaletteScreen {{
        align: center middle;
    }}
    CommandPaletteScreen #palette-box {{
        width: 70%;
        max-width: 80;
        height: auto;
        max-height: 85%;
        border: round {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 0;
    }}
    CommandPaletteScreen #palette-input {{
        margin: 1 2;
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT};
        border: solid {Tokens.LINE_SOFT};
    }}
    CommandPaletteScreen VerticalScroll {{
        height: auto;
        max-height: 20;
        margin: 1 0;
        padding: 0 2;
        background: {Tokens.BG_RAISED};
    }}
    CommandPaletteScreen #palette-list {{
        height: auto;
        background: {Tokens.BG_RAISED};
    }}
    CommandPaletteScreen #palette-footer {{
        height: 1;
        padding: 0 2;
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT_MUTED};
        border-top: solid {Tokens.LINE};
    }}
    """

    def __init__(self, session: SessionState, only_section: Optional[str] = None) -> None:
        super().__init__()
        self._s = session
        self._selected_idx = 0
        self._query = ""
        self._only_section = only_section

    def compose(self) -> ComposeResult:
        with Container(id="palette-box"):
            yield Input(
                value="",
                placeholder="⌘ Type to search commands, models, personas…",
                id="palette-input",
            )
            with VerticalScroll():
                yield Static(self._build_list(""), id="palette-list", markup=True)
            yield Static(
                f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ select  ⇥ complete  ⎋ close[/]",
                id="palette-footer",
            )

    def _gather_items(self, query: str) -> List[Dict[str, Any]]:
        """Build sectioned, filtered results."""
        q = query.lower().strip()
        sections: List[Dict[str, Any]] = []
        only = self._only_section

        def add_section(title: str, items: List[Dict[str, Any]]) -> None:
            if not items:
                return
            sections.append({"title": title, "items": items})

        # Commands
        if only is None or only == "commands":
            cmds = []
            for cmd, desc in HELP_MENU_ENTRIES:
                if not q or q in cmd.lower() or q in desc.lower():
                    cmds.append({"label": cmd, "desc": desc, "action": ("cmd", cmd)})
            add_section("Commands", cmds)

        # Personas
        if only is None or only == "personas":
            personas = []
            avail = self._s.available_personas or []
            for p in avail:
                if not q or q in p.lower():
                    personas.append(
                        {
                            "label": f"/persona {p}",
                            "desc": "Switch persona",
                            "action": ("persona", p),
                        }
                    )
            add_section("Personas", personas)

        # Models
        if only is None or only == "models":
            models = []
            m = self._s.available_models or {}
            for provider, names in m.items():
                for n in names:
                    if not q or q in n.lower() or q in provider.lower():
                        models.append({"label": n, "desc": provider, "action": ("model", n)})
            add_section("Models", models)

        # Reasoning
        if only is None or only == "reasoning":
            reason = []
            for e in ("high", "medium", "low", "none"):
                if not q or q in e:
                    reason.append(
                        {
                            "label": f"/reasoning {e}",
                            "desc": "Set reasoning effort",
                            "action": ("reasoning", e),
                        }
                    )
            add_section("Reasoning", reason)

        # Skills
        if only is None or only == "skills":
            skills = []
            for s in self._s.available_skills or []:
                name = s.get("name", "")
                desc = s.get("description", "")
                if not q or q in name.lower() or q in desc.lower():
                    skills.append(
                        {"label": f"/skills {name}", "desc": desc, "action": ("skills", name)}
                    )
            add_section("Skills", skills)

        # Session
        if only is None or only == "session":
            session = []
            session_items = [
                ("/clear", "Wipe conversation & context", "clear"),
                ("/compact", "Summarize long context", "compact"),
                ("/yolo", "Toggle auto-approve", "yolo"),
                ("/verbose", "Toggle verbose mode", "verbose"),
                ("/agents", "Refresh agents panel", "agents"),
                ("/export", "Export session to markdown", "export"),
                ("/search", "Search timeline", "search"),
            ]
            for label, desc, action in session_items:
                if not q or q in label.lower() or q in desc.lower():
                    session.append({"label": label, "desc": desc, "action": ("cmd", label)})
            add_section("Session", session)

        return sections

    def _build_list(self, query: str) -> str:
        sections = self._gather_items(query)
        lines: List[str] = []
        flat_idx = 0
        for sec in sections:
            lines.append(f"[{Styles.SECTION}]{sec['title']}[/]")
            for item in sec["items"]:
                label = item["label"]
                desc = item["desc"]
                sel = ">" if flat_idx == self._selected_idx else " "
                color = Tokens.AGENT if flat_idx == self._selected_idx else Tokens.TEXT
                lines.append(
                    f"  {sel} [{color}]{escape(label)}[/]  [{Tokens.TEXT_MUTED}]{escape(desc)}[/]"
                )
                flat_idx += 1
            lines.append("")
        if flat_idx == 0:
            lines.append(f'[{Tokens.TEXT_MUTED}]  no matches for "{escape(query)}"[/]')
        if flat_idx > 0:
            self._selected_idx = min(self._selected_idx, flat_idx - 1)
        else:
            self._selected_idx = 0
        return "\n".join(lines)

    def _get_flat_items(self, query: str) -> List[Dict[str, Any]]:
        sections = self._gather_items(query)
        flat: List[Dict[str, Any]] = []
        for sec in sections:
            flat.extend(sec["items"])
        return flat

    @on(Input.Changed, "#palette-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value or ""
        self._selected_idx = 0
        self.query_one("#palette-list", Static).update(self._build_list(self._query))

    @on(events.Key)
    def _on_key(self, event: events.Key) -> None:
        key = event.key
        flat = self._get_flat_items(self._query)
        if key == "up":
            event.stop()
            event.prevent_default()
            if flat:
                self._selected_idx = (self._selected_idx - 1) % len(flat)
                self.query_one("#palette-list", Static).update(self._build_list(self._query))
        elif key == "down":
            event.stop()
            event.prevent_default()
            if flat:
                self._selected_idx = (self._selected_idx + 1) % len(flat)
                self.query_one("#palette-list", Static).update(self._build_list(self._query))
        elif key == "enter":
            event.stop()
            event.prevent_default()
            if flat and 0 <= self._selected_idx < len(flat):
                item = flat[self._selected_idx]
                action_type = item["action"][0]
                action_val = item["action"][1]
                if action_type == "cmd":
                    self.dismiss(action_val)
                elif action_type == "model":
                    self.dismiss(f"/model {action_val}")
                elif action_type == "persona":
                    self.dismiss(f"/persona {action_val}")
                elif action_type == "reasoning":
                    self.dismiss(f"/reasoning {action_val}")
                elif action_type == "skills":
                    self.dismiss(f"/skills {action_val}")
        elif key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
        elif key == "tab":
            event.stop()
            event.prevent_default()
            if flat and 0 <= self._selected_idx < len(flat):
                item = flat[self._selected_idx]
                action_type = item["action"][0]
                action_val = item["action"][1]
                if action_type == "cmd":
                    text = action_val + " "
                elif action_type == "model":
                    text = f"/model {action_val}"
                elif action_type == "persona":
                    text = f"/persona {action_val}"
                elif action_type == "reasoning":
                    text = f"/reasoning {action_val}"
                elif action_type == "skills":
                    text = f"/skills {action_val}"
                else:
                    text = action_val
                self.dismiss(text)


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
        Binding("ctrl+k", "command_palette", "Commands", show=True),
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
        self._log_rendered_idx = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="rail"):
                yield Static(self._rail_markup(), id="rail-content", markup=True)
            with Vertical(id="center"):
                yield Static("", id="session-header", markup=True)
                yield RichLog(id="timeline", highlight=True, markup=True, wrap=True)
                yield Static("", id="stream-tail", markup=True)
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
        self.reducer.on_change = self._on_reducer_change
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.show_line_numbers = False
        prompt.placeholder = "Message CoderAI…   / commands   @ pin   ⌘K palette   Enter to send"
        prompt.focus()
        footer = self.query_one("#composer-footer", Static)
        footer.update(self._composer_footer_markup())
        self._refresh_ui("full")
        # Run the agent loop on its own thread (with its own asyncio loop)
        # so blocking provider work doesn't freeze the UI. The agent's loop
        # is captured in UIBridge.start() so command dispatches from this
        # thread can hop over via call_soon_threadsafe.
        self.run_worker(
            self._run_agent,
            exclusive=True,
            thread=True,
            name="agent-loop",
        )
        self.set_interval(STREAM_TICK_S, self._stream_tick)

    def _on_reducer_change(self, mode: RefreshMode) -> None:
        self.post_message(AgentEventMsg("__refresh__", {"mode": mode}))

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        if msg.event == "__refresh__":
            self._refresh_ui(str(msg.data.get("mode", "full")))
            return
        self.reducer.handle(msg.event, msg.data)
        if msg.event == "tool" and msg.data.get("phase") == "awaiting_approval":
            self.run_worker(self._maybe_show_approval())

    def _stream_tick(self) -> None:
        flushed = self.reducer._maybe_flush_stream()
        if self.reducer._stream_flush_at is None and (
            self.reducer._stream_pending_content or self.reducer._stream_pending_reasoning
        ):
            flushed = self.reducer._flush_stream_buffers() or flushed
        if flushed:
            self.reducer._bump_refresh("stream")
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
            self._emit_bridge("goodbye", {"reason": "loop_crashed"})

    _TOAST_STYLES = {
        "info": Tokens.INFO,
        "success": Tokens.AGENT,
        "warning": Tokens.WARN,
        "error": Tokens.DANGER,
    }

    def _refresh_ui(self, mode: str = "full") -> None:
        try:
            log = self.query_one("#timeline", RichLog)
        except Exception:
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

        if mode == "stream":
            for it in reversed(timeline):
                if it.get("kind") == "assistant" and it.get("streaming"):
                    self._render_stream_tail(it, verbose)
                    break
            else:
                self._hide_stream_tail()
        else:
            idx = self._log_rendered_idx
            while idx < len(timeline):
                it = timeline[idx]
                if it.get("kind") == "assistant" and it.get("streaming"):
                    self._render_stream_tail(it, verbose)
                    break
                write_timeline_item(log, it, verbose=verbose)
                idx += 1
            self._log_rendered_idx = idx
            if idx >= len(timeline) or not (
                timeline[idx].get("kind") == "assistant" and timeline[idx].get("streaming")
            ):
                if not any(
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
                prompt.placeholder = (
                    "Message CoderAI…   / commands   @ pin   ⌘K palette   Enter to send"
                )
            footer = self.query_one("#composer-footer", Static)
            footer.update(self._composer_footer_markup())
        except Exception:
            pass

    def _render_chrome(self, s: SessionState) -> None:
        self._render_session_header(s)
        self._render_fleet(s)
        self._render_context(s)
        self._render_cost(s)

    def _render_stream_tail(self, it: Dict[str, Any], verbose: bool) -> None:
        try:
            tail = self.query_one("#stream-tail", Static)
        except Exception:
            return
        tail.update(build_stream_tail_markup(it, verbose=verbose))
        tail.display = True

    def _hide_stream_tail(self) -> None:
        try:
            tail = self.query_one("#stream-tail", Static)
        except Exception:
            return
        tail.update("")
        tail.display = False

    # ── session header + right pane ────────────────────────────────────

    def _render_session_header(self, s: SessionState) -> None:
        try:
            header = self.query_one("#session-header", Static)
        except Exception:
            return
        status_color = Tokens.AGENT if s.streaming or s.thinking else Tokens.TEXT_DIM
        ctx_used = f"{s.ctx_used:,}" if s.ctx_used else "0"
        ctx_lim = f"{s.ctx_limit // 1000}k" if s.ctx_limit else "?"
        model_label = s.model or "…"
        provider = s.provider or ""

        def chip(label: str, value: str, color: str = Tokens.TEXT, bar: float = -1) -> str:
            inner = f"[{Tokens.TEXT_MUTED}]{label}[/] [{color}]{value}[/]"
            if bar >= 0:
                w = 10
                f = min(w, max(0, int(bar * w)))
                b = f"[{color}]" + ("█" * f) + "[/]"
                b += f"[{Tokens.LINE}]" + ("─" * (w - f)) + "[/]"
                inner += f" {b}"
            return inner

        ctx_ratio = (s.ctx_used / max(1, s.ctx_limit)) if s.ctx_limit else 0
        chips = [
            f"[{status_color}]{Glyphs.DOT}[/] [{Tokens.TEXT}]{model_label}[/]",
            chip("provider", provider, Tokens.TEXT_MUTED),
            chip("ctx", f"{ctx_used} / {ctx_lim}", Tokens.TEXT, bar=ctx_ratio),
            chip("$", f"{s.cost_usd:.4f}", Tokens.TEXT_DIM),
            chip("iter", f"{s.iteration}/{s.max_iterations}", Tokens.TEXT_DIM),
        ]
        if s.elapsed_s > 0:
            m, sec = divmod(int(s.elapsed_s), 60)
            ts = f"{m}m {sec}s" if m > 0 else f"{sec}s"
            chips.append(chip("t", ts, Tokens.TEXT_MUTED))
        active = sum(1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled"))
        if active:
            chips.append(chip("agents", f"{active} active", Tokens.AGENT))
        yolo_c = Tokens.WARN if s.auto_approve else Tokens.TEXT_MUTED
        yolo_v = "ON" if s.auto_approve else "off"
        chips.append(chip("yolo", yolo_v, yolo_c))
        if s.reasoning and s.reasoning != "none":
            chips.append(chip("reason", s.reasoning, Tokens.THOUGHT))
        if s.progress:
            prog = s.progress
            label = str(prog.get("label") or "Working")
            current = prog.get("current")
            total = prog.get("total")
            if current is not None and total is not None:
                chips.append(chip("progress", f"{label} {current}/{total}", Tokens.AGENT))
            else:
                chips.append(chip("progress", label, Tokens.AGENT))

        left = f"[{Tokens.TEXT_MUTED}]│[/] ".join(chips)
        hints = f"[{Tokens.TEXT_MUTED}]⌘K palette · ⎋ cancel · @ pin · / slash[/]"
        header.update(f"{left}\n{hints}")

    def _render_fleet(self, s: SessionState) -> None:
        try:
            fleet = self.query_one("#fleet-content", Static)
        except Exception:
            return
        active_count = sum(
            1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled")
        )
        title = (
            f"[{Styles.SECTION}]AGENT FLEET[/]  [{Tokens.TEXT_MUTED}]· {active_count} active[/]\n"
        )
        if not s.agents:
            fleet.update(title + f"[{Tokens.TEXT_MUTED}](no agents yet)[/]")
            return

        # Build tree-ordered list: root first, then children in order
        agents = list(s.agents.values())
        root_ids = [a.id for a in agents if a.parent_id is None]
        tree_order: List[AgentInfo] = []
        seen: set[str] = set()

        def add_tree(aid: str, depth: int) -> None:
            if aid in seen:
                return
            info = s.agents.get(aid)
            if info is None:
                return
            seen.add(aid)
            info.depth = depth
            tree_order.append(info)
            children = [a for a in agents if a.parent_id == aid]
            for c in sorted(children, key=lambda x: x.name):
                add_tree(c.id, depth + 1)

        for rid in root_ids:
            add_tree(rid, 0)

        lines = [title]
        for i, info in enumerate(tree_order):
            is_last = (i == len(tree_order) - 1) or (
                i + 1 < len(tree_order) and tree_order[i + 1].depth < info.depth
            )
            lines.append(self._format_agent_card_tree(info, is_last))
            lines.append("")
        fleet.update("\n".join(lines).rstrip())

    def _format_agent_card_tree(self, info: AgentInfo, is_last: bool) -> str:
        status = info.status
        if status in ("thinking", "tool_call"):
            color = Tokens.AGENT if status == "tool_call" else Tokens.THOUGHT
            glow = True
        elif status == "waiting_for_user":
            color = Tokens.WARN
            glow = True
        elif status in ("done", "cancelled"):
            color = Tokens.TEXT_MUTED
            glow = False
        elif status == "error":
            color = Tokens.DANGER
            glow = False
        else:
            color = Tokens.WARN
            glow = False

        depth = info.depth
        name = info.name or info.id
        role = info.role or info.model or ""
        task = (info.task or "")[:34]
        tool = (info.tool or "—")[:24]
        cost = f"${info.cost_usd:.4f}"
        ctx_k = f"{info.ctx_used // 1000}k" if info.ctx_used else "0"

        # Build tree prefix
        if depth > 0:
            conn = Glyphs.TREE_END if is_last else Glyphs.TREE_MID
            prefix = f"[{Tokens.TEXT_MUTED}]{'  ' * (depth - 1)}{conn}─[/]"
        else:
            prefix = ""

        dot_glow = f"[{color}]" + ("●" if glow else Glyphs.DOT) + "[/]"
        line1 = f"{prefix} [{color}]{dot_glow}[/] [{Tokens.TEXT}]{name}[/]"
        if role:
            line1 += f"  [{Tokens.TEXT_MUTED}]{role}[/]"

        status_label = f"[{color}]{'▸' if glow else status.upper()}[/]"
        line2 = f"  [{Tokens.TEXT_DIM}]{task}[/]" if task else ""

        parent_line = ""
        if info.parent_id and depth <= 1:
            parent_line = f"  [{Tokens.TEXT_MUTED}]{Glyphs.PARENT} parent: {info.parent_id[-8:]}[/]"

        ctx_bar_w = 8
        ctx_fill = min(ctx_bar_w, max(0, int((info.ctx_used / max(1, info.ctx_limit)) * ctx_bar_w)))
        ctx_bar = (
            f"[{color}]"
            + ("▌" * ctx_fill)
            + f"[/][{Tokens.LINE}]"
            + ("─" * (ctx_bar_w - ctx_fill))
            + "[/]"
        )

        line3 = (
            f"  [{Tokens.TEXT_MUTED}]{tool}[/]  "
            f"[{Tokens.TEXT_MUTED}]{cost}  {ctx_k} {ctx_bar}[/]  "
            f"{status_label}"
        )

        parts = [line1]
        if parent_line:
            parts.append(parent_line)
        if line2:
            parts.append(line2)
        parts.append(line3)

        if status in ("done", "cancelled"):
            result = "\n".join(parts)
            return f"[dim]{result}[/]"
        return "\n".join(parts)

    def _render_context(self, s: SessionState) -> None:
        try:
            pane = self.query_one("#context-pane", Static)
        except Exception:
            return
        files = s.context_files or []
        title = f"[{Styles.SECTION}]PINNED CONTEXT[/]  [{Tokens.TEXT_MUTED}]· {len(files)} files[/]"
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
            bar = (
                f"[{color}]"
                + ("█" * filled)
                + f"[/][{Tokens.LINE}]"
                + ("─" * (30 - filled))
                + "[/]"
            )
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
        hints = f"[{Tokens.TEXT_MUTED}]↵ send · ⇧↵ newline · / commands · ⌘K palette[/]"
        meta = f"[{Tokens.TEXT_DIM}]reasoning:[/] [{Tokens.THOUGHT}]{reasoning}[/]"
        if not s.ready:
            return f"[{Tokens.TEXT_MUTED}]Waiting for agent…[/]   {hints}   {meta}"
        if s.progress:
            prog = s.progress
            label = escape(str(prog.get("label") or "Working"))
            current = prog.get("current")
            total = prog.get("total")
            if current is not None and total is not None:
                progress = f"[{Tokens.AGENT}]{label}[/] [{Tokens.TEXT_DIM}]{current}/{total}[/]"
            else:
                progress = f"[{Tokens.AGENT}]{label}[/]"
            return f"{progress}   {hints}   {meta}"
        return f"{hints}   {meta}"

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
        self._refresh_ui("full")

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

    def action_command_palette(self) -> None:
        self.run_worker(self._show_palette(), exclusive=True)

    async def _show_palette(self, only_section: Optional[str] = None) -> None:
        result = await self.push_screen_wait(
            CommandPaletteScreen(self.reducer.session, only_section)
        )
        if result is None or not self.controller:
            return
        # Handle command palette returns
        r = result.strip()
        if r.startswith("/"):
            # Route through slash handler directly to avoid palette re-entry loop
            if r in ("/help", "/?"):
                return  # already in palette
            self._submit(r)
        else:
            self.query_one("#prompt-area", TextArea).text = r
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
        self.reducer._push({"kind": "user", "id": self.reducer.next_id(), "text": text})
        self.reducer._bump_refresh("append")
        self.reducer._notify()
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
        self.run_worker(self._show_palette(), exclusive=True)

    def _show_model_menu(self) -> None:
        self.run_worker(self._show_palette("models"), exclusive=True)

    def _show_reasoning_menu(self) -> None:
        self.run_worker(self._show_palette("reasoning"), exclusive=True)

    def _show_persona_menu(self) -> None:
        self.run_worker(self._show_palette("personas"), exclusive=True)

    def _show_skills_menu(self) -> None:
        self.run_worker(self._show_palette("skills"), exclusive=True)

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
            from coderAI.core.agents import get_available_personas

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
        self.reducer._bump_refresh("append")
        self.reducer._notify()

    def _clear_context(self) -> None:
        if self.controller:
            self.controller.enqueue_command("clear_context")
            self.reducer.timeline.clear()
            self._log_rendered_idx = 0
            self._refresh_ui("full")

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
                self.reducer._bump_refresh("append")
                self.reducer._notify()
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

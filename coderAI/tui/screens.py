"""Modal screens extracted from CoderAIApp."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, List, Optional

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from coderAI.tui.diff_render import format_diff_compact
from coderAI.tui.help_menu import HELP_MENU_ENTRIES
from coderAI.tui.platform import palette_input_placeholder
from coderAI.tui.state import SessionState
from coderAI.tui.theme import Glyphs, Styles, Tokens


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
        if event.key == "@" and not self.text:
            event.stop()
            event.prevent_default()
            if hasattr(self.app, "action_file_picker"):
                self.app.action_file_picker()
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
        border: panel {Tokens.WARN};
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

    def on_mount(self) -> None:
        self.query_one("#approve-y", Button).focus()

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

            meta_parts = []
            if req_by:
                meta_parts.append(f"requested by [{Tokens.TEXT}]{escape(req_by)}[/]")
            if parent_id:
                meta_parts.append(f"sub-agent of [{Tokens.TEXT_MUTED}]{parent_id[-8:]}[/]")
            if iteration:
                meta_parts.append(f"iteration [{Tokens.TEXT_DIM}]{iteration}[/]")
            if meta_parts:
                yield Label(" · ".join(meta_parts), id="approval-meta")

            if tool_name == "run_command" and args:
                cmd_str = str(args.get("command", args.get("cmd", "")))
                if cmd_str:
                    yield Static(
                        f"[{Tokens.AGENT}]$[/] [{Tokens.TEXT}]{escape(cmd_str)}[/]",
                        id="approval-command",
                    )

            if diff:
                diff_text = format_diff_compact(diff, max_lines=14)
                yield Static(diff_text, id="approval-diff")

            args_text = str(args)[:400]
            if not diff:
                yield Label(args_text)

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

    @on(events.Key)
    def _on_approval_key(self, event: events.Key) -> None:
        key = event.key.lower()
        if key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss((False, False))
            return
        if key == "y":
            event.stop()
            event.prevent_default()
            self.dismiss((True, False))
            return
        if key == "n":
            event.stop()
            event.prevent_default()
            self.dismiss((False, False))
            return
        if key == "a":
            event.stop()
            event.prevent_default()
            self.dismiss((True, True))


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
        border: panel {Tokens.LINE};
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
        self.search_query = query.lower()

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def compose(self) -> ComposeResult:
        with Container(id="search-box"):
            yield Label("Search Timeline:")
            yield Input(value=self.search_query, placeholder="Type to search...", id="search-input")
            with VerticalScroll():
                yield Static(self._build_matches(self.search_query), id="search-results", markup=False)
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
        self.search_query = event.value
        self.query_one("#search-results", Static).update(self._build_matches(self.search_query))

    @on(Button.Pressed, "#search-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(events.Key)
    def _on_search_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)


def _action_to_text(action_type: str, action_val: Any, is_tab_completion: bool = False) -> str:
    if action_type == "cmd":
        return f"{action_val} " if is_tab_completion else str(action_val)
    elif action_type == "model":
        return f"/model {action_val}"
    elif action_type == "persona":
        return f"/persona {action_val}"
    elif action_type == "reasoning":
        return f"/reasoning {action_val}"
    elif action_type == "skills":
        return f"/skills {action_val}"
    return str(action_val)


class FuzzyPickerScreen(ModalScreen[Optional[str]]):
    """Base class for fuzzy-searchable picker modals."""

    BOX_ID: str = ""
    INPUT_ID: str = ""
    LIST_ID: str = ""
    FOOTER_ID: str = ""
    PLACEHOLDER: str = ""
    FOOTER_HELP: str = ""

    DEFAULT_CSS = f"""
    FuzzyPickerScreen {{
        align: center middle;
    }}
    FuzzyPickerScreen #picker-box, FuzzyPickerScreen #palette-box {{
        width: 80%;
        max-width: 80;
        height: auto;
        max-height: 85%;
        border: panel {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 0;
    }}
    #palette-box {{
        width: 70% !important;
    }}
    FuzzyPickerScreen #picker-input, FuzzyPickerScreen #palette-input {{
        margin: 1 2;
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT};
        border: solid {Tokens.LINE_SOFT};
    }}
    FuzzyPickerScreen OptionList {{
        height: auto;
        max-height: 20;
        margin: 1 0;
        padding: 0;
        background: {Tokens.BG_RAISED};
        border: none;
    }}
    FuzzyPickerScreen #picker-footer, FuzzyPickerScreen #palette-footer {{
        height: 1;
        padding: 0 2;
        background: {Tokens.BG_SUNK};
        color: {Tokens.TEXT_MUTED};
        border-top: solid {Tokens.LINE};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._query = ""

    @abstractmethod
    def _update_options(self, query: str) -> None:
        ...

    @abstractmethod
    def _get_selected_action_value(self, is_tab: bool = False) -> Optional[str]:
        ...

    def on_mount(self) -> None:
        self.query_one(f"#{self.INPUT_ID}", Input).focus()
        self._update_options("")

    def compose(self) -> ComposeResult:
        with Container(id=self.BOX_ID):
            yield Input(
                value="",
                placeholder=self.PLACEHOLDER,
                id=self.INPUT_ID,
            )
            yield OptionList(id=self.LIST_ID)
            yield Static(
                self.FOOTER_HELP,
                id=self.FOOTER_ID,
            )

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value or ""
        self._update_options(self._query)

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option and not event.option.disabled:
            val = self._get_selected_action_value(is_tab=False)
            if val is not None:
                self.dismiss(val)

    def _handle_key(self, event: events.Key) -> bool:
        key = event.key
        try:
            option_list = self.query_one(f"#{self.LIST_ID}", OptionList)
        except NoMatches:
            return False

        if key == "up":
            event.stop()
            event.prevent_default()
            option_list.action_cursor_up()
            return True
        if key == "down":
            event.stop()
            event.prevent_default()
            option_list.action_cursor_down()
            return True
        if key == "enter":
            event.stop()
            event.prevent_default()
            val = self._get_selected_action_value(is_tab=False)
            if val is not None:
                self.dismiss(val)
            return True
        if key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return True
        return False


class FilePickerScreen(FuzzyPickerScreen):
    """Fuzzy-searchable project file picker for pinning context."""

    BOX_ID = "picker-box"
    INPUT_ID = "picker-input"
    LIST_ID = "picker-list"
    FOOTER_ID = "picker-footer"
    PLACEHOLDER = "🔍 Type to search project files to pin..."
    FOOTER_HELP = f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ pin  ⎋ close[/]"

    def __init__(self, files: List[str]) -> None:
        super().__init__()
        self.files = files

    def _get_matches(self, query: str) -> List[str]:
        q = query.lower().strip()
        if not q:
            return self.files[:100]
        matches = []
        for f in self.files:
            if q in f.lower():
                matches.append(f)
        matches.sort(key=lambda x: (x.lower() != q, not x.lower().startswith(q), len(x)))
        return matches[:100]

    def _update_options(self, query: str) -> None:
        matches = self._get_matches(query)
        option_list = self.query_one(f"#{self.LIST_ID}", OptionList)
        option_list.clear_options()

        options = []
        for item in matches:
            prompt = f"  [{Tokens.TEXT}]{escape(item)}[/]"
            options.append(Option(prompt, id=item))

        if not options:
            options.append(
                Option(f'[{Tokens.TEXT_MUTED}]  no matching files for "{escape(query)}"[/]', id="none", disabled=True)
            )

        option_list.add_options(options)
        if option_list.option_count > 0:
            option_list.highlighted = 0

    def _get_selected_action_value(self, is_tab: bool = False) -> Optional[str]:
        try:
            option_list = self.query_one(f"#{self.LIST_ID}", OptionList)
        except NoMatches:
            return None
        idx = option_list.highlighted
        if idx is not None and 0 <= idx < option_list.option_count:
            opt = option_list.get_option_at_index(idx)
            if not opt.disabled:
                return opt.id
        return None

    @on(events.Key)
    async def _on_key(self, event: events.Key) -> None:
        self._handle_key(event)


class CommandPaletteScreen(FuzzyPickerScreen):
    """Fuzzy-searchable command palette with grouped sections."""

    BOX_ID = "palette-box"
    INPUT_ID = "palette-input"
    LIST_ID = "palette-list"
    FOOTER_ID = "palette-footer"
    PLACEHOLDER = palette_input_placeholder()
    FOOTER_HELP = f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ select  ⇥ complete  ⎋ close[/]"

    def __init__(self, session: SessionState, only_section: Optional[str] = None) -> None:
        super().__init__()
        self._s = session
        self._only_section = only_section
        self._cached_sections: Optional[List[Dict[str, Any]]] = None
        self._cached_query: str = "\0"

    def _gather_items(self, query: str) -> List[Dict[str, Any]]:
        q = query.lower().strip()
        sections: List[Dict[str, Any]] = []
        only = self._only_section

        def add_section(title: str, items: List[Dict[str, Any]]) -> None:
            if not items:
                return
            sections.append({"title": title, "items": items})

        if only is None or only == "commands":
            cmds = []
            for cmd, desc in HELP_MENU_ENTRIES:
                if not q or q in cmd.lower() or q in desc.lower():
                    cmds.append({"label": cmd, "desc": desc, "action": ("cmd", cmd)})
            add_section("Commands", cmds)

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

        if only is None or only == "models":
            models = []
            m = self._s.available_models or {}
            for provider, names in m.items():
                for n in names:
                    if not q or q in n.lower() or q in provider.lower():
                        models.append({"label": n, "desc": provider, "action": ("model", n)})
            add_section("Models", models)

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

        if only is None or only == "session":
            session = []
            session_items = [
                ("/clear", "Wipe conversation & context", "clear"),
                ("/compact", "Summarize long context", "compact"),
                ("/yolo", "Toggle auto-approve", "yolo"),
                ("/verbose", "Toggle verbose mode", "verbose"),
                ("/agents", "Refresh agents tree (left panel)", "agents"),
                ("/tasks", "Refresh TODO checklist panel", "session"),
                ("/plan", "Show execution plan (right panel)", "session"),
                ("/export", "Export session to markdown", "export"),
                ("/search", "Search timeline", "search"),
            ]
            for label, desc, action in session_items:
                if not q or q in label.lower() or q in desc.lower():
                    session.append({"label": label, "desc": desc, "action": ("cmd", label)})
            add_section("Session", session)

        return sections

    def _get_sections(self, query: str) -> List[Dict[str, Any]]:
        if self._cached_query != query or self._cached_sections is None:
            self._cached_sections = self._gather_items(query)
            self._cached_query = query
        return self._cached_sections

    def _update_options(self, query: str) -> None:
        sections = self._get_sections(query)
        option_list = self.query_one(f"#{self.LIST_ID}", OptionList)
        option_list.clear_options()

        options = []
        for sec in sections:
            options.append(
                Option(f"[{Styles.SECTION}]{sec['title']}[/]", id=f"header:{sec['title']}", disabled=True)
            )
            for item in sec["items"]:
                label = item["label"]
                desc = item["desc"]
                action_type, action_val = item["action"]
                prompt = f"  [{Tokens.AGENT}]{escape(label)}[/]  [{Tokens.TEXT_MUTED}]{escape(desc)}[/]"
                options.append(Option(prompt, id=f"{sec['title']}:{action_type}:{action_val}"))

        if not options:
            options.append(
                Option(f'[{Tokens.TEXT_MUTED}]  no matches for "{escape(query)}"[/\n]', id="none", disabled=True)
            )

        option_list.add_options(options)
        if option_list.option_count > 0:
            for idx in range(option_list.option_count):
                if not option_list.get_option_at_index(idx).disabled:
                    option_list.highlighted = idx
                    break

    def _get_selected_action_value(self, is_tab: bool = False) -> Optional[str]:
        try:
            option_list = self.query_one(f"#{self.LIST_ID}", OptionList)
        except NoMatches:
            return None
        idx = option_list.highlighted
        if idx is not None and 0 <= idx < option_list.option_count:
            opt = option_list.get_option_at_index(idx)
            if not opt.disabled and opt.id:
                parts = opt.id.split(":", 2)
                if len(parts) == 3:
                    _, action_type, action_val = parts
                    return _action_to_text(action_type, action_val, is_tab)
        return None

    @on(events.Key)
    async def _on_key(self, event: events.Key) -> None:
        if self._handle_key(event):
            return
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            val = self._get_selected_action_value(is_tab=True)
            if val is not None:
                self.dismiss(val)


class FullContentScreen(ModalScreen[None]):
    """Modal showing the full content of a diff or assistant response."""

    DEFAULT_CSS = f"""
    FullContentScreen {{
        align: center middle;
    }}
    FullContentScreen #full-box {{
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 85%;
        border: panel {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    FullContentScreen #full-header {{
        color: {Tokens.TEXT_DIM};
        margin-bottom: 1;
    }}
    FullContentScreen VerticalScroll {{
        height: auto;
        max-height: 30;
        margin-bottom: 1;
    }}
    FullContentScreen Horizontal {{
        height: auto;
        align-horizontal: center;
    }}
    FullContentScreen Button {{
        margin: 0 1;
    }}
    """

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with Container(id="full-box"):
            yield Label(self._title, id="full-header")
            with VerticalScroll():
                yield Static(self._content, id="full-body")
            with Horizontal():
                yield Button("Close (Esc)", id="full-close")
                yield Button("Copy", id="full-copy")

    @on(Button.Pressed, "#full-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#full-copy")
    def _copy(self) -> None:
        from coderAI.tui.clipboard import copy_to_clipboard_osc52

        copy_to_clipboard_osc52(self._content, self.notify)

    @on(events.Key)
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)

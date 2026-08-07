"""Modal screens extracted from CoderAIApp."""

from __future__ import annotations

from abc import abstractmethod
import re
import time
from typing import Any, Optional

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from coderAI.tui.diff_render import find_in_diff, format_diff_gutter
from coderAI.tui.help_menu import HELP_MENU_ENTRIES
from coderAI.tui.platform import palette_input_placeholder
from coderAI.tui.prompt_history import PromptHistory
from coderAI.tui.state import SessionState
from coderAI.tui.theme import Glyphs, Styles, Tokens


class AgentEventMsg(Message):
    """Agent event delivered to the UI thread."""

    def __init__(self, event: str, data: dict[str, Any]) -> None:
        super().__init__()
        self.event = event
        self.data = data


class PromptArea(TextArea):
    """TextArea that submits on Enter and inserts a newline on Shift/Alt+Enter.

    Adds shell-style prompt recall (Up/Down cycle previously submitted prompts)
    and an inline ``@`` file-mention trigger.
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Note: TextArea already owns ``self.history`` (its undo stack), so the
        # prompt recall buffer must use a distinct attribute name.
        self.prompt_history = PromptHistory()

    _TIMELINE_SCROLL_KEYS = {
        "pageup": "action_timeline_page_up",
        "pagedown": "action_timeline_page_down",
        "ctrl+home": "action_timeline_scroll_top",
        "ctrl+end": "action_timeline_scroll_bottom",
    }

    async def _on_key(self, event: events.Key) -> None:
        # TextArea consumes pageup/pagedown for cursor movement; redirect
        # them to the timeline so scrollback works while composing.
        action_name = self._TIMELINE_SCROLL_KEYS.get(event.key)
        if action_name is not None and hasattr(self.app, action_name):
            event.stop()
            event.prevent_default()
            getattr(self.app, action_name)()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text
            self.prompt_history.add(text)
            self.post_message(self.Submitted(text))
            return
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.prompt_history.reset()
            self.insert("\n")
            return
        # Textual names this key "at", so match the character, not event.key.
        if event.character == "@" and self._at_word_boundary():
            event.stop()
            event.prevent_default()
            self.prompt_history.reset()
            if hasattr(self.app, "action_file_mention"):
                self.app.action_file_mention()
            return
        if event.key == "up" and self._recall_prev():
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self._recall_next():
            event.stop()
            event.prevent_default()
            return
        if event.is_printable:
            # Typing forks a new draft, so abandon any in-flight history walk.
            self.prompt_history.reset()
        await super()._on_key(event)

    def _at_word_boundary(self) -> bool:
        """True when the cursor sits at line start or just after whitespace."""
        row, col = self.cursor_location
        if col == 0:
            return True
        line = self.document.get_line(row)
        prev_char = line[col - 1] if col - 1 < len(line) else ""
        return prev_char == "" or prev_char.isspace()

    def _recall_prev(self) -> bool:
        # Only hijack Up on the first line so multi-line editing still works.
        row, _ = self.cursor_location
        if row != 0:
            return False
        recalled = self.prompt_history.prev(self.text)
        if recalled is None:
            return False
        self._set_text(recalled)
        return True

    def _recall_next(self) -> bool:
        if not self.prompt_history.navigating:
            return False
        row, _ = self.cursor_location
        if row != self.document.line_count - 1:
            return False
        recalled = self.prompt_history.next()
        if recalled is not None:
            self._set_text(recalled)
        return True

    def _set_text(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)


class ApprovalScreen(ModalScreen[Optional[tuple[bool, bool]]]):
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
    ApprovalScreen #approval-timeout {{
        color: {Tokens.TEXT_DIM};
        margin-top: 1;
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

    def __init__(self, approval: dict[str, Any]) -> None:
        super().__init__()
        self.approval = approval

    def on_mount(self) -> None:
        self.query_one("#approve-y", Button).focus()
        if str(self.approval.get("risk", "low")) == "high":
            self.query_one("#approval-box").styles.border = ("panel", Tokens.DANGER)
        self._update_timeout()
        if self.approval.get("expiresAt"):
            self.set_interval(1.0, self._update_timeout)

    @property
    def approval_id(self) -> str:
        return str(self.approval.get("id") or "")

    def _update_timeout(self) -> None:
        try:
            label = self.query_one("#approval-timeout", Static)
        except NoMatches:
            return
        expires_at = self.approval.get("expiresAt")
        if not expires_at:
            label.update(f"[{Tokens.TEXT_DIM}]Waiting for your decision[/]")
            return
        remaining = max(0, int(float(expires_at) - time.time()))
        color = Tokens.DANGER if remaining <= 15 else Tokens.TEXT_DIM
        label.update(f"[{color}]Auto-denies in {remaining}s[/]")

    def compose(self) -> ComposeResult:
        a = self.approval
        tool_name = str(a.get("tool", ""))
        risk = str(a.get("risk", "low"))
        args = a.get("args") or {}
        diff = a.get("diff")
        req_by = str(a.get("requestedBy", ""))
        parent_id = a.get("parentId")
        iteration = int(a.get("iteration") or 0)

        risk_color = Tokens.DANGER if risk == "high" else Tokens.WARN
        remember_label = str(a.get("rememberLabel") or "")
        if tool_name in ("run_command", "run_background", "python_repl"):
            approve_label = "Run once (y)"
        elif tool_name == "workspace_trust":
            approve_label = "Trust workspace (y)"
        elif tool_name == "project_hooks":
            approve_label = "Enable hooks (y)"
        elif diff or tool_name in {
            "write_file",
            "search_replace",
            "apply_diff",
            "delete_file",
            "move_file",
        }:
            approve_label = "Apply once (y)"
        else:
            approve_label = "Allow once (y)"
        with Container(id="approval-box"):
            yield Label(
                f"[bold {risk_color}]▲[/] Approval required · "
                f"[bold {Tokens.TEXT}]{escape(tool_name)}[/]"
                f" · [{risk_color}]▲ {risk.upper()}[/] risk",
                id="approval-header",
            )

            meta_parts = []
            if req_by:
                meta_parts.append(f"requested by [{Tokens.TEXT}]{escape(req_by)}[/]")
            if parent_id:
                meta_parts.append(f"sub-agent of [{Tokens.TEXT_MUTED}]{parent_id[-8:]}[/]")
            if iteration:
                meta_parts.append(f"iteration [{Tokens.TEXT_DIM}]{iteration}[/]")
            prior_approved = int(a.get("priorApproved") or 0)
            if prior_approved:
                plural = "s" if prior_approved != 1 else ""
                meta_parts.append(
                    f"[{Tokens.WARN}]{prior_approved} prior approval{plural} this turn[/]"
                )
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
                diff_text = format_diff_gutter(diff, max_lines=14)
                yield Static(diff_text, id="approval-diff")

            if not diff and args and not isinstance(args, dict):
                yield Label(escape(str(args)[:400]))
            elif not diff and args:
                arg_lines = [
                    f"[{Tokens.TEXT_MUTED}]{escape(str(k))}:[/] [{Tokens.TEXT}]{escape(str(v)[:120])}[/]"
                    for k, v in list(args.items())[:6]
                    if not (tool_name == "run_command" and k in ("command", "cmd"))
                ]
                if len(args) > 6:
                    arg_lines.append(f"[{Tokens.TEXT_MUTED}]… {len(args) - 6} more[/]")
                yield Label("\n".join(arg_lines))

            # Risk factors are supplied by the controller (single source in
            # coderAI/tui/tool_metadata.tool_risk_factors); the screen only renders.
            risk_factors = a.get("riskFactors") or []
            risk_lines = [
                f"  [{Tokens.WARN}]{Glyphs.APPROVAL}[/] [{Tokens.TEXT}]{escape(str(factor))}[/]"
                for factor in risk_factors[:6]
            ]
            if risk_lines:
                yield Label(
                    f"[{Tokens.TEXT_MUTED}]WHY IT'S {risk.upper()} RISK[/]\n"
                    + "\n".join(risk_lines),
                    id="approval-risk",
                )

            yield Static("", id="approval-timeout")

            with Horizontal():
                yield Button(approve_label, id="approve-y", variant="success")
                yield Button("Deny (n)", id="approve-n", variant="error")
                if remember_label:
                    yield Button(f"{remember_label} (a)", id="approve-a", variant="warning")

    @on(Button.Pressed, "#approve-y")
    def _yes(self) -> None:
        self.dismiss((True, False))

    @on(Button.Pressed, "#approve-n")
    def _no(self) -> None:
        self.dismiss((False, False))

    @on(Button.Pressed, "#approve-a")
    def _always(self) -> None:
        if self.approval.get("rememberLabel"):
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
            if not self.approval.get("rememberLabel"):
                return
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

    def __init__(self, timeline: list[dict[str, Any]], query: str = "") -> None:
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
                yield Static(
                    self._build_matches(self.search_query), id="search-results", markup=True
                )
            yield Button("Close", id="search-close")

    def _extract_search_blob(self, it: dict[str, Any]) -> tuple[str, str]:
        """Return (kind_label, searchable text) for any timeline item."""
        kind = str(it.get("kind") or "")
        if kind == "user":
            return kind, str(it.get("text") or "")
        if kind == "assistant":
            parts = [str(it.get("content") or "")]
            reasoning = str(it.get("reasoning") or "").strip()
            if reasoning:
                parts.append(reasoning)
            return kind, "\n".join(p for p in parts if p)
        if kind == "tool":
            args = it.get("args") or {}
            if isinstance(args, dict):
                args_s = " ".join(f"{k}={v}" for k, v in args.items())
            else:
                args_s = str(args)
            preview = str(it.get("preview") or "")
            error = str(it.get("error") or "")
            name = str(it.get("name") or "")
            category = str(it.get("category") or "")
            blob = " ".join(p for p in [name, category, args_s, preview, error] if p)
            return kind, blob
        if kind == "diff":
            return kind, f"{it.get('path', '')} \n{it.get('diff', '')}"
        if kind == "error":
            return kind, f"{it.get('message', '')} {it.get('hint', '')} {it.get('details', '')}"
        if kind == "toast":
            return kind, str(it.get("message") or "")
        if kind == "approval":
            return kind, f"{it.get('tool', '')} {it.get('decided', '')}"
        if kind == "skill_card":
            return kind, f"{it.get('name', '')} {it.get('description', '')}"
        if kind == "plan_card":
            return kind, str(it.get("markdown") or "")
        if kind == "welcome":
            return kind, f"{it.get('model', '')} {it.get('provider', '')} {it.get('cwd', '')}"
        if kind == "separator":
            return kind, str(it.get("message") or "")
        # fallback: dump values
        return kind or "unknown", " ".join(str(v) for v in it.values() if isinstance(v, str))

    def _highlight_query(self, text: str, query: str) -> str:
        """Escape text and wrap query hits in accent markup."""
        esc = escape(text)
        if not query:
            return esc
        try:
            pat = re.compile(re.escape(query), re.IGNORECASE)
        except re.error:
            return esc
        # Use Tokens.WARN for highlight (amber) with bold
        return pat.sub(lambda m: f"[bold {Tokens.WARN}]{escape(m.group(0))}[/]", esc)

    def _build_matches(self, query: str) -> str:
        q = query.lower().strip()
        if not q:
            return f"[{Tokens.TEXT_MUTED}](type to search — {len(self.timeline)} items; all kinds included)[/]"
        matches: list[str] = []
        for i, it in enumerate(self.timeline):
            kind, blob = self._extract_search_blob(it)
            if not blob:
                continue
            if q not in blob.lower():
                continue
            # Find first hit position for snippet window
            low = blob.lower()
            pos = low.find(q)
            start = max(0, pos - 30)
            snippet = blob[start : start + 80]
            if start > 0:
                snippet = "…" + snippet
            if start + 80 < len(blob):
                snippet = snippet + "…"
            # Collapse newlines for one-line display
            snippet = snippet.replace("\n", " ").replace("\r", " ")
            highlighted = self._highlight_query(snippet, query.strip())
            kind_color = Tokens.TEXT_DIM
            if kind == "tool":
                kind_color = Tokens.INFO
            elif kind == "assistant":
                kind_color = Tokens.AGENT
            elif kind == "user":
                kind_color = Tokens.INFO
            elif kind == "diff":
                kind_color = Tokens.WARN
            elif kind == "error":
                kind_color = Tokens.DANGER
            matches.append(
                f"[{Tokens.TEXT_MUTED}]#{i}[/] [{kind_color}]{escape(kind)}[/] {highlighted}"
            )
            if len(matches) >= 50:
                matches.append(
                    f"[{Tokens.TEXT_MUTED}]… {len(self.timeline)} items scanned; showing first 50[/]"
                )
                break
        if not matches:
            return f'[{Tokens.TEXT_MUTED}](no matches for "{escape(query.strip())}")[/]'
        return "\n".join(matches)

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
    elif action_type == "mcp":
        return f"/mcp {action_val}"
    return str(action_val)


class FuzzyPickerScreen(ModalScreen[Optional[str]]):
    """Base class for fuzzy-searchable picker modals.

    Subclasses pass widget IDs and copy as constructor arguments so typos
    are caught at instantiation time rather than failing silently at runtime.
    """

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

    def __init__(
        self,
        *,
        box_id: str,
        input_id: str,
        list_id: str,
        footer_id: str,
        placeholder: str,
        footer_help: str,
    ) -> None:
        super().__init__()
        self._box_id = box_id
        self._input_id = input_id
        self._list_id = list_id
        self._footer_id = footer_id
        self._placeholder = placeholder
        self._footer_help = footer_help

    @abstractmethod
    def _update_options(self, query: str) -> None: ...

    def _get_selected_action_value(self, is_tab: bool = False) -> Optional[str]:
        """Default: the highlighted option's id. Subclasses with composite
        option ids (e.g. the command palette) override this."""
        try:
            option_list = self.query_one(f"#{self._list_id}", OptionList)
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

    def on_mount(self) -> None:
        self.query_one(f"#{self._input_id}", Input).focus()
        self._update_options("")

    def compose(self) -> ComposeResult:
        with Container(id=self._box_id):
            yield Input(
                value="",
                placeholder=self._placeholder,
                id=self._input_id,
            )
            yield OptionList(id=self._list_id)
            yield Static(self._footer_help, id=self._footer_id)

    @on(Input.Changed)
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._update_options(event.value or "")

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option and not event.option.disabled:
            # Special CTA ids
            if event.option.id == "__clear__":
                try:
                    inp = self.query_one(f"#{self._input_id}", Input)
                    inp.value = ""
                    self._update_options("")
                except Exception:
                    pass
                return
            val = self._get_selected_action_value(is_tab=False)
            if val is not None and val != "__clear__":
                self.dismiss(val)

    def _handle_key(self, event: events.Key) -> bool:
        key = event.key
        try:
            option_list = self.query_one(f"#{self._list_id}", OptionList)
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
            # Handle clear-filter CTA
            try:
                ol = self.query_one(f"#{self._list_id}", OptionList)
                idx = ol.highlighted
                if idx is not None and 0 <= idx < ol.option_count:
                    oid = ol.get_option_at_index(idx).id
                    if oid == "__clear__":
                        inp = self.query_one(f"#{self._input_id}", Input)
                        inp.value = ""
                        self._update_options("")
                        return True
            except Exception:
                pass
            val = self._get_selected_action_value(is_tab=False)
            if val is not None and val != "__clear__":
                self.dismiss(val)
            return True
        if key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return True
        return False


class FilePickerScreen(FuzzyPickerScreen):
    """Fuzzy-searchable project file picker for mentions that also pin context.

    Adds gitignore toggle, MRU tracking, and error UI.
    """

    _mru: list[str] = []

    def __init__(
        self,
        files: list[str],
        *,
        placeholder: Optional[str] = None,
        footer_help: Optional[str] = None,
        error: str | None = None,
        gitignore_enabled: bool = True,
    ) -> None:
        super().__init__(
            box_id="picker-box",
            input_id="picker-input",
            list_id="picker-list",
            footer_id="picker-footer",
            placeholder=placeholder or "🔍 Type to search project files to pin...",
            footer_help=footer_help
            or f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ pin  ⎋ close · g toggle gitignore · mru on top[/]",
        )
        self.files = files
        self._gitignore_enabled = gitignore_enabled
        self._error = error
        # Merge MRU on top when no query
        if FilePickerScreen._mru and not error:
            # Keep MRU entries that still exist in files on top
            mru_top = [p for p in FilePickerScreen._mru if p in files]
            others = [p for p in files if p not in mru_top]
            self.files = mru_top + others

    def _toggle_gitignore(self) -> None:
        self._gitignore_enabled = not self._gitignore_enabled
        # Filter: when disabled, show all files including gitignored (already in list)
        # When enabled, hide dotfiles and common ignored patterns
        if self._gitignore_enabled:
            self.files = [f for f in self.files if not f.startswith(".") and "__pycache__" not in f]
        # Refresh
        self._update_options(self.query_one(f"#{self._input_id}", Input).value or "")

    def _on_pick_success(self, path: str) -> None:
        # Update MRU
        if path in FilePickerScreen._mru:
            FilePickerScreen._mru.remove(path)
        FilePickerScreen._mru.insert(0, path)
        FilePickerScreen._mru = FilePickerScreen._mru[:10]

    def _handle_key(self, event: events.Key) -> bool:
        if event.key == "g":
            event.stop()
            event.prevent_default()
            self._toggle_gitignore()
            self.notify(f"Gitignore {'on' if self._gitignore_enabled else 'off'}")
            return True
        # Let parent handle other keys
        handled = super()._handle_key(event)
        # If handled as selection, update MRU
        if handled and event.key == "enter":
            val = self._get_selected_action_value()
            if val and val not in ("none", "__clear__"):
                self._on_pick_success(val)
        return handled

    def _score_file(self, path: str, query: str) -> float | None:
        """Fuzzy score — higher is better, None means no match.

        Uses difflib ratio on lowercased strings plus bonuses for
        substring/prefix, segment-boundary and path-depth locality so
        `q in lower` still ranks but `con` matches `controller.py` over
        `documentation.md`.
        """
        import difflib

        low = path.lower()
        q = query.lower().strip()
        if not q:
            return 1.0
        # Fast reject — at least half the query chars must appear in order
        # (subsequence check); otherwise difflib would score noise highly.
        it = iter(low)
        if not all(ch in it for ch in q):
            # Still allow pure substring hits to pass
            if q not in low:
                return None

        # Base fuzzy ratio on filename first, then full path
        fname = low.rsplit("/", 1)[-1] if "/" in low else low
        r_path = difflib.SequenceMatcher(None, q, low).ratio()
        r_name = difflib.SequenceMatcher(None, q, fname).ratio()
        score = max(r_path, r_name * 1.1)

        # Bonuses — earned, not decoration
        if q in low:
            score += 0.35
        if low.startswith(q) or fname.startswith(q):
            score += 0.25
        if f"/{q}" in low:
            score += 0.15
        # Penalize deep paths slightly so shallow matches surface
        score -= low.count("/") * 0.02
        # Shorter files get tiny bonus
        score -= len(path) * 0.0005
        return score

    def _get_matches(self, query: str) -> list[str]:
        q = query.lower().strip()
        if not q:
            # MRU-ish: recent/shallow files first when no query
            return sorted(self.files, key=lambda x: (x.count("/"), len(x)))[:100]
        scored: list[tuple[float, str]] = []
        for f in self.files:
            s = self._score_file(f, q)
            if s is not None and s > 0.25:
                scored.append((s, f))
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        return [f for _, f in scored[:100]]

    def _update_options(self, query: str) -> None:
        option_list = self.query_one(f"#{self._list_id}", OptionList)
        # Error UI (previously only logger warning)
        if getattr(self, "_error", None):
            option_list.clear_options()
            option_list.add_options(
                [
                    Option(
                        f"[{Tokens.DANGER}]  error: {escape(str(self._error)[:80])}[/]",
                        id="none",
                        disabled=True,
                    ),
                    Option(
                        f"[{Tokens.TEXT_MUTED}]  try /refresh or check project root[/]",
                        id="none2",
                        disabled=True,
                    ),
                ]
            )
            option_list.highlighted = 0
            return
        # Loading state when file list is still scanning
        if not self.files:
            option_list.clear_options()
            option_list.add_options(
                [
                    Option(
                        f"[{Tokens.TEXT_MUTED}]  scanning project files…[/]",
                        id="none",
                        disabled=True,
                    )
                ]
            )
            option_list.highlighted = 0
            return

        matches = self._get_matches(query)
        option_list.clear_options()

        options = []
        for item in matches:
            # Highlight the query substring when present (case-insensitive)
            esc_item = escape(item)
            if query.strip():
                try:
                    import re as _re

                    pat = _re.compile(_re.escape(query.strip()), _re.IGNORECASE)
                    # Re-highlight on the escaped string — safe because escape() never introduces regex metachars
                    hl = pat.sub(lambda m: f"[bold {Tokens.WARN}]{escape(m.group(0))}[/]", esc_item)
                except Exception:
                    hl = esc_item
            else:
                hl = esc_item
            prompt = f"  [{Tokens.TEXT}]{hl}[/]"
            options.append(Option(prompt, id=item))

        if not options:
            q_esc = escape(query.strip()[:40])
            options.append(
                Option(
                    f'[{Tokens.TEXT_MUTED}]  no matching files for "{q_esc}"[/]',
                    id="none",
                    disabled=True,
                )
            )
            # CTA to clear filter when query is active
            if query.strip():
                options.append(
                    Option(
                        f"[{Tokens.TEXT_DIM}]  ↩ clear filter (Esc to close)[/]",
                        id="__clear__",
                        disabled=False,
                    )
                )
            # When _get_matches is empty but files exist, hint that fuzzy threshold may be too strict
            if q_esc and len(q_esc) >= 2:
                options.append(
                    Option(
                        f"[{Tokens.TEXT_MUTED}]  tip: try a shorter prefix — fuzzy match @ 0.25[/]",
                        id="none2",
                        disabled=True,
                    )
                )

        option_list.add_options(options)
        if option_list.option_count > 0:
            # Highlight first selectable (skip disabled scanning row)
            for i in range(option_list.option_count):
                if (
                    not option_list.get_option_at_index(i).disabled
                    or option_list.get_option_at_index(i).id == "__clear__"
                ):
                    option_list.highlighted = i
                    break
            else:
                option_list.highlighted = 0


class SessionPickerScreen(FuzzyPickerScreen):
    """Fuzzy-searchable saved-session picker for /resume.

    ``sessions`` is the output of ``history_manager.list_sessions()`` (newest
    first); dismisses with the chosen session id, or None on escape.
    """

    def __init__(
        self,
        sessions: list[dict[str, Any]],
        current_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            box_id="picker-box",
            input_id="picker-input",
            list_id="picker-list",
            footer_id="picker-footer",
            placeholder="🔍 Type to search saved sessions to resume…",
            footer_help=f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ resume  ⎋ close[/]",
        )
        self.sessions = sessions
        self.current_id = current_id

    def _get_matches(self, query: str) -> list[dict[str, Any]]:
        q = query.lower().strip()
        if not q:
            return self.sessions[:100]
        matches = []
        for s in self.sessions:
            haystack = " ".join(
                str(s.get(k, "")) for k in ("session_id", "model", "updated_at", "created_at")
            ).lower()
            if q in haystack:
                matches.append(s)
        return matches[:100]

    def _update_options(self, query: str) -> None:
        matches = self._get_matches(query)
        option_list = self.query_one(f"#{self._list_id}", OptionList)
        option_list.clear_options()

        options = []
        for s in matches:
            sid = str(s.get("session_id", ""))
            is_current = bool(sid) and sid == self.current_id
            marker = f"  [{Tokens.WARN}]· current[/]" if is_current else ""
            # Long ids wrap the row in the 80-col picker box; the trailing
            # hex chunk is enough to tell sessions apart visually.
            sid_disp = sid if len(sid) <= 18 else "…" + sid[-8:]
            prompt = (
                f"  [{Tokens.TEXT}]{escape(str(s.get('updated_at', '')))}[/]"
                f"  [{Tokens.TEXT_DIM}]{s.get('messages', 0):>4} msgs[/]"
                f"  [{Tokens.TEXT_MUTED}]{escape(str(s.get('model', '')))}[/]"
                f"  [{Tokens.TEXT_MUTED}]{escape(sid_disp)}[/]{marker}"
            )
            options.append(Option(prompt, id=sid, disabled=is_current))

        if not options:
            empty = (
                f'no sessions matching "{escape(query)}"' if query.strip() else "no saved sessions"
            )
            options.append(Option(f"[{Tokens.TEXT_MUTED}]  {empty}[/]", id="none", disabled=True))

        option_list.add_options(options)
        for idx in range(option_list.option_count):
            if not option_list.get_option_at_index(idx).disabled:
                option_list.highlighted = idx
                break


class CommandPaletteScreen(FuzzyPickerScreen):
    """Fuzzy-searchable command palette with grouped sections."""

    def __init__(self, session: SessionState, only_section: Optional[str] = None) -> None:
        super().__init__(
            box_id="palette-box",
            input_id="palette-input",
            list_id="palette-list",
            footer_id="palette-footer",
            placeholder=palette_input_placeholder(),
            footer_help=f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ select  ⇥ complete  ⎋ close[/]",
        )
        self._s = session
        self._only_section = only_section
        self._cached_sections: Optional[list[dict[str, Any]]] = None
        self._cached_query: str = "\0"

    def _gather_items(self, query: str) -> list[dict[str, Any]]:
        q = query.lower().strip()
        sections: list[dict[str, Any]] = []
        only = self._only_section

        def add_section(title: str, items: list[dict[str, Any]]) -> None:
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
            details = getattr(self._s, "available_model_details", None) or {}
            for provider, names in m.items():
                for n in names:
                    d = details.get(n, {})
                    tier = d.get("tier", "")
                    tier_badge = {
                        "frontier": "● Frontier",
                        "mid": "◐ Mid",
                        "small": "○ Small",
                        "custom": "⬡ Local",
                    }.get(tier, provider)
                    think = "🧠" if d.get("supports_reasoning") else ""
                    label = n
                    desc = f"{provider} · {tier_badge} {think}".strip()
                    # searchable: tier, label, provider, id
                    hay = f"{n.lower()} {provider.lower()} {tier} {d.get('label', '').lower()}"
                    if not q or q in hay:
                        models.append({"label": label, "desc": desc, "action": ("model", n)})
            add_section("Models", models)

        if only is None or only == "reasoning":
            # hide unsupported levels if current model doesn't support reasoning
            from coderAI.llm.registry import get_spec

            spec = get_spec(self._s.model) if self._s.model else None
            supports = bool(spec and spec.supports_reasoning)
            reason = []
            for e in ("high", "medium", "low", "none"):
                if not q or q in e:
                    if not supports and e != "none":
                        desc = "Not supported by current model"
                    else:
                        desc = "Set reasoning effort"
                    reason.append(
                        {
                            "label": f"/reasoning {e}",
                            "desc": desc,
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

        if only is None or only == "mcp":
            mcp_items = []
            for srv in self._s.available_mcp_servers or []:
                name = srv.get("name", "")
                if srv.get("connected"):
                    status = f"● on · {srv.get('tools', 0)} tools"
                    if srv.get("degraded"):
                        status += " (degraded)"
                elif srv.get("disabled"):
                    status = "○ off (disabled)"
                else:
                    status = "○ off"
                if not q or q in name.lower():
                    mcp_items.append(
                        {"label": f"/mcp {name}", "desc": status, "action": ("mcp", name)}
                    )
            add_section("MCP servers", mcp_items)

        return sections

    def _get_sections(self, query: str) -> list[dict[str, Any]]:
        if self._cached_query != query or self._cached_sections is None:
            self._cached_sections = self._gather_items(query)
            self._cached_query = query
        return self._cached_sections

    def _update_options(self, query: str) -> None:
        sections = self._get_sections(query)
        option_list = self.query_one(f"#{self._list_id}", OptionList)
        option_list.clear_options()

        options = []
        for sec in sections:
            options.append(
                Option(
                    f"[{Styles.SECTION}]{sec['title']}[/]",
                    id=f"header:{sec['title']}",
                    disabled=True,
                )
            )
            for item in sec["items"]:
                label = item["label"]
                desc = item["desc"]
                action_type, action_val = item["action"]
                prompt = (
                    f"  [{Tokens.AGENT}]{escape(label)}[/]  [{Tokens.TEXT_MUTED}]{escape(desc)}[/]"
                )
                options.append(Option(prompt, id=f"{sec['title']}:{action_type}:{action_val}"))

        if not options:
            options.append(
                Option(
                    f'[{Tokens.TEXT_MUTED}]  no matches for "{escape(query)}"[/]',
                    id="none",
                    disabled=True,
                )
            )

        option_list.add_options(options)
        if option_list.option_count > 0:
            for idx in range(option_list.option_count):
                if not option_list.get_option_at_index(idx).disabled:
                    option_list.highlighted = idx
                    break

    def _get_selected_action_value(self, is_tab: bool = False) -> Optional[str]:
        try:
            option_list = self.query_one(f"#{self._list_id}", OptionList)
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

    def __init__(self, title: str, content: str, *, syntax: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._content = content
        self._syntax = syntax

    def compose(self) -> ComposeResult:
        with Container(id="full-box"):
            yield Label(self._title, id="full-header")
            with VerticalScroll():
                if self._syntax:
                    try:
                        from rich.syntax import Syntax

                        yield Static(
                            Syntax(self._content, self._syntax, line_numbers=True, word_wrap=True),
                            id="full-body",
                        )
                    except Exception:
                        yield Static(self._content, id="full-body")
                else:
                    yield Static(self._content, id="full-body")
            with Horizontal():
                yield Button("Close (Esc)", id="full-close")
                yield Button("Copy", id="full-copy")

    @on(Button.Pressed, "#full-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#full-copy")
    def _copy(self) -> None:
        from coderAI.tui.clipboard import copy_text

        write_osc52 = None
        app = self.app
        if hasattr(app, "_osc52_writer"):
            write_osc52 = app._osc52_writer()
        copy_text(
            self._content,
            write_osc52=write_osc52,
            notify_fn=self.notify,
            fallback_file=True,
        )

    @on(events.Key)
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)


class DiffReviewScreen(ModalScreen[Optional[dict[str, Any]]]):
    """Inline hunk --/++ accept/reject toggles + find-in-diff."""

    DEFAULT_CSS = f"""
    DiffReviewScreen {{
        align: center middle;
    }}
    DiffReviewScreen #diff-box {{
        width: 92%;
        max-width: 120;
        height: auto;
        max-height: 85%;
        border: panel {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    DiffReviewScreen #diff-header {{
        color: {Tokens.TEXT_DIM};
        margin-bottom: 1;
    }}
    DiffReviewScreen #diff-body {{
        height: auto;
        max-height: 28;
        background: {Tokens.BG_SUNK};
        padding: 1;
        border: solid {Tokens.LINE_SOFT};
    }}
    DiffReviewScreen #diff-find {{
        margin: 1 0;
    }}
    DiffReviewScreen Horizontal {{
        height: auto;
        align-horizontal: center;
        margin-top: 1;
    }}
    DiffReviewScreen Button {{
        margin: 0 1;
    }}
    """

    def __init__(self, path: str, diff: str) -> None:
        super().__init__()
        self.path = path
        self.diff = diff
        self._find_query = ""
        # hunk_idx -> bool (True=accept, False=reject, None=undecided)
        self._toggles: dict[int, bool] = {}

    def compose(self) -> ComposeResult:
        with Container(id="diff-box"):
            yield Label(f"Diff — {escape(self.path)}  (--/++ per hunk)", id="diff-header")
            yield Input(placeholder="Find in diff… (highlights matches)", id="diff-find")
            with VerticalScroll():
                yield Static(self._render_diff(), id="diff-body", markup=True)
            with Horizontal():
                yield Button("Accept all", id="diff-accept-all", variant="success")
                yield Button("Reject all", id="diff-reject-all", variant="error")
                yield Button("Apply toggles", id="diff-apply", variant="primary")
                yield Button("Close", id="diff-close")

    def _render_diff(self) -> str:
        # If find query active, highlight matches via find_in_diff
        matches = set(find_in_diff(self.diff, self._find_query)) if self._find_query else set()
        base = format_diff_gutter(self.diff, max_lines=10_000, show_line_numbers=True)
        lines = base.split("\n")
        out: list[str] = []
        hunk_idx = -1
        for i, line in enumerate(lines):
            # Detect hunk header to increment toggle index
            if "@@" in line:
                hunk_idx += 1
                state = self._toggles.get(hunk_idx)
                if state is True:
                    line += f"  [{Tokens.AGENT}]✓ accept[/]"
                elif state is False:
                    line += f"  [{Tokens.DANGER}]✗ reject[/]"
                else:
                    line += f"  [{Tokens.TEXT_MUTED}][a]ccept/[r]eject[/]"
            # Highlight find matches
            if i in matches and self._find_query:
                # Simple highlight already done via format? Add marker
                line = f"[on {Tokens.WARN}]{line}[/]"
            out.append(line)
        # Add toggle help
        out.append(
            f"\n[{Tokens.TEXT_MUTED}]Keys: a=accept hunk, r=reject hunk, n/p=find next/prev, Esc=close[/]"
        )
        return "\n".join(out)

    def _refresh_body(self) -> None:
        try:
            self.query_one("#diff-body", Static).update(self._render_diff())
        except NoMatches:
            pass

    @on(Input.Changed, "#diff-find")
    def _on_find_changed(self, event: Input.Changed) -> None:
        self._find_query = event.value
        self._refresh_body()

    @on(Button.Pressed, "#diff-accept-all")
    def _accept_all(self) -> None:
        # Mark all hunks as accept
        hunk_count = self.diff.count("@@")
        for idx in range(hunk_count):
            self._toggles[idx] = True
        self._refresh_body()
        self.notify("All hunks marked accept")

    @on(Button.Pressed, "#diff-reject-all")
    def _reject_all(self) -> None:
        hunk_count = self.diff.count("@@")
        for idx in range(hunk_count):
            self._toggles[idx] = False
        self._refresh_body()
        self.notify("All hunks marked reject")

    @on(Button.Pressed, "#diff-apply")
    def _apply(self) -> None:
        self.dismiss({"path": self.path, "toggles": dict(self._toggles)})

    @on(Button.Pressed, "#diff-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(events.Key)
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
            return
        if event.key == "a":
            # Accept current hunk (last focused)
            # For simplicity, accept next undecided
            hunk_count = self.diff.count("@@")
            for idx in range(hunk_count):
                if idx not in self._toggles:
                    self._toggles[idx] = True
                    break
            else:
                # All decided, toggle first
                self._toggles[0] = True
            self._refresh_body()
            event.stop()
            event.prevent_default()
        elif event.key == "r":
            hunk_count = self.diff.count("@@")
            for idx in range(hunk_count):
                if idx not in self._toggles:
                    self._toggles[idx] = False
                    break
            else:
                self._toggles[0] = False
            self._refresh_body()
            event.stop()
            event.prevent_default()


class ConfigScreen(ModalScreen[Optional[dict[str, str]]]):
    """In-TUI /config form (model/budget/notifications) — previously CLI only."""

    DEFAULT_CSS = f"""
    ConfigScreen {{
        align: center middle;
    }}
    ConfigScreen #config-box {{
        width: 70;
        height: auto;
        max-height: 80%;
        border: panel {Tokens.LINE};
        background: {Tokens.BG_RAISED};
        padding: 1 2;
    }}
    ConfigScreen Label {{
        color: {Tokens.TEXT_DIM};
    }}
    ConfigScreen Input {{
        margin: 1 0;
        background: {Tokens.BG_SUNK};
        border: solid {Tokens.LINE_SOFT};
    }}
    ConfigScreen Horizontal {{
        height: auto;
        align-horizontal: center;
        margin-top: 1;
    }}
    ConfigScreen Button {{
        margin: 0 1;
    }}
    """

    def __init__(self, current: dict[str, str] | None = None) -> None:
        super().__init__()
        self.current = current or {}

    def compose(self) -> ComposeResult:
        with Container(id="config-box"):
            yield Label("Configuration — model / budget / notifications", id="config-header")
            yield Label("Model", id="cfg-model-label")
            yield Input(
                value=self.current.get("model", ""), placeholder="e.g. gpt-4o", id="cfg-model"
            )
            yield Label("Budget USD", id="cfg-budget-label")
            yield Input(
                value=self.current.get("budget", ""), placeholder="e.g. 10.00", id="cfg-budget"
            )
            yield Label("Notifications (on/off)", id="cfg-notif-label")
            yield Input(
                value=self.current.get("notifications", "on"),
                placeholder="on / off",
                id="cfg-notif",
            )
            with Horizontal():
                yield Button("Save", id="cfg-save", variant="primary")
                yield Button("Cancel", id="cfg-cancel")

    @on(Button.Pressed, "#cfg-save")
    def _cfg_save(self) -> None:
        try:
            model = self.query_one("#cfg-model", Input).value
            budget = self.query_one("#cfg-budget", Input).value
            notif = self.query_one("#cfg-notif", Input).value
        except NoMatches:
            self.dismiss(None)
            return
        self.dismiss({"model": model, "budget": budget, "notifications": notif})

    @on(Button.Pressed, "#cfg-cancel")
    def _cfg_cancel(self) -> None:
        self.dismiss(None)

    @on(events.Key)
    def _cfg_on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)

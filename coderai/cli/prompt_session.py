"""Prompt Toolkit session — Phase2 port of Kimi ui/shell/prompt.py (lean).

Provides:
- SlashCommandCompleter (fuzzy, WordCompleter, should_complete)
- FileMentionCompleter (git ls-files + walk fallback, basename re-rank)
- Bottom toolbar (git branch/status cache, cwd truncate, plan/mode badges, tip rotation)
- CoderAIPromptSession (PromptSession wrapper, history per-workspace, key bindings)

Pony: ~380 LOC vs Kimi 2259 — keeps core UX, drops placeholder/media pipeline (Phase5).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Lazy prompt_toolkit import with fallback flag
# ---------------------------------------------------------------------------
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import (
        Completer,
        Completion,
        FuzzyCompleter,
        WordCompleter,
        merge_completers,
    )
    from prompt_toolkit.document import Document
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    HAS_PTK = True
except ImportError:  # pragma: no cover
    HAS_PTK = False
    PromptSession = Any  # type: ignore
    Completer = object  # type: ignore
    Completion = Any  # type: ignore

# ---------------------------------------------------------------------------
# Slash completer — Kimi prompt.py:89
# ---------------------------------------------------------------------------
if HAS_PTK:

    class SlashCommandCompleter(Completer):
        """Fuzzy slash completer — canonical /name, alias support, and subargument completion."""

        def __init__(self, available_commands: Any, project_root: str = ".") -> None:
            super().__init__()
            self.project_root = project_root
            self._available_commands: list[Any] = []
            self._command_lookup: dict[str, list[Any]] = {}
            words: list[str] = []
            try:
                # Normalize to list
                cmds = list(available_commands)
                # Support tuple entries ("/name", desc) by wrapping into object
                normalized: list[Any] = []
                for c in cmds:
                    if isinstance(c, (list, tuple)) and len(c) == 2 and isinstance(c[0], str):
                        # ("/name", desc) tuple
                        class _Tmp:
                            def __init__(self, n: str, d: str):
                                self.name = n.lstrip("/")
                                self.description = d
                                self.summary = d
                                self.aliases: list[str] = []

                            def display_name(self, trigger: str | None = None) -> str:
                                if trigger and trigger != self.name and trigger in self.aliases:
                                    return f"/{self.name} ({trigger})"
                                return f"/{self.name}"

                        normalized.append(_Tmp(c[0], c[1]))
                    else:
                        normalized.append(c)
                self._available_commands = sorted(
                    normalized, key=lambda c: getattr(c, "name", str(c))
                )
                for cmd in self._available_commands:
                    name = getattr(cmd, "name", None)
                    if not name:
                        continue
                    if name not in self._command_lookup:
                        self._command_lookup[name] = []
                        words.append(name)
                    self._command_lookup[name].append(cmd)
                    for alias in getattr(cmd, "aliases", []) or []:
                        if alias in self._command_lookup:
                            self._command_lookup[alias].append(cmd)
                        else:
                            self._command_lookup[alias] = [cmd]
                            words.append(alias)
            except Exception:
                # fallback: plain strings
                try:
                    for w in available_commands:  # type: ignore
                        words.append(str(w))
                except Exception:
                    pass
            self._word_pattern = re.compile(r"[^\s]+")
            self._fuzzy_pattern = r"^[^\s]*"
            self._word_completer = WordCompleter(words, WORD=False, pattern=self._word_pattern)
            self._fuzzy = FuzzyCompleter(
                self._word_completer, WORD=False, pattern=self._fuzzy_pattern
            )

        @staticmethod
        def should_complete(document: Document) -> bool:
            text = document.text_before_cursor
            if document.text_after_cursor.strip():
                return False
            stripped_start = text.lstrip()
            return stripped_start.startswith("/")

        def _display_name(self, cmd: Any, trigger: str) -> str:
            try:
                if hasattr(cmd, "display_name") and callable(cmd.display_name):
                    res = cmd.display_name(trigger)
                    if isinstance(res, str):
                        return res
            except Exception:
                pass
            name = getattr(cmd, "name", str(cmd))
            if not isinstance(name, str):
                name = str(name)
            aliases = getattr(cmd, "aliases", []) or []
            if trigger != name and trigger in aliases:
                return f"/{name} ({trigger})"
            return f"/{name}"

        def _cmd_description(self, cmd: Any) -> str:
            desc = getattr(cmd, "description", None) or getattr(cmd, "summary", "") or ""
            return str(desc) if not isinstance(desc, str) else desc

        def get_completions(self, document: Document, complete_event: Any):  # type: ignore[override]
            if not self.should_complete(document):
                return
            text = document.text_before_cursor
            stripped = text.lstrip()
            if not stripped.startswith("/"):
                return

            from coderai.cli.fuzzy import fuzzy_filter

            # Case 1: Sub-argument completion (when a space is present after command)
            if " " in stripped:
                parts = stripped.split(None, 1)
                lead_cmd = parts[0].lower()
                arg_typed = (
                    parts[1]
                    if len(parts) > 1 and not stripped.endswith(" ")
                    else (stripped.split()[-1] if not stripped.endswith(" ") else "")
                )
                if stripped.endswith(" "):
                    arg_typed = ""
                else:
                    # token before cursor
                    arg_typed = text.split()[-1] if text.split() else ""

                candidates: list[tuple[str, str]] = []

                if lead_cmd in ("/model",):
                    from coderai.cli.interactive_menu import CURATED_MODELS

                    for m_name, m_desc, _ in CURATED_MODELS:
                        candidates.append((m_name, m_desc[:50]))

                elif lead_cmd in ("/plan",):
                    for sub in ("on", "off", "view", "clear", "apply", "reset"):
                        candidates.append((sub, f"Plan Mode {sub}"))

                elif lead_cmd in ("/effort", "/reasoning"):
                    for eff in ("max", "high", "medium", "low", "off"):
                        candidates.append((eff, f"Reasoning effort: {eff}"))

                elif lead_cmd in ("/thinking", "/raw"):
                    for mode in ("full", "summary", "lite", "normal", "on", "off"):
                        candidates.append((mode, f"Thinking trace mode: {mode}"))

                elif lead_cmd in ("/setup", "/auth", "/configure"):
                    for sub in ("quick", "keys", "models", "provider", "test", "status"):
                        candidates.append((sub, f"Setup wizard: {sub}"))

                elif lead_cmd in ("/theme",):
                    for th in ("dark", "light"):
                        candidates.append((th, f"Terminal theme: {th}"))

                elif lead_cmd in ("/skill",):
                    from coderai.core.skill import list_skills

                    try:
                        skills = list_skills(self.project_root)
                        for sk in skills:
                            if isinstance(sk, dict) and sk.get("name"):
                                candidates.append((sk["name"], (sk.get("description") or "")[:50]))
                    except Exception:
                        pass

                elif lead_cmd in ("/help", "/?"):
                    for cmd_name in self._command_lookup:
                        candidates.append((cmd_name, f"Help on /{cmd_name}"))

                if candidates:
                    matching_names = fuzzy_filter(arg_typed, [c[0] for c in candidates], limit=20)
                    desc_map = {c[0]: c[1] for c in candidates}
                    for name in matching_names:
                        yield Completion(
                            text=name,
                            start_position=-len(arg_typed),
                            display=name,
                            display_meta=desc_map.get(name, ""),
                        )
                return

            # Case 2: Slash command completion
            last_space = text.rfind(" ")
            token = text[last_space + 1 :]
            typed = token[1:]
            mention_doc = Document(text=typed, cursor_position=len(typed))
            fuzzy_candidates = list(self._fuzzy.get_completions(mention_doc, complete_event))
            seen: set[str] = set()
            candidate_triggers: list[str] = []
            if typed and typed in self._command_lookup:
                candidate_triggers.append(typed)
            for cand in fuzzy_candidates:
                if cand.text not in candidate_triggers:
                    candidate_triggers.append(cand.text)
            for trigger in candidate_triggers:
                cmds = self._command_lookup.get(trigger)
                if not cmds:
                    continue
                for cmd in cmds:
                    name = getattr(cmd, "name", str(cmd))
                    if name in seen:
                        continue
                    seen.add(name)
                    completion_text = f"/{name}"
                    if trigger == name and typed == name:
                        completion_text += " "
                    yield Completion(
                        text=completion_text,
                        start_position=-len(token),
                        display=self._display_name(cmd, trigger),
                        display_meta=self._cmd_description(cmd),
                    )

    class FileMentionCompleter(Completer):
        """@-file completer — thin wrapper over coderai.cli.file_mention.

        Canonical file list / fuzzy logic lives in file_mention.py
        (git ls-files 5s TTL + walk fallback 1000 cap + basename re-rank).
        This completer only handles trigger detection (@) and PTK Completion
        yielding. Ponytail: no duplicate walk/git code here.
        """

        def __init__(self, project_root: str) -> None:
            super().__init__()
            self.project_root = project_root

        def get_completions(self, document: Document, complete_event: Any):  # type: ignore[override]
            text_before = document.text_before_cursor
            at_idx = text_before.rfind("@")
            if at_idx == -1:
                return
            token = text_before[at_idx + 1 :]
            if " " in token or "\n" in token:
                return
            if token and not re.match(r"^[\w.\-_/\\'\":@#~]*$", token):
                return
            query = token
            # Delegate to canonical file_mention helper (single source)
            try:
                from coderai.cli.file_mention import suggest_workspace_files

                candidates = suggest_workspace_files(query, self.project_root, limit=20)
            except Exception:
                candidates = []
            for f in candidates:
                yield Completion(
                    text=f,
                    start_position=-len(token),
                    display=f,
                    display_meta="file",
                )

else:  # stub when no ptk

    class SlashCommandCompleter:  # type: ignore
        pass

    class FileMentionCompleter:  # type: ignore
        pass


# ---------------------------------------------------------------------------
# Bottom toolbar helpers
# ---------------------------------------------------------------------------
_tip_index = 0
_tip_last_rotate = time.monotonic()
_TIPS = [
    "ctrl-o: editor",
    "shift-tab: toggle mode",
    "@: mention files",
    "tab: complete",
]


def _format_git_badge(branch: str, dirty: bool, ahead: int, behind: int) -> str:
    parts: list[str] = []
    if dirty:
        parts.append("±")
    sync = ""
    if ahead:
        sync += f"↑{ahead}"
    if behind:
        sync += f"↓{behind}"
    if sync:
        parts.append(sync)
    if not parts:
        return branch
    return f"{branch} [{' '.join(parts)}]"


def _shorten_cwd(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _truncate_left(text: str, max_cols: int) -> str:
    if not HAS_PTK:
        return text[:max_cols]
    from prompt_toolkit.utils import get_cwidth

    if sum(get_cwidth(c) for c in text) <= max_cols:
        return text
    ellipsis = "…"
    budget = max_cols - get_cwidth(ellipsis)
    chars: list[str] = []
    width = 0
    for ch in reversed(text):
        w = get_cwidth(ch)
        if width + w > budget:
            break
        chars.append(ch)
        width += w
    return ellipsis + "".join(reversed(chars))


def get_bottom_toolbar_tokens(
    project_root: str,
    plan_mode: bool,
    active_model: str | None = None,
    extra_info: str | None = None,
    tokens: int = 0,
    turns: int = 0,
    mcp_count: int = 0,
) -> list[tuple[str, str]]:
    """Return prompt_toolkit FormattedText for bottom toolbar."""
    global _tip_index, _tip_last_rotate
    toolbar_tokens: list[tuple[str, str]] = []

    # Active Model badge
    if active_model:
        toolbar_tokens.append(("class:toolbar.model", f" {active_model} "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # Token Usage / Context Window %
    if active_model and tokens > 0:
        try:
            from coderai.cli.statusline import compute_token_gauge

            _, _, pct = compute_token_gauge(tokens, active_model)
            pct_str = f"{pct:.0f}%" if pct >= 1 or tokens == 0 else f"{pct:.1f}%"
            if tokens >= 1000:
                tok_str = f"{tokens / 1000:.1f}k ({pct_str})"
            else:
                tok_str = f"{tokens} ({pct_str})"
            toolbar_tokens.append(("class:toolbar.tokens", f" {tok_str} "))
            toolbar_tokens.append(("class:toolbar.sep", " · "))
        except Exception:
            toolbar_tokens.append(("class:toolbar.tokens", f" {tokens:,} tok "))
            toolbar_tokens.append(("class:toolbar.sep", " · "))

    # Git badge (delegates to centralized statusline caching)
    try:
        from coderai.cli.statusline import get_git_detailed_status

        branch, dirty, ahead, behind = get_git_detailed_status(project_root)
        if branch:
            badge = _format_git_badge(branch, dirty, ahead, behind)
            if HAS_PTK:
                from prompt_toolkit.utils import get_cwidth

                if sum(get_cwidth(c) for c in badge) > 22:
                    badge = _truncate_left(badge, 22)
            else:
                badge = badge[:22]
            toolbar_tokens.append(("class:toolbar.git", f" {badge} "))
            toolbar_tokens.append(("class:toolbar.sep", " · "))
    except Exception:
        pass

    # Plan / mode
    if plan_mode:
        toolbar_tokens.append(("class:toolbar.plan", " plan: ON "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # Turns count
    if turns > 0:
        toolbar_tokens.append(("class:toolbar.turns", f" turns: {turns} "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # MCP count
    if mcp_count > 0:
        toolbar_tokens.append(("class:toolbar.mcp", f" mcp: {mcp_count} "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # CWD (26 cols left-truncate)
    cwd = _shorten_cwd(project_root)
    cwd_disp = _truncate_left(cwd, 26)
    toolbar_tokens.append(("class:toolbar.cwd", f" {cwd_disp} "))

    # Tip rotation 30s
    now = time.monotonic()
    if now - _tip_last_rotate > 30:
        _tip_index = (_tip_index + 1) % len(_TIPS)
        _tip_last_rotate = now
    tip = _TIPS[_tip_index]
    toolbar_tokens.append(("class:toolbar.sep", " · "))
    toolbar_tokens.append(("class:toolbar.tip", f" {tip} "))
    if extra_info:
        toolbar_tokens.append(("class:toolbar.sep", " · "))
        toolbar_tokens.append(("", f" {extra_info} "))
    return toolbar_tokens


def _get_history_file(project_root: str) -> Path:
    """Per-workspace history via md5 (CoderAI completer canonical path)."""
    try:
        from coderai.cli.completer import get_history_file_path

        return get_history_file_path(project_root)
    except Exception:
        h = hashlib.md5(str(project_root).encode()).hexdigest()[:12]
        hist_dir = Path.home() / ".coderai" / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        return hist_dir / f"{h}.history"


# ---------------------------------------------------------------------------
# CoderAIPromptSession — main wrapper
# ---------------------------------------------------------------------------
if HAS_PTK:

    class CoderAIPromptSession:
        """Thin wrapper around PromptSession with CoderAI completers + toolbar."""

        def __init__(
            self,
            project_root: str,
            get_active_model: Any | None = None,
            plan_mode: bool = False,
            get_session_stats: Any | None = None,
            on_plan_mode_toggle: Any | None = None,
        ) -> None:
            self.project_root = project_root
            self.get_active_model = get_active_model
            self.plan_mode = plan_mode
            self.get_session_stats = get_session_stats
            self.on_plan_mode_toggle = on_plan_mode_toggle
            self._tokens: int = 0
            self._turns: int = 0
            self._mcp_count: int = 0

            # Build completers — pass canonical SlashCommand objects with aliases (Kimi prompt.py:97)
            slash_objs: list[Any]
            try:
                from coderai.cli.commands import _COMMANDS

                slash_objs = list(_COMMANDS)
            except Exception:
                try:
                    from coderai.cli.commands import completion_entries

                    slash_cmds = completion_entries()

                    class _Cmd:
                        def __init__(self, name: str, desc: str = ""):
                            self.name = name
                            self.description = desc
                            self.summary = desc
                            self.aliases: list[str] = []

                        def display_name(self, trigger: str | None = None) -> str:
                            if trigger and trigger != self.name and trigger in self.aliases:
                                return f"/{self.name} ({trigger})"
                            return f"/{self.name}"

                    slash_objs = [_Cmd(n.lstrip("/"), d) for n, d in slash_cmds]
                except Exception:
                    slash_objs = []

            self._slash_completer = SlashCommandCompleter(slash_objs, project_root=project_root)
            self._file_completer = FileMentionCompleter(project_root)
            self._completer = merge_completers(
                [self._slash_completer, self._file_completer], deduplicate=True
            )

            # History per-workspace
            hist_file = _get_history_file(project_root)
            # FileHistory expects file to exist; prompt_toolkit handles creation
            try:
                hist_file.touch(exist_ok=True)
            except Exception:
                pass
            self._history = FileHistory(str(hist_file))

            # Key bindings: ctrl-j -> newline, shift-tab -> toggle plan/build mode
            kb = KeyBindings()

            @kb.add("c-j")
            def _(event: Any) -> None:  # type: ignore
                event.current_buffer.insert_text("\n")

            @kb.add("escape", "enter")
            def _(event: Any) -> None:  # type: ignore
                event.current_buffer.insert_text("\n")

            @kb.add("s-tab")
            @kb.add("escape", "tab")
            @kb.add("escape", "[", "Z")
            def _toggle_plan_mode(event: Any) -> None:  # type: ignore
                self.plan_mode = not self.plan_mode
                if self.on_plan_mode_toggle and callable(self.on_plan_mode_toggle):
                    try:
                        self.on_plan_mode_toggle(self.plan_mode)
                    except Exception:
                        pass
                if hasattr(event, "app") and event.app is not None:
                    event.app.invalidate()

            self._kb = kb

            # Style — clean, modern, unhighlighted completion menu & palette
            self._style = Style.from_dict(
                {
                    # Toolbar
                    "toolbar": "bg:#1e1e2e #cdd6f4",
                    "toolbar.model": "bg:#1e1e2e #89dceb bold",
                    "toolbar.tokens": "bg:#1e1e2e #a6e3a1",
                    "toolbar.git": "bg:#1e1e2e #cba6f7",
                    "toolbar.plan": "bg:#1e1e2e #f9e2af bold",
                    "toolbar.turns": "bg:#1e1e2e #89b4fa",
                    "toolbar.mcp": "bg:#1e1e2e #94e2d5",
                    "toolbar.cwd": "bg:#1e1e2e #9399b2",
                    "toolbar.sep": "bg:#1e1e2e #585b70",
                    "toolbar.tip": "bg:#1e1e2e #7f849c italic",
                    # Prompt
                    "prompt": "bold",
                    "prompt.plan": "bold yellow",
                    # Completion menu styling (clean, flat, no text match highlights)
                    "completion-menu": "bg:#181825 #cdd6f4",
                    "completion-menu.completion": "bg:#181825 #cdd6f4",
                    "completion-menu.completion.current": "bg:#313244 #89b4fa bold",
                    "completion-menu.meta": "bg:#181825 #6c7086",
                    "completion-menu.meta.completion.current": "bg:#313244 #a6adc8",
                    "completion-menu.multi-column-meta": "bg:#181825 #6c7086",
                    "scrollbar.background": "bg:#181825",
                    "scrollbar.button": "bg:#45475a",
                    # Remove bright / underlined character highlights from fuzzy completions
                    "fuzzymatch.inside": "nobold nounderline",
                    "fuzzymatch.outside": "nobold nounderline",
                }
            )

            def _toolbar_callback() -> list[tuple[str, str]]:
                model = self.get_active_model() if self.get_active_model else None
                toks = self._tokens
                t_count = self._turns
                mcp_cnt = self._mcp_count
                if self.get_session_stats and callable(self.get_session_stats):
                    try:
                        st = self.get_session_stats()
                        if isinstance(st, dict):
                            toks = st.get("tokens", toks)
                            t_count = st.get("turns", t_count)
                            mcp_cnt = st.get("mcp_count", mcp_cnt)
                    except Exception:
                        pass
                return get_bottom_toolbar_tokens(
                    self.project_root,
                    self.plan_mode,
                    active_model=model,
                    tokens=toks,
                    turns=t_count,
                    mcp_count=mcp_cnt,
                )

            self._session: PromptSession[Any] = PromptSession(
                completer=self._completer,
                history=self._history,
                key_bindings=kb,
                style=self._style,
                complete_in_thread=True,
                complete_while_typing=True,
                bottom_toolbar=_toolbar_callback,  # type: ignore[arg-type]
            )

        def update_plan_mode(self, plan_mode: bool) -> None:
            self.plan_mode = plan_mode

        def update_session_stats(
            self,
            tokens: int | None = None,
            turns: int | None = None,
            mcp_count: int | None = None,
            plan_mode: bool | None = None,
        ) -> None:
            """Update dynamic stats rendered in the persistent bottom toolbar."""
            if tokens is not None:
                self._tokens = tokens
            if turns is not None:
                self._turns = turns
            if mcp_count is not None:
                self._mcp_count = mcp_count
            if plan_mode is not None:
                self.plan_mode = plan_mode

        def _get_prompt_message(self) -> list[tuple[str, str]]:
            """Return dynamic formatted prompt tokens based on active plan/build mode."""
            if self.plan_mode:
                return [("class:prompt.plan", "[plan] "), ("class:prompt", "❯ ")]
            return [("class:prompt", "❯ ")]

        async def prompt_async(self, message: Any = None) -> str:
            """Async prompt with styled message and live plan/build toggle support."""
            try:
                # Use patch_stdout to not interfere with Live (Kimi parity)
                from prompt_toolkit.patch_stdout import patch_stdout

                msg = self._get_prompt_message if message in (None, "❯ ", "[plan] ❯ ") else message
                with patch_stdout():
                    text = await self._session.prompt_async(msg)  # type: ignore[arg-type]
                    return text
            except (KeyboardInterrupt, EOFError):
                raise

        def prompt(self, message: Any = None) -> str:
            try:
                msg = self._get_prompt_message if message in (None, "❯ ", "[plan] ❯ ") else message
                return self._session.prompt(msg)  # type: ignore[arg-type]
            except (KeyboardInterrupt, EOFError):
                raise

        # For app.py to check availability
        @property
        def session(self) -> PromptSession:
            return self._session

else:

    class CoderAIPromptSession:  # type: ignore
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("prompt_toolkit not installed")


# ---------------------------------------------------------------------------
# Helper: read_user_turn with prompt_toolkit fallback
# ---------------------------------------------------------------------------
async def read_user_turn_ptk(
    prompt_text: str = "❯ ",
    project_root: str | None = None,
    get_active_model: Any | None = None,
    plan_mode: bool = False,
    session: CoderAIPromptSession | None = None,
    session_stats: dict[str, Any] | None = None,
) -> str:
    """Read a turn via PromptSession if available, else fallback to input() loop.

    Handles multiline: trailing \\, fences, triple quotes via is_multiline_incomplete.
    """
    from coderai.cli.input_engine import is_multiline_incomplete, normalize_multiline_input

    if not HAS_PTK or project_root is None or not os.isatty(1):
        # Fallback to legacy readline path (no prompt_toolkit)
        from coderai.cli.input_engine import read_user_turn

        # run in thread to not block event loop
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: read_user_turn(prompt_text))

    # PTK path
    if session is None:
        session = CoderAIPromptSession(project_root, get_active_model, plan_mode=plan_mode)
    else:
        session.update_plan_mode(plan_mode)

    if session_stats and isinstance(session_stats, dict):
        session.update_session_stats(
            tokens=session_stats.get("tokens"),
            turns=session_stats.get("turns"),
            mcp_count=session_stats.get("mcp_count"),
            plan_mode=plan_mode,
        )

    # First line
    try:
        first = await session.prompt_async(prompt_text)
    except (KeyboardInterrupt, EOFError):
        raise

    buf = [first]
    # Multiline continuation via same session but with continuation prompt
    while is_multiline_incomplete(buf):
        try:
            nxt = await session.prompt_async("... ")
            buf.append(nxt)
        except (KeyboardInterrupt, EOFError):
            break
    return normalize_multiline_input("\n".join(buf))


def is_ptk_available() -> bool:
    return HAS_PTK

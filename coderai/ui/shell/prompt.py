from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import md5
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast, override, runtime_checkable

HAS_PTK = True

from kaos.path import KaosPath
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyCompleter,
    WordCompleter,
    merge_completers,
)
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, has_focus, is_done
from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText, to_formatted_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.utils import get_cwidth
from pydantic import BaseModel, ValidationError

from coderai.llm import ModelCapability
from coderai.share import get_share_dir
from coderai.soul import StatusSnapshot, format_context_status
from coderai.ui.shell import placeholders as prompt_placeholders
from coderai.ui.shell.console import console
from coderai.ui.shell.placeholders import (
    PromptPlaceholderManager,
    normalize_pasted_text,
    sanitize_surrogates,
)
from coderai.ui.theme import get_prompt_style, get_toolbar_colors
from coderai.utils.clipboard import (
    grab_media_from_clipboard,
    is_clipboard_available,
    is_media_clipboard_available,
)
from coderai.utils.logging import logger
from coderai.utils.slashcmd import SlashCommand
from coderai.wire.types import ContentPart

AttachmentCache = prompt_placeholders.AttachmentCache
CachedAttachment = prompt_placeholders.CachedAttachment
_parse_attachment_kind = prompt_placeholders.parse_attachment_kind

PROMPT_SYMBOL = "✨"
PROMPT_SYMBOL_SHELL = "$"
PROMPT_SYMBOL_THINKING = "💫"
PROMPT_SYMBOL_PLAN = "📋"


class CwdLostError(OSError):
    """Raised when the working directory no longer exists (e.g. external drive unplugged)."""


T = TypeVar("T")


def fuzzy_score(query: str, candidate: str) -> tuple[bool, int]:
    """Calculate whether query is a subsequence of candidate and compute a ranking score.

    Returns:
        tuple[bool, int]: (is_match, score)
    """
    if not query:
        return True, 0

    q_lower = query.lower()
    c_lower = candidate.lower()

    # Exact match bonus
    if q_lower == c_lower:
        return True, 10000

    # Prefix match bonus
    if c_lower.startswith(q_lower):
        return True, 5000 + (len(q_lower) * 20) - len(c_lower)

    # Substring match bonus
    if q_lower in c_lower:
        idx = c_lower.find(q_lower)
        score = 2000 - (idx * 10) + (len(q_lower) * 20) - len(c_lower)
        return True, score

    # Subsequence matching
    score = 0
    q_idx = 0
    q_len = len(q_lower)
    c_len = len(c_lower)
    prev_c_idx = -2

    for c_idx, char in enumerate(c_lower):
        if q_idx < q_len and char == q_lower[q_idx]:
            # Base match points
            score += 15

            # Consecutive character match bonus
            if c_idx == prev_c_idx + 1:
                score += 30

            # Word boundary bonus (start of word or following separator)
            if c_idx == 0:
                score += 50
            elif candidate[c_idx - 1] in ("/", "\\", "_", "-", ".", " ", ":"):
                score += 40
            elif candidate[c_idx].isupper() and not candidate[c_idx - 1].isupper():
                score += 35

            prev_c_idx = c_idx
            q_idx += 1

    if q_idx == q_len:
        # Full subsequence matched; apply slight penalty for length distance
        score -= c_len
        return True, score

    return False, 0


def fuzzy_filter(
    query: str,
    candidates: list[T],
    key_func: Callable[[T], str] | None = None,
    limit: int = 15,
) -> list[T]:
    """Filter and rank candidates using fuzzy matching score."""
    if not query:
        return candidates[:limit]

    scored: list[tuple[int, T]] = []
    for item in candidates:
        text = key_func(item) if key_func is not None else str(item)
        matched, score = fuzzy_score(query, text)
        if matched:
            scored.append((score, item))

    # Sort descending by score
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


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
                from coderai.ui.shell.session_picker import CURATED_MODELS

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

            elif lead_cmd in ("/agent", "/role"):
                try:
                    from coderai.subagents.agent_spec import discover_agent_specs

                    discovered = discover_agent_specs(Path(self.project_root))
                    all_roles = ["default", "okabe"] + [s.name for s in discovered]
                    for role_name in all_roles:
                        candidates.append((role_name, f"Agent role: {role_name}"))
                except Exception:
                    for role_name in (
                        "default",
                        "okabe",
                        "architect",
                        "code-reviewer",
                        "planner",
                        "security-reviewer",
                        "tdd-guide",
                        "build-error-resolver",
                    ):
                        candidates.append((role_name, f"Agent role: {role_name}"))

            elif lead_cmd in ("/skill",) or lead_cmd.startswith(("/skill:", "/flow:")):
                from coderai.skill import list_skills

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
        # Case 3: /skill:<name> + /flow:<name> colon dispatch (Kimi parity).
        if typed.startswith(("skill:", "flow:")):
            prefix, _, partial = typed.partition(":")
            try:
                from coderai.skill import list_skills

                skills = list_skills(self.project_root)
                names = [
                    str(sk.get("name"))
                    for sk in skills
                    if isinstance(sk, dict) and sk.get("name")
                ]
            except Exception:
                names = []
            try:
                for name in fuzzy_filter(partial, names, limit=15):
                    yield Completion(
                        text=f"/{prefix}:{name}",
                        start_position=-len(token),
                        display=f"/{prefix}:{name}",
                        display_meta=f"{prefix} skill: {name}",
                    )
            except Exception:
                pass


def _truncate_to_width(text: str, width: int) -> str:
    if width <= 0:
        return ""

    total = 0
    chars: list[str] = []
    for ch in text:
        ch_width = get_cwidth(ch)
        if total + ch_width > width:
            break
        chars.append(ch)
        total += ch_width

    if total == get_cwidth(text):
        return text + (" " * max(0, width - total))

    ellipsis = "..."
    ellipsis_width = get_cwidth(ellipsis)
    if width <= ellipsis_width:
        return "." * width

    available = width - ellipsis_width
    total = 0
    chars = []
    for ch in text:
        ch_width = get_cwidth(ch)
        if total + ch_width > available:
            break
        chars.append(ch)
        total += ch_width
    return "".join(chars) + ellipsis + (" " * max(0, width - total - ellipsis_width))


def _wrap_to_width(text: str, width: int, *, max_lines: int | None = None) -> list[str]:
    if width <= 0:
        return []

    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current_words: list[str] = []
    current_width = 0
    index = 0

    while index < len(words):
        word = words[index]
        word_width = get_cwidth(word)
        separator_width = 1 if current_words else 0

        if current_words and current_width + separator_width + word_width <= width:
            current_words.append(word)
            current_width += separator_width + word_width
            index += 1
            continue

        if not current_words and word_width <= width:
            current_words.append(word)
            current_width = word_width
            index += 1
            continue

        if not current_words and word_width > width:
            current_words.append(_truncate_to_width(word, width).rstrip())
            current_width = get_cwidth(current_words[0])
            index += 1

        lines.append(" ".join(current_words))
        current_words = []
        current_width = 0

        if max_lines is not None and len(lines) == max_lines:
            remaining = " ".join(words[index:])
            if remaining:
                prefix = f"{lines[-1]} " if lines[-1] else ""
                lines[-1] = _truncate_to_width(prefix + remaining, width).rstrip()
            return lines

    if current_words:
        line = " ".join(current_words)
        if max_lines is not None and len(lines) + 1 > max_lines:
            if lines:
                lines[-1] = _truncate_to_width(f"{lines[-1]} {line}", width).rstrip()
            else:
                lines.append(_truncate_to_width(line, width).rstrip())
        else:
            lines.append(line)

    return lines


def _find_prompt_float_container(layout_container: object) -> FloatContainer | None:
    if not isinstance(layout_container, HSplit):
        return None

    for child in cast(Sequence[object], layout_container.children):
        float_container = _extract_float_container(child)
        if float_container is not None:
            return float_container
    return None


def _extract_float_container(container: object) -> FloatContainer | None:
    if isinstance(container, FloatContainer):
        return container
    if isinstance(container, ConditionalContainer):
        if isinstance(container.content, FloatContainer):
            return container.content
        if isinstance(container.alternative_content, FloatContainer):
            return container.alternative_content
    return None


def _find_default_buffer_container(
    layout_container: object,
    target_buffer: Buffer,
) -> ConditionalContainer | None:
    seen: set[int] = set()

    def _walk(node: object) -> ConditionalContainer | None:
        if id(node) in seen:
            return None
        seen.add(id(node))

        if isinstance(node, ConditionalContainer):
            content = getattr(node, "content", None)
            if isinstance(content, Window):
                control = content.content
                if isinstance(control, BufferControl) and control.buffer is target_buffer:
                    return node

        if isinstance(node, DynamicContainer):
            with contextlib.suppress(Exception):
                found = _walk(node.get_container())
                if found is not None:
                    return found

        for attr in ("children", "content", "floats", "container"):
            if not hasattr(node, attr):
                continue
            value = getattr(node, attr)
            if attr == "children" and isinstance(value, Sequence):
                for child in value:  # pyright: ignore[reportUnknownVariableType]
                    found = _walk(child)  # pyright: ignore[reportUnknownArgumentType]
                    if found is not None:
                        return found
            elif attr == "floats" and isinstance(value, Sequence):
                for float_ in value:  # pyright: ignore[reportUnknownVariableType]
                    content = getattr(float_, "content", None)  # pyright: ignore[reportUnknownArgumentType]
                    if content is None:
                        continue
                    found = _walk(content)
                    if found is not None:
                        return found
            elif (
                attr in {"content", "container"}
                and value is not None
                and type(value).__module__.startswith("prompt_toolkit")
            ):
                found = _walk(value)
                if found is not None:
                    return found
        return None

    return _walk(layout_container)


class SlashCommandMenuControl(UIControl):
    """Render slash command completions as a full-width menu that matches the shell UI."""

    _MAX_EXPANDED_META_LINES = 3

    def __init__(
        self,
        *,
        left_padding: Callable[[], int],
        scroll_offset: int = 1,
    ) -> None:
        self._left_padding = left_padding
        self._scroll_offset = scroll_offset

    def has_focus(self) -> bool:
        return False

    def preferred_width(self, max_available_width: int) -> int | None:
        return max_available_width

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Callable[..., AnyFormattedText] | None,
    ) -> int | None:
        app = get_app_or_none()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None:
            return 0
        completions = complete_state.completions
        selected_index = complete_state.complete_index
        if selected_index is None:
            return min(max_available_height, len(completions) + 1)
        menu_width = max(0, width - self._left_padding())
        marker_width = 2
        command_width = self._command_column_width(completions, menu_width, marker_width)
        gap_width = 3 if menu_width > command_width + 6 else 1
        meta_width = max(0, menu_width - marker_width - command_width - gap_width)
        selected_meta_lines = self._selected_meta_lines(
            completions[selected_index].display_meta_text,
            meta_width,
        )
        return min(max_available_height, len(completions) + len(selected_meta_lines))

    def create_content(self, width: int, height: int) -> UIContent:
        app = get_app_or_none()
        complete_state = (
            getattr(app.current_buffer, "complete_state", None) if app is not None else None
        )
        if complete_state is None or not complete_state.completions:
            return UIContent()

        completions = complete_state.completions
        selected_index = complete_state.complete_index
        available_rows = max(1, height - 1)

        menu_width = max(0, width - self._left_padding())
        marker_width = 2
        command_width = self._command_column_width(completions, menu_width, marker_width)
        gap_width = 3 if menu_width > command_width + 6 else 1
        meta_width = max(0, menu_width - marker_width - command_width - gap_width)

        rendered_lines: list[FormattedText] = [
            FormattedText([("class:slash-completion-menu.separator", "─" * max(0, width))])
        ]
        selected_line_index = 0

        if selected_index is None:
            end = min(len(completions) - 1, available_rows - 1)
            for index in range(0, end + 1):
                rendered_lines.append(
                    self._render_single_line_item(
                        width=width,
                        completion=completions[index],
                        marker_width=marker_width,
                        command_width=command_width,
                        meta_width=meta_width,
                        gap_width=gap_width,
                        is_current=False,
                    )
                )

            return UIContent(
                get_line=lambda i: rendered_lines[i],
                line_count=len(rendered_lines),
                cursor_position=Point(x=0, y=selected_line_index),
            )

        selected_meta_lines = self._selected_meta_lines(
            completions[selected_index].display_meta_text,
            meta_width,
        )
        start, end = self._visible_window_bounds(
            completion_count=len(completions),
            selected_index=selected_index,
            available_rows=available_rows,
            selected_item_height=len(selected_meta_lines),
        )
        selected_line_index = 1

        for index in range(start, end + 1):
            completion = completions[index]
            if index == selected_index:
                selected_line_index = len(rendered_lines)
                rendered_lines.extend(
                    self._render_selected_item_lines(
                        width=width,
                        completion=completion,
                        marker_width=marker_width,
                        command_width=command_width,
                        meta_width=meta_width,
                        gap_width=gap_width,
                        meta_lines=selected_meta_lines,
                    )
                )
                continue

            rendered_lines.append(
                self._render_single_line_item(
                    width=width,
                    completion=completion,
                    marker_width=marker_width,
                    command_width=command_width,
                    meta_width=meta_width,
                    gap_width=gap_width,
                    is_current=False,
                )
            )

        return UIContent(
            get_line=lambda i: rendered_lines[i],
            line_count=len(rendered_lines),
            cursor_position=Point(x=0, y=selected_line_index),
        )

    def _selected_meta_lines(self, text: str, meta_width: int) -> list[str]:
        lines = _wrap_to_width(
            text,
            meta_width,
            max_lines=self._MAX_EXPANDED_META_LINES,
        )
        return lines or [""]

    def _visible_window_bounds(
        self,
        *,
        completion_count: int,
        selected_index: int,
        available_rows: int,
        selected_item_height: int,
    ) -> tuple[int, int]:
        selected_item_height = min(selected_item_height, available_rows)
        remaining_rows = max(0, available_rows - selected_item_height)

        before = min(self._scroll_offset, selected_index, remaining_rows)
        remaining_rows -= before
        after = min(completion_count - selected_index - 1, remaining_rows)
        remaining_rows -= after

        extra_before = min(selected_index - before, remaining_rows)
        before += extra_before
        remaining_rows -= extra_before

        extra_after = min(completion_count - selected_index - 1 - after, remaining_rows)
        after += extra_after

        return selected_index - before, selected_index + after

    def _command_column_width(
        self,
        completions: Sequence[Completion],
        menu_width: int,
        marker_width: int,
    ) -> int:
        if menu_width <= 0:
            return 0
        longest = max((get_cwidth(c.display_text) for c in completions), default=0)
        preferred = longest + 2
        usable_width = max(0, menu_width - marker_width)
        minimum = min(usable_width, 18)
        maximum = max(minimum, min(28, usable_width // 2))
        return max(minimum, min(preferred, maximum))

    def _render_single_line_item(
        self,
        *,
        width: int,
        completion: Completion,
        marker_width: int,
        command_width: int,
        meta_width: int,
        gap_width: int,
        is_current: bool,
    ) -> FormattedText:
        padding_width = max(0, width - marker_width - command_width - meta_width - gap_width)
        left_padding = min(self._left_padding(), padding_width)
        trailing_width = max(
            0,
            width - left_padding - marker_width - command_width - gap_width - meta_width,
        )

        command_style = (
            "class:slash-completion-menu.command.current"
            if is_current
            else "class:slash-completion-menu.command"
        )
        meta_style = (
            "class:slash-completion-menu.meta.current"
            if is_current
            else "class:slash-completion-menu.meta"
        )
        marker_style = (
            "class:slash-completion-menu.marker.current"
            if is_current
            else "class:slash-completion-menu.marker"
        )
        marker = "› " if is_current else "  "

        fragments: FormattedText = FormattedText()
        fragments.append(("class:slash-completion-menu", " " * left_padding))
        fragments.append((marker_style, marker.ljust(marker_width)))
        fragments.append(
            (command_style, _truncate_to_width(completion.display_text, command_width))
        )
        fragments.append(("class:slash-completion-menu", " " * gap_width))
        fragments.append((meta_style, _truncate_to_width(completion.display_meta_text, meta_width)))
        fragments.append(("class:slash-completion-menu", " " * trailing_width))
        return fragments

    def _render_selected_item_lines(
        self,
        *,
        width: int,
        completion: Completion,
        marker_width: int,
        command_width: int,
        meta_width: int,
        gap_width: int,
        meta_lines: Sequence[str],
    ) -> list[FormattedText]:
        lines = [
            self._render_single_line_item(
                width=width,
                completion=Completion(
                    text=completion.text,
                    start_position=completion.start_position,
                    display=completion.display,
                    display_meta=meta_lines[0],
                ),
                marker_width=marker_width,
                command_width=command_width,
                meta_width=meta_width,
                gap_width=gap_width,
                is_current=True,
            )
        ]

        continuation_prefix = (
            " " * self._left_padding() + " " * marker_width + " " * command_width + " " * gap_width
        )
        continuation_trailing = max(
            0,
            width - get_cwidth(continuation_prefix) - meta_width,
        )
        for meta_line in meta_lines[1:]:
            fragments: FormattedText = FormattedText()
            fragments.append(("class:slash-completion-menu", continuation_prefix))
            fragments.append(
                (
                    "class:slash-completion-menu.meta.current",
                    _truncate_to_width(meta_line, meta_width),
                )
            )
            fragments.append(("class:slash-completion-menu", " " * continuation_trailing))
            lines.append(fragments)

        return lines


class LocalFileMentionCompleter(Completer):
    """Offer fuzzy `@` path completion by indexing workspace files.

    File discovery and ignore rules are delegated to
    :mod:`coderai.utils.file_filter` so that the web backend can reuse
    them.
    """

    _FRAGMENT_PATTERN = re.compile(r"[^\s@]+")
    _TRIGGER_GUARDS = frozenset((".", "-", "_", "`", "'", '"', ":", "@", "#", "~"))

    def __init__(
        self,
        root: Path,
        *,
        refresh_interval: float = 2.0,
        limit: int = 1000,
    ) -> None:
        self._root = root
        self._refresh_interval = refresh_interval
        self._limit = limit
        self._cache_time: float = 0.0
        self._cached_paths: list[str] = []
        self._cache_scope: str | None = None
        self._top_cache_time: float = 0.0
        self._top_cached_paths: list[str] = []
        self._fragment_hint: str | None = None
        self._is_git: bool | None = None  # lazily detected
        self._git_index_mtime: float | None = None

        self._word_completer = WordCompleter(
            self._get_paths,
            WORD=False,
            pattern=self._FRAGMENT_PATTERN,
        )

        self._fuzzy = FuzzyCompleter(
            self._word_completer,
            WORD=False,
            pattern=r"^[^\s@]*",
        )

    def _get_paths(self) -> list[str]:
        fragment = self._fragment_hint or ""
        if "/" not in fragment and len(fragment) < 3:
            return self._get_top_level_paths()
        return self._get_deep_paths()

    def _get_top_level_paths(self) -> list[str]:
        from coderai.utils.file_filter import is_ignored

        now = time.monotonic()
        if now - self._top_cache_time <= self._refresh_interval:
            return self._top_cached_paths

        entries: list[str] = []
        try:
            for entry in sorted(self._root.iterdir(), key=lambda p: p.name):
                name = entry.name
                if is_ignored(name):
                    continue
                entries.append(f"{name}/" if entry.is_dir() else name)
                if len(entries) >= self._limit:
                    break
        except OSError:
            return self._top_cached_paths

        self._top_cached_paths = entries
        self._top_cache_time = now
        return self._top_cached_paths

    def _get_deep_paths(self) -> list[str]:
        from coderai.utils.file_filter import (
            detect_git,
            git_index_mtime,
            list_files_git,
            list_files_walk,
        )

        fragment = self._fragment_hint or ""

        scope: str | None = None
        if "/" in fragment:
            scope = fragment.rsplit("/", 1)[0]

        now = time.monotonic()
        cache_valid = (
            now - self._cache_time <= self._refresh_interval and self._cache_scope == scope
        )

        # Invalidate on .git/index mtime change (like Claude Code).
        if cache_valid and self._is_git:
            mtime = git_index_mtime(self._root)
            if mtime != self._git_index_mtime:
                cache_valid = False

        if cache_valid:
            return self._cached_paths

        if self._is_git is None:
            self._is_git = detect_git(self._root)

        paths: list[str] | None = None
        if self._is_git:
            paths = list_files_git(self._root, scope)
            self._git_index_mtime = git_index_mtime(self._root)
        if paths is None:
            paths = list_files_walk(self._root, scope, limit=self._limit)

        self._cached_paths = paths
        self._cache_scope = scope
        self._cache_time = now
        return self._cached_paths

    @staticmethod
    def _extract_fragment(text: str) -> str | None:
        index = text.rfind("@")
        if index == -1:
            return None

        if index > 0:
            prev = text[index - 1]
            if prev.isalnum() or prev in LocalFileMentionCompleter._TRIGGER_GUARDS:
                return None

        fragment = text[index + 1 :]
        if not fragment:
            return ""

        if any(ch.isspace() for ch in fragment):
            return None

        return fragment

    def _is_completed_file(self, fragment: str) -> bool:
        candidate = fragment.rstrip("/")
        if not candidate:
            return False
        try:
            return (self._root / candidate).is_file()
        except OSError:
            return False

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        fragment = self._extract_fragment(document.text_before_cursor)
        if fragment is None:
            return
        if self._is_completed_file(fragment):
            return

        mention_doc = Document(text=fragment, cursor_position=len(fragment))
        self._fragment_hint = fragment
        try:
            # First, ask the fuzzy completer for candidates.
            candidates = list(self._fuzzy.get_completions(mention_doc, complete_event))

            # re-rank: prefer basename matches
            frag_lower = fragment.lower()

            def _rank(c: Completion) -> tuple[int, ...]:
                path = c.text
                base = path.rstrip("/").split("/")[-1].lower()
                if base.startswith(frag_lower):
                    cat = 0
                elif frag_lower in base:
                    cat = 1
                else:
                    cat = 2
                # preserve original FuzzyCompleter's order in the same category
                return (cat,)

            candidates.sort(key=_rank)
            yield from candidates
        finally:
            self._fragment_hint = None


class _HistoryEntry(BaseModel):
    content: str


def _load_history_entries(history_file: Path) -> list[_HistoryEntry]:
    entries: list[_HistoryEntry] = []
    if not history_file.exists():
        return entries

    try:
        with history_file.open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse user history line; skipping: {line}",
                        line=line,
                    )
                    continue
                try:
                    entry = _HistoryEntry.model_validate(record)
                    entries.append(entry)
                except ValidationError:
                    logger.warning(
                        "Failed to validate user history entry; skipping: {line}",
                        line=line,
                    )
                    continue
    except OSError as exc:
        logger.warning(
            "Failed to load user history file: {file} ({error})",
            file=history_file,
            error=exc,
        )

    return entries


class PromptMode(Enum):
    AGENT = "agent"
    SHELL = "shell"

    def toggle(self) -> PromptMode:
        return PromptMode.SHELL if self == PromptMode.AGENT else PromptMode.AGENT

    def __str__(self) -> str:
        return self.value


class PromptUIState(Enum):
    NORMAL_INPUT = "normal_input"
    MODAL_HIDDEN_INPUT = "modal_hidden_input"
    MODAL_TEXT_INPUT = "modal_text_input"


class UserInput(BaseModel):
    mode: PromptMode
    command: str
    """The plain text representation of the user input."""
    resolved_command: str
    """The text command after UI-only placeholders are expanded."""
    content: list[ContentPart]
    """The rich content parts."""

    def __str__(self) -> str:
        return self.command

    def __bool__(self) -> bool:
        return bool(self.command)


_IDLE_REFRESH_INTERVAL = 1.0
_RUNNING_REFRESH_INTERVAL = 0.1

_GIT_BRANCH_TTL = 5.0
_GIT_STATUS_TTL = 15.0
_TIP_ROTATE_INTERVAL = 30.0
_MAX_CWD_COLS = 30
_MAX_BRANCH_COLS = 22


@dataclass
class _GitBranchState:
    timestamp: float = 0.0
    branch: str | None = None
    proc: subprocess.Popen[str] | None = None


@dataclass
class _GitStatusState:
    timestamp: float = 0.0
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    proc: subprocess.Popen[str] | None = None


_git_branch_state = _GitBranchState()
_git_status_state = _GitStatusState()

_GIT_STATUS_AB_RE = re.compile(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]")


def _get_git_branch() -> str | None:
    """Return the current git branch name via a non-blocking cached subprocess."""
    state = _git_branch_state
    now = time.monotonic()

    # Collect result if a previously launched process has finished
    if state.proc is not None:
        returncode = state.proc.poll()
        if returncode is not None:
            try:
                stdout, _ = state.proc.communicate()
                new_branch = stdout.strip() or None
                # Branch changed — discard any in-flight status subprocess so it cannot
                # write stale results for the old branch, then force an immediate refresh.
                if new_branch != state.branch:
                    if _git_status_state.proc is not None:
                        with contextlib.suppress(Exception):
                            _git_status_state.proc.terminate()
                        _git_status_state.proc = None
                    _git_status_state.timestamp = 0.0
                state.branch = new_branch
            except Exception:
                state.branch = None
            state.proc = None

    # Launch a new process when the TTL has expired and nothing is running
    if state.timestamp + _GIT_BRANCH_TTL <= now and state.proc is None:
        state.timestamp = now
        try:
            state.proc = subprocess.Popen(
                ["git", "branch", "--show-current"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            state.branch = None

    return state.branch


def _get_git_status() -> tuple[bool, int, int]:
    """Return (dirty, ahead, behind) via a non-blocking cached subprocess.

    Runs ``git status --porcelain -b`` (includes untracked files so newly created
    files show as dirty).  TTL is longer than the branch check because file-tree
    scanning is expensive.
    """
    state = _git_status_state
    now = time.monotonic()

    if state.proc is not None:
        returncode = state.proc.poll()
        if returncode is not None:
            try:
                stdout, _ = state.proc.communicate()
                dirty = False
                ahead = 0
                behind = 0
                for line in stdout.splitlines():
                    if line.startswith("## "):
                        m = _GIT_STATUS_AB_RE.search(line)
                        if m:
                            ahead = int(m.group(1) or 0)
                            behind = int(m.group(2) or 0)
                    elif line.strip():
                        dirty = True
                state.dirty = dirty
                state.ahead = ahead
                state.behind = behind
            except Exception:
                pass
            state.proc = None
        elif now - state.timestamp > _GIT_STATUS_TTL:
            # Subprocess is stuck (e.g. OS pipe buffer full from many untracked files).
            # Terminate it so the toolbar is not permanently frozen; retry after next TTL.
            with contextlib.suppress(Exception):
                state.proc.terminate()
            state.proc = None
            state.timestamp = now  # delay next spawn by one full TTL

    if state.timestamp + _GIT_STATUS_TTL <= now and state.proc is None:
        state.timestamp = now
        with contextlib.suppress(Exception):
            state.proc = subprocess.Popen(
                ["git", "status", "--porcelain", "-b"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

    return state.dirty, state.ahead, state.behind


def _format_git_badge(branch: str, dirty: bool, ahead: int, behind: int) -> str:
    """Format branch name with an optional status badge: ``main [± ↑3↓1]``."""
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
    """Replace the home directory prefix in *path* with ``~``."""
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _display_width(text: str) -> int:
    """Return the terminal column width of *text*, handling wide Unicode characters."""
    return sum(get_cwidth(c) for c in text)


def _truncate_left(text: str, max_cols: int) -> str:
    """Truncate *text* from the left, prepending '…' if it exceeds *max_cols*."""
    if max_cols <= 0:
        return ""
    if _display_width(text) <= max_cols:
        return text
    ellipsis = "…"
    budget = max_cols - _display_width(ellipsis)
    chars: list[str] = []
    width = 0
    for ch in reversed(text):
        w = get_cwidth(ch)
        if width + w > budget:
            break
        chars.append(ch)
        width += w
    return ellipsis + "".join(reversed(chars))


def _truncate_right(text: str, max_cols: int) -> str:
    """Truncate *text* from the right, appending '…' if it exceeds *max_cols*."""
    if max_cols <= 0:
        return ""
    if _display_width(text) <= max_cols:
        return text
    ellipsis = "…"
    budget = max_cols - _display_width(ellipsis)
    chars: list[str] = []
    width = 0
    for ch in text:
        w = get_cwidth(ch)
        if width + w > budget:
            break
        chars.append(ch)
        width += w
    return "".join(chars) + ellipsis


@dataclass(slots=True)
class _ToastEntry:
    topic: str | None
    """There can be only one toast of each non-None topic in the queue."""
    message: str
    expires_at: float


class RunningPromptDelegate(Protocol):
    """Protocol for components that can take over the bottom prompt area."""

    modal_priority: int

    def render_running_prompt_body(self, columns: int) -> AnyFormattedText: ...

    def running_prompt_placeholder(self) -> AnyFormattedText | None: ...

    def running_prompt_allows_text_input(self) -> bool: ...

    def running_prompt_hides_input_buffer(self) -> bool: ...

    def running_prompt_accepts_submission(self) -> bool: ...

    def should_handle_running_prompt_key(self, key: str) -> bool: ...

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class BgTaskCounts:
    bash: int = 0
    agent: int = 0


@runtime_checkable
class AgentStatusProvider(Protocol):
    """Optional protocol for delegates that render always-visible agent status.

    When the running prompt delegate implements this, ``_render_agent_status``
    will call ``render_agent_status`` instead of the fallback status block.
    This ensures spinners, content blocks, and tool calls remain visible
    even when a modal (approval/question/btw) is active.
    """

    def render_agent_status(self, columns: int) -> AnyFormattedText: ...


_toast_queues: dict[Literal["left", "right"], deque[_ToastEntry]] = {
    "left": deque(),
    "right": deque(),
}
"""The queue of toasts to show, including the one currently being shown (the first one)."""


def toast(
    message: str,
    duration: float = 5.0,
    topic: str | None = None,
    immediate: bool = False,
    position: Literal["left", "right"] = "left",
) -> None:
    queue = _toast_queues[position]
    duration = max(duration, _IDLE_REFRESH_INTERVAL)
    entry = _ToastEntry(topic=topic, message=message, expires_at=time.monotonic() + duration)
    if topic is not None:
        # Remove existing toasts with the same topic
        for existing in list(queue):
            if existing.topic == topic:
                queue.remove(existing)
    if immediate:
        queue.appendleft(entry)
    else:
        queue.append(entry)


def _current_toast(position: Literal["left", "right"] = "left") -> _ToastEntry | None:
    queue = _toast_queues[position]
    now = time.monotonic()
    while queue and queue[0].expires_at <= now:
        queue.popleft()
    if not queue:
        return None
    return queue[0]


def _build_toolbar_tips(clipboard_available: bool) -> list[str]:
    tips = [
        "ctrl-x: toggle mode",
        "shift-tab: plan mode",
        "ctrl-o: editor",
        "ctrl-j: newline",
        "/feedback: send feedback",
        "/theme: switch dark/light",
    ]
    if clipboard_available:
        tips.append("ctrl-v: paste clipboard")
    tips.append("@: mention files")
    return tips


_TIP_SEPARATOR = " | "


class CustomPromptSession:
    def __init__(
        self,
        *,
        status_provider: Callable[[], StatusSnapshot],
        status_block_provider: Callable[[int], AnyFormattedText | None] | None = None,
        fast_refresh_provider: Callable[[], bool] | None = None,
        background_task_count_provider: Callable[[], BgTaskCounts] | None = None,
        model_capabilities: set[ModelCapability],
        model_name: str | None,
        thinking: bool,
        agent_mode_slash_commands: Sequence[SlashCommand[Any]],
        shell_mode_slash_commands: Sequence[SlashCommand[Any]],
        editor_command_provider: Callable[[], str] = lambda: "",
        plan_mode_toggle_callback: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        history_dir = get_share_dir() / "user-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        work_dir_id = md5(str(KaosPath.cwd()).encode(encoding="utf-8")).hexdigest()
        self._history_file = (history_dir / work_dir_id).with_suffix(".jsonl")
        self._status_provider = status_provider
        self._status_block_provider = status_block_provider
        self._fast_refresh_provider = fast_refresh_provider
        self._background_task_count_provider = background_task_count_provider
        self._editor_command_provider = editor_command_provider
        self._plan_mode_toggle_callback = plan_mode_toggle_callback
        self._model_capabilities = model_capabilities
        self._model_name = model_name
        self._last_history_content: str | None = None
        self._mode: PromptMode = PromptMode.AGENT
        self._thinking = thinking
        self._placeholder_manager = PromptPlaceholderManager()
        # Keep the old attribute for test compatibility and for any external imports.
        self._attachment_cache = self._placeholder_manager.attachment_cache
        self._last_tip_rotate_time: float = time.monotonic()
        self._last_submission_was_running = False
        self._last_input_activity_time: float = 0.0
        self._suppress_auto_completion: bool = False
        self._input_activity_event: asyncio.Event = asyncio.Event()
        self._running_prompt_previous_mode: PromptMode | None = None
        self._running_prompt_delegate: RunningPromptDelegate | None = None
        self._modal_delegates: list[RunningPromptDelegate] = []
        self._prompt_buffer_container: ConditionalContainer | None = None
        self._last_ui_state: PromptUIState = PromptUIState.NORMAL_INPUT
        self._suspended_buffer_document: Document | None = None
        clipboard_available = is_clipboard_available()
        media_clipboard_available = is_media_clipboard_available()
        self._tips = _build_toolbar_tips(clipboard_available or media_clipboard_available)
        self._tip_rotation_index: int = random.randrange(len(self._tips)) if self._tips else 0

        history_entries = _load_history_entries(self._history_file)
        history = InMemoryHistory()
        for entry in history_entries:
            history.append_string(entry.content)

        if history_entries:
            # for consecutive deduplication
            self._last_history_content = history_entries[-1].content

        # Build completers
        self._agent_mode_completer = merge_completers(
            [
                SlashCommandCompleter(agent_mode_slash_commands),
                # TODO(kaos): we need an async KaosFileMentionCompleter
                LocalFileMentionCompleter(KaosPath.cwd().unsafe_to_local_path()),
            ],
            deduplicate=True,
        )
        self._shell_mode_completer = SlashCommandCompleter(shell_mode_slash_commands)

        # Build key bindings
        _kb = KeyBindings()

        def _accept_completion(buff: Buffer) -> None:
            """Accept the current or first completion, suppressing re-completion."""
            completion = buff.complete_state.current_completion  # type: ignore[union-attr]
            if not completion:
                completion = buff.complete_state.completions[0]  # type: ignore[union-attr]
            self._suppress_auto_completion = True
            try:
                buff.apply_completion(completion)
            finally:
                self._suppress_auto_completion = False

        def _is_slash_completion() -> bool:
            """True when the active completion menu is for a slash command."""
            buff = self._session.default_buffer
            return bool(
                buff.complete_state
                and buff.complete_state.completions
                and SlashCommandCompleter.should_complete(buff.document)
            )

        _slash_completion_filter = has_completions & Condition(_is_slash_completion)
        _non_slash_completion_filter = has_completions & ~Condition(_is_slash_completion)

        @_kb.add("enter", filter=_slash_completion_filter)
        def _(event: KeyPressEvent) -> None:
            """Slash command completion: accept and submit in one step."""
            _accept_completion(event.current_buffer)
            event.current_buffer.validate_and_handle()

        @_kb.add("enter", filter=_non_slash_completion_filter)
        def _(event: KeyPressEvent) -> None:
            """Non-slash completion (file mentions, etc.): accept only."""
            _accept_completion(event.current_buffer)

        @_kb.add("c-x", eager=True)
        def _(event: KeyPressEvent) -> None:
            if self._active_prompt_delegate() is not None:
                return
            self._mode = self._mode.toggle()
            from coderai.telemetry import track

            track("shortcut_mode_switch", to_mode=self._mode.value)
            # Apply mode-specific settings
            self._apply_mode(event)
            # Redraw UI
            event.app.invalidate()

        @_kb.add("s-tab", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Toggle plan mode with Shift+Tab."""
            if self._active_prompt_delegate() is not None:
                return
            if self._plan_mode_toggle_callback is not None:

                async def _toggle() -> None:
                    assert self._plan_mode_toggle_callback is not None
                    new_state = await self._plan_mode_toggle_callback()
                    from coderai.telemetry import track

                    track("shortcut_plan_toggle", enabled=new_state)
                    if new_state:
                        toast("plan mode ON", topic="plan_mode", duration=3.0, immediate=True)
                    else:
                        toast("plan mode OFF", topic="plan_mode", duration=3.0, immediate=True)
                    event.app.invalidate()

                event.app.create_background_task(_toggle())
            event.app.invalidate()

        @_kb.add("escape", "enter", eager=True)
        @_kb.add("c-j", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Insert a newline when Alt-Enter or Ctrl-J is pressed."""
            from coderai.telemetry import track

            track("shortcut_newline")
            event.current_buffer.insert_text("\n")

        @_kb.add("c-o", eager=True)
        def _(event: KeyPressEvent) -> None:
            """Open current buffer in external editor."""
            from coderai.telemetry import track

            track("shortcut_editor")
            self._open_in_external_editor(event)

        @_kb.add(
            "up",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("up")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("up", event)

        @_kb.add(
            "down",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("down")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("down", event)

        @_kb.add(
            "left",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("left")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("left", event)

        @_kb.add(
            "right",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("right")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("right", event)

        @_kb.add(
            "tab",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("tab")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("tab", event)

        @_kb.add(
            "enter",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("enter")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("enter", event)

        @_kb.add(
            "space",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("space")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("space", event)

        @_kb.add(
            "c-s",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-s")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-s", event)

        @_kb.add(
            "c-e",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-e")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-e", event)

        @_kb.add(
            "c-c",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-c")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-c", event)

        @_kb.add(
            "c-d",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("c-d")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("c-d", event)

        @_kb.add(
            "escape",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("escape")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("escape", event)

        @_kb.add(
            "1",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("1")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("1", event)

        @_kb.add(
            "2",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("2")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("2", event)

        @_kb.add(
            "3",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("3")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("3", event)

        @_kb.add(
            "4",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("4")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("4", event)

        @_kb.add(
            "5",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("5")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("5", event)

        @_kb.add(
            "6",
            eager=True,
            filter=Condition(lambda: self._should_handle_running_prompt_key("6")),
        )
        def _(event: KeyPressEvent) -> None:
            self._handle_running_prompt_key("6", event)

        @_kb.add(Keys.BracketedPaste, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._handle_bracketed_paste(event)

        if clipboard_available or media_clipboard_available:

            @_kb.add("c-v", eager=True)
            def _(event: KeyPressEvent) -> None:
                from coderai.telemetry import track

                track("shortcut_paste")
                if self._try_paste_media(event):
                    return
                if clipboard_available:
                    try:
                        clipboard_data = event.app.clipboard.get_data()
                    except Exception:
                        return
                    if clipboard_data is None:  # type: ignore[reportUnnecessaryComparison]
                        return
                    self._insert_pasted_text(event.current_buffer, clipboard_data.text)
                    event.app.invalidate()

        # Only use PyperclipClipboard when pyperclip actually works.
        # PromptSession built-in keybindings (ctrl-k, ctrl-w, ctrl-y)
        # use clipboard without error handling, so a broken clipboard
        # object would crash the UI.
        clipboard = PyperclipClipboard() if clipboard_available else None

        self._session = PromptSession[str](
            message=self._render_message,
            # prompt_continuation=FormattedText([("fg:#4d4d4d", "... ")]),
            completer=self._agent_mode_completer,
            complete_while_typing=True,
            reserve_space_for_menu=6,
            key_bindings=_kb,
            clipboard=clipboard,
            history=history,
            bottom_toolbar=self._render_bottom_toolbar,
            style=get_prompt_style(),
        )
        self._session.default_buffer.read_only = Condition(
            lambda: (
                (delegate := self._active_prompt_delegate()) is not None
                and not delegate.running_prompt_allows_text_input()
            )
        )
        self._install_slash_completion_menu()
        self._install_prompt_buffer_visibility()
        self._apply_mode()

        # Allow completion to be triggered when the text is changed,
        # such as when backspace is used to delete text.
        @self._session.default_buffer.on_text_changed.add_handler
        def _(buffer: Buffer) -> None:
            self._last_input_activity_time = time.monotonic()
            self._input_activity_event.set()
            if buffer.complete_while_typing() and not self._suppress_auto_completion:
                buffer.start_completion()

        self._status_refresh_task: asyncio.Task[None] | None = None

    def _install_slash_completion_menu(self) -> None:
        float_container = _find_prompt_float_container(self._session.layout.container)
        if not isinstance(float_container, FloatContainer):
            return

        slash_menu_filter = (
            has_focus(self._session.default_buffer)
            & has_completions
            & ~is_done
            & Condition(self._should_show_slash_completion_menu)
        )
        slash_menu = ConditionalContainer(
            Window(
                content=SlashCommandMenuControl(left_padding=self._slash_menu_left_padding),
                dont_extend_height=True,
                height=Dimension(max=10),
                style="class:slash-completion-menu",
            ),
            filter=slash_menu_filter,
        )
        float_container.floats.insert(
            0,
            Float(
                left=0,
                right=0,
                ycursor=True,
                content=slash_menu,
                z_index=10**8,
            ),
        )

        original_float = next(
            (
                float_
                for float_ in float_container.floats[1:]
                if isinstance(float_.content, CompletionsMenu)
            ),
            None,
        )
        if original_float is None:
            return
        original_float.content = ConditionalContainer(
            original_float.content,
            filter=~Condition(self._should_show_slash_completion_menu),
        )

    def _install_prompt_buffer_visibility(self) -> None:
        buffer_container = _find_default_buffer_container(
            self._session.layout.container,
            self._session.default_buffer,
        )
        if buffer_container is None:
            return
        buffer_container.filter = buffer_container.filter & Condition(
            self._should_render_input_buffer
        )
        self._prompt_buffer_container = buffer_container

    def _should_show_slash_completion_menu(self) -> bool:
        document = self._session.default_buffer.document
        return SlashCommandCompleter.should_complete(document)

    def _slash_menu_left_padding(self) -> int:
        if self._mode == PromptMode.SHELL:
            return max(1, get_cwidth(f"{PROMPT_SYMBOL_SHELL} ") - 2)
        # Agent mode: prompt prefix is "│  " (3 chars inside input panel)
        return 1

    def _render_message(self) -> FormattedText:
        if self._mode == PromptMode.SHELL:
            return self._render_shell_prompt_message()
        return self._render_agent_prompt_message()

    def _render_shell_prompt_message(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80
        fragments: FormattedText = FormattedText()

        # Agent status (always visible)
        agent_status = self._render_agent_status(columns)
        if agent_status:
            fragments.extend(agent_status)
            if not agent_status[-1][1].endswith("\n"):
                fragments.append(("", "\n"))

        # Interactive body
        body = self._render_interactive_body(columns)
        if body:
            fragments.extend(body)
            if not body[-1][1].endswith("\n"):
                fragments.append(("", "\n"))

        if self._active_modal_delegate() is not None:
            return fragments
        has_content = bool(agent_status or body)
        if has_content:
            fragments.append(("", "\n"))
        # Shell mode: simple separator + $ prefix (no panel border)
        fragments.append(("class:running-prompt-separator", "─" * max(0, columns)))
        fragments.append(("", "\n"))
        fragments.append(("bold", f"{PROMPT_SYMBOL_SHELL} "))
        return fragments

    def _open_in_external_editor(self, event: KeyPressEvent) -> None:
        """Open the current buffer content in an external editor."""
        from prompt_toolkit.application.run_in_terminal import run_in_terminal

        from coderai.utils.editor import edit_text_in_editor, get_editor_command

        configured = self._editor_command_provider()

        if get_editor_command(configured) is None:
            toast("No editor found. Set $VISUAL/$EDITOR or run /editor.")
            return

        buff = event.current_buffer
        original_text = buff.text
        editor_text = self._get_placeholder_manager().expand_for_editor(original_text)

        async def _run_editor() -> None:
            result = await run_in_terminal(
                lambda: edit_text_in_editor(editor_text, configured), in_executor=True
            )
            if result is not None:
                refolded = self._get_placeholder_manager().refold_after_editor(
                    result, original_text
                )
                buff.document = Document(text=refolded, cursor_position=len(refolded))

        event.app.create_background_task(_run_editor())

    def _apply_mode(self, event: KeyPressEvent | None = None) -> None:
        # Apply mode to the active buffer (not the PromptSession itself)
        try:
            buff = event.current_buffer if event is not None else self._session.default_buffer
        except Exception:
            buff = None

        if self._mode == PromptMode.SHELL:
            if buff is not None:
                buff.completer = self._shell_mode_completer
        else:
            if buff is not None:
                buff.completer = self._agent_mode_completer
        self._sync_erase_when_done()

    def _sync_erase_when_done(self) -> None:
        app = getattr(self._session, "app", None)
        if app is not None:
            app.erase_when_done = self._mode == PromptMode.AGENT

    def _active_modal_delegate(self) -> RunningPromptDelegate | None:
        modal_delegates = getattr(self, "_modal_delegates", [])
        if not modal_delegates:
            return None
        _, delegate = max(
            enumerate(modal_delegates),
            key=lambda item: (item[1].modal_priority, item[0]),
        )
        return delegate

    def _active_prompt_delegate(self) -> RunningPromptDelegate | None:
        if delegate := self._active_modal_delegate():
            return delegate
        return getattr(self, "_running_prompt_delegate", None)

    def _active_ui_state(self) -> PromptUIState:
        delegate = self._active_modal_delegate()
        if delegate is None:
            return PromptUIState.NORMAL_INPUT
        if delegate.running_prompt_hides_input_buffer():
            return PromptUIState.MODAL_HIDDEN_INPUT
        if delegate.running_prompt_allows_text_input():
            return PromptUIState.MODAL_TEXT_INPUT
        return PromptUIState.NORMAL_INPUT

    def _should_render_input_buffer(self) -> bool:
        return self._active_ui_state() != PromptUIState.MODAL_HIDDEN_INPUT

    def _should_handle_running_prompt_key(self, key: str) -> bool:
        delegate = self._active_prompt_delegate()
        return delegate is not None and delegate.should_handle_running_prompt_key(key)

    def _handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None:
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return
        delegate.handle_running_prompt_key(key, event)
        event.app.invalidate()

    def invalidate(self) -> None:
        self._sync_prompt_ui_state()
        app = get_app_or_none()
        if app is not None:
            app.invalidate()

    def _sync_prompt_ui_state(self) -> None:
        new_state = self._active_ui_state()
        old_state = getattr(self, "_last_ui_state", PromptUIState.NORMAL_INPUT)
        buffer = self._session.default_buffer

        if (
            old_state != PromptUIState.MODAL_HIDDEN_INPUT
            and new_state == PromptUIState.MODAL_HIDDEN_INPUT
        ):
            if self._suspended_buffer_document is None and buffer.text:
                self._suspended_buffer_document = buffer.document
                buffer.set_document(Document(), bypass_readonly=True)
        elif (
            old_state == PromptUIState.MODAL_HIDDEN_INPUT
            and new_state != PromptUIState.MODAL_HIDDEN_INPUT
            and self._suspended_buffer_document is not None
        ):
            if not buffer.text:
                buffer.set_document(self._suspended_buffer_document, bypass_readonly=True)
            else:
                # Buffer was externally modified (e.g. approval inline feedback).
                # Don't overwrite the new content, but log that the old input is lost.
                logger.debug(
                    "Dropping suspended buffer document because buffer was modified externally"
                )
            self._suspended_buffer_document = None

        self._last_ui_state = new_state

    def _render_agent_prompt_message(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80
        fragments: FormattedText = FormattedText()

        # 1. Agent status — ALWAYS rendered from running prompt delegate.
        #    This ensures spinners, content blocks, tool calls etc. stay
        #    visible even when a modal (btw/approval/question) is active.
        agent_status = self._render_agent_status(columns)
        if agent_status:
            fragments.extend(agent_status)
            if not agent_status[-1][1].endswith("\n"):
                fragments.append(("", "\n"))

        # 2. Interactive area — from the active delegate (modal overrides).
        body = self._render_interactive_body(columns)
        if body:
            fragments.extend(body)
            if not body[-1][1].endswith("\n"):
                fragments.append(("", "\n"))

        # 3. When a modal is active, skip input panel border.
        if self._active_modal_delegate() is not None:
            return fragments

        # 4. Input section header — style varies by mode:
        #    normal:  ── input ─────────────────  (grey, solid)
        #    plan:    ╌╌ input · plan ╌╌╌╌╌╌╌╌╌  (blue, dashed)
        status = self._status_provider()
        # Build title parts
        title_parts = ["input"]
        if status.plan_mode:
            title_parts.append("plan")
        # Queue count from running prompt delegate
        running = self._running_prompt_delegate
        queue_count = len(getattr(running, "_queued_messages", []))
        if queue_count > 0:
            title_parts.append(f"{queue_count} queued")
        title = f" {' · '.join(title_parts)} "
        if status.plan_mode:
            dash = "╌"
            style = "fg:#60a5fa"  # blue
        else:
            dash = "─"
            style = "class:running-prompt-separator"
        border_fill = max(0, columns - len(title) - 2)
        top_border = f"{dash}{dash}{title}{dash * border_fill}"
        fragments.append(("", "\n"))
        fragments.append((style, top_border))
        fragments.append(("", "\n"))
        fragments.append(("", " "))
        return fragments

    def _render_agent_status(self, columns: int) -> FormattedText:
        """Render agent streaming output (always visible, independent of modals)."""
        running = self._running_prompt_delegate
        if running is not None and isinstance(running, AgentStatusProvider):
            return to_formatted_text(running.render_agent_status(columns))
        return self._render_status_block(columns)

    def _render_interactive_body(self, columns: int) -> FormattedText:
        """Render the interactive area from the active delegate (modal or running prompt)."""
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return FormattedText([])
        return to_formatted_text(delegate.render_running_prompt_body(columns))

    def _render_status_block(self, columns: int) -> FormattedText:
        status_block_provider = getattr(self, "_status_block_provider", None)
        if status_block_provider is None:
            return FormattedText([])
        block = status_block_provider(columns)
        if block is None:
            return FormattedText([])
        return to_formatted_text(block)

    def _render_agent_prompt_label(self) -> FormattedText:
        """Render the prompt label (empty — cursor starts at column 0)."""
        return FormattedText([("", "  ")])

    def __enter__(self) -> CustomPromptSession:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            return self

        async def _refresh() -> None:
            try:
                while True:
                    app = get_app_or_none()
                    if app is not None:
                        app.invalidate()

                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        logger.warning("No running loop found, exiting status refresh task")
                        self._status_refresh_task = None
                        break

                    interval = (
                        _RUNNING_REFRESH_INTERVAL
                        if self._active_prompt_delegate() is not None
                        or (
                            self._fast_refresh_provider is not None
                            and self._fast_refresh_provider()
                        )
                        else _IDLE_REFRESH_INTERVAL
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                # graceful exit
                pass

        self._status_refresh_task = asyncio.create_task(_refresh())
        return self

    def __exit__(self, *_) -> None:
        if self._status_refresh_task is not None and not self._status_refresh_task.done():
            self._status_refresh_task.cancel()
        self._status_refresh_task = None

    def _get_placeholder_manager(self) -> PromptPlaceholderManager:
        manager = getattr(self, "_placeholder_manager", None)
        if manager is None:
            attachment_cache = getattr(self, "_attachment_cache", None)
            manager = PromptPlaceholderManager(attachment_cache=attachment_cache)
            self._placeholder_manager = manager
            self._attachment_cache = manager.attachment_cache
        return manager

    def _insert_pasted_text(self, buffer: Buffer, text: str) -> None:
        normalized = normalize_pasted_text(text)
        if self._mode != PromptMode.AGENT:
            buffer.insert_text(normalized)
            return
        token_or_text = self._get_placeholder_manager().maybe_placeholderize_pasted_text(normalized)
        buffer.insert_text(token_or_text)

    def _handle_bracketed_paste(self, event: KeyPressEvent) -> None:
        self._insert_pasted_text(event.current_buffer, event.data)
        event.app.invalidate()

    def _try_paste_media(self, event: KeyPressEvent) -> bool:
        """Try to paste media from the clipboard.

        Reads the clipboard once and handles all detected content:
        non-image files (videos, PDFs, etc.) are inserted as paths,
        image files are cached and inserted as placeholders.
        Returns True if any media content was inserted.
        """
        try:
            result = grab_media_from_clipboard()
        except Exception:
            # ImageGrab.grabclipboard() may fail on headless Linux if the
            # real xclip cannot connect to an X server. Silently ignore so
            # that the text-paste fallback can still be attempted.
            return False
        if result is None:
            return False

        parts: list[str] = []

        # 1. Insert file paths (videos, PDFs, etc.)
        if result.file_paths:
            logger.debug("Pasted {count} file path(s) from clipboard", count=len(result.file_paths))
            for p in result.file_paths:
                text = str(p)
                if self._mode == PromptMode.SHELL:
                    text = shlex.quote(text)
                parts.append(text)

        # 2. Insert images via cache.
        if result.images:
            if "image_in" not in self._model_capabilities:
                console.print(
                    "[yellow]Image input is not supported by the selected LLM model[/yellow]"
                )
            else:
                for image in result.images:
                    token = self._get_placeholder_manager().create_image_placeholder(image)
                    if token is None:
                        continue
                    logger.debug(
                        "Pasted image from clipboard placeholder: {token}, {image_size}",
                        token=token,
                        image_size=image.size,
                    )
                    parts.append(token)

        if parts:
            event.current_buffer.insert_text(" ".join(parts))
        event.app.invalidate()
        return bool(parts)

    def set_prefill_text(self, text: str) -> None:
        """Pre-fill the input buffer with the given text.

        Must be called after the prompt session is created but before the
        first prompt_async call.  The text will appear as editable default
        input in the next prompt.
        """
        self._prefill_text = text

    async def prompt_next(self) -> UserInput:
        return await self._prompt_once(append_history=None)

    @property
    def last_submission_was_running(self) -> bool:
        return getattr(self, "_last_submission_was_running", False)

    def has_pending_input(self) -> bool:
        return bool(self._session.default_buffer.text)

    def had_recent_input_activity(self, *, within_s: float) -> bool:
        if self._last_input_activity_time <= 0:
            return False
        return (time.monotonic() - self._last_input_activity_time) <= within_s

    def recent_input_activity_remaining(self, *, within_s: float) -> float:
        if self._last_input_activity_time <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last_input_activity_time
        return max(0.0, within_s - elapsed)

    async def wait_for_input_activity(self) -> None:
        await self._input_activity_event.wait()
        self._input_activity_event.clear()

    def attach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        current = getattr(self, "_running_prompt_delegate", None)
        if current is delegate:
            return
        if current is None:
            self._running_prompt_previous_mode = self._mode
        self._running_prompt_delegate = delegate
        self._mode = PromptMode.AGENT
        self._apply_mode()
        self.invalidate()

    def detach_running_prompt(self, delegate: RunningPromptDelegate) -> None:
        if getattr(self, "_running_prompt_delegate", None) is not delegate:
            return
        previous_mode = getattr(self, "_running_prompt_previous_mode", None)
        self._running_prompt_delegate = None
        self._running_prompt_previous_mode = None
        if previous_mode is not None:
            self._mode = previous_mode
        self._apply_mode()
        self.invalidate()

    def attach_modal(self, delegate: RunningPromptDelegate) -> None:
        modal_delegates: list[RunningPromptDelegate] | None = getattr(
            self, "_modal_delegates", None
        )
        if modal_delegates is None:
            modal_delegates = []
            self._modal_delegates = modal_delegates
        if delegate in modal_delegates:
            return
        modal_delegates.append(delegate)
        self.invalidate()

    def detach_modal(self, delegate: RunningPromptDelegate) -> None:
        modal_delegates = getattr(self, "_modal_delegates", None)
        if not modal_delegates or delegate not in modal_delegates:
            return
        modal_delegates.remove(delegate)
        self.invalidate()

    def running_prompt_accepts_submission(self) -> bool:
        delegate = self._active_prompt_delegate()
        if delegate is None:
            return False
        return delegate.running_prompt_accepts_submission()

    async def _prompt_once(self, *, append_history: bool | None) -> UserInput:
        placeholder = None
        if (delegate := self._active_prompt_delegate()) is not None:
            placeholder = delegate.running_prompt_placeholder()
        # Consume one-shot prefill text if set
        default = getattr(self, "_prefill_text", None) or ""
        self._prefill_text = None
        with patch_stdout(raw=True):
            command = str(
                await self._session.prompt_async(placeholder=placeholder, default=default)
            ).strip()
            command = command.replace("\x00", "")  # just in case null bytes are somehow inserted
            # Sanitize UTF-16 surrogates that may come from Windows clipboard
            command = sanitize_surrogates(command)
        was_running = self.running_prompt_accepts_submission()
        self._last_submission_was_running = was_running
        if append_history is None:
            append_history = not was_running
        if append_history:
            self._append_history_entry(command)
        self._tip_rotation_index += 1
        return self._build_user_input(command)

    def _build_user_input(self, command: str) -> UserInput:
        resolved = self._get_placeholder_manager().resolve_command(command)

        return UserInput(
            mode=self._mode,
            command=resolved.display_command,
            resolved_command=resolved.resolved_text,
            content=resolved.content,
        )

    def _append_history_entry(self, text: str) -> None:
        safe_history_text = self._get_placeholder_manager().serialize_for_history(text).strip()
        entry = _HistoryEntry(content=safe_history_text)
        if not entry.content:
            return

        # skip if same as last entry
        if entry.content == self._last_history_content:
            return

        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with self._history_file.open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json(ensure_ascii=False) + "\n")
            self._last_history_content = entry.content
        except OSError as exc:
            logger.warning(
                "Failed to append user history entry: {file} ({error})",
                file=self._history_file,
                error=exc,
            )

    def _render_bottom_toolbar(self) -> FormattedText:
        if (
            hasattr(self, "_session")
            and self._should_show_slash_completion_menu()
            and self._session.default_buffer.complete_state is not None
        ):
            return FormattedText([])
        app = get_app_or_none()
        assert app is not None
        columns = app.output.get_size().columns

        fragments: list[tuple[str, str]] = []
        tc = get_toolbar_colors()

        fragments.append((tc.separator, "─" * columns))
        fragments.append(("", "\n"))

        remaining = columns

        # Time-based tip rotation (every 30 s, independent of user submissions)
        now = time.monotonic()
        if now - self._last_tip_rotate_time >= _TIP_ROTATE_INTERVAL:
            self._tip_rotation_index += 1
            self._last_tip_rotate_time = now

        # Status flags: yolo / afk / plan
        status = self._status_provider()
        if status.yolo_enabled:
            fragments.extend([(tc.yolo_label, "yolo"), ("", "  ")])
            remaining -= 6  # "yolo" = 4, "  " = 2
        if status.afk_enabled:
            fragments.extend([(tc.afk_label, "afk"), ("", "  ")])
            remaining -= 5  # "afk" = 3, "  " = 2
        if status.plan_mode:
            fragments.extend([(tc.plan_label, "plan"), ("", "  ")])
            remaining -= 6

        # Mode indicator (agent / shell) + model name + thinking indicator.
        # Degrade gracefully on narrow terminals:
        #   full: "agent (model-name ○)"  → mid: "agent ○"  → bare: "agent"
        mode = str(self._mode)
        if self._mode == PromptMode.AGENT and self._model_name:
            thinking_dot = "●" if self._thinking else "○"
            mode_full = f"{mode} ({self._model_name} {thinking_dot})"
            mode_mid = f"{mode} {thinking_dot}"
            if _display_width(mode_full) <= remaining - 2:
                mode = mode_full
            elif _display_width(mode_mid) <= remaining - 2:
                mode = mode_mid
            # else: keep bare mode name — model_name and dot are both dropped
        fragments.extend([("", mode), ("", "  ")])
        remaining -= _display_width(mode) + 2

        # CWD (truncated from left) + git branch with status badge
        # Degrade gracefully on narrow terminals: full → cwd-only → truncated cwd → skip
        try:
            cwd = _truncate_left(_shorten_cwd(str(KaosPath.cwd())), _MAX_CWD_COLS)
        except OSError:
            # CWD no longer exists (e.g. external drive unplugged).  Ask
            # prompt_toolkit to exit; the raised exception will propagate out
            # of prompt_async() into the Shell's event router which prints a
            # crash report with session info and exits cleanly.
            app.exit(exception=CwdLostError())
            return FormattedText([])
        branch = _get_git_branch()
        if branch:
            dirty, ahead, behind = _get_git_status()
            branch = _truncate_right(branch, _MAX_BRANCH_COLS)
            badge = _format_git_badge(branch, dirty, ahead, behind)
            cwd_text = f"{cwd}  {badge}"
        else:
            cwd_text = cwd
        cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = cwd  # drop badge
            cwd_w = _display_width(cwd_text)
        if cwd_w > remaining - 2:
            cwd_text = _truncate_right(cwd, max(0, remaining - 2))
            cwd_w = _display_width(cwd_text)
        if cwd_text and remaining >= cwd_w + 2:
            fragments.extend([(tc.cwd, cwd_text), ("", "  ")])
            remaining -= cwd_w + 2

        # Active background task counts (bash + agent, each rendered as its own
        # badge). Order matters: bash renders first; if there isn't room for the
        # agent badge too, drop agent and keep bash.
        bg_counts = (
            self._background_task_count_provider()
            if self._background_task_count_provider
            else BgTaskCounts()
        )
        for kind_label, kind_count in (("bash", bg_counts.bash), ("agent", bg_counts.agent)):
            if kind_count <= 0:
                continue
            bg_text = f"⚙ {kind_label}: {kind_count}"
            bg_width = _display_width(bg_text)
            if remaining < bg_width + 2:
                break
            fragments.extend([(tc.bg_tasks, bg_text), ("", "  ")])
            remaining -= bg_width + 2

        # Tips fill remaining space on line 1
        tip_text = self._get_two_rotating_tips()
        if tip_text and _display_width(tip_text) > remaining:
            tip_text = self._get_one_rotating_tip()
        if tip_text and _display_width(tip_text) <= remaining:
            fragments.append((tc.tip, tip_text))

        # ── line 2: toast (left) + context (right) — always rendered ──────
        fragments.append(("", "\n"))

        right_text = self._render_right_span(status)
        right_width = _display_width(right_text)

        left_toast = _current_toast("left")
        if left_toast is not None:
            max_left = max(0, columns - right_width - 2)
            if max_left > 0:
                left_text = left_toast.message
                if _display_width(left_text) > max_left:
                    left_text = _truncate_right(left_text, max_left)
                left_width = _display_width(left_text)
                fragments.append(("", left_text))
            else:
                left_width = 0
        else:
            left_width = 0

        fragments.append(("", " " * max(0, columns - left_width - right_width)))
        fragments.append(("", right_text))

        return FormattedText(fragments)

    def _get_two_rotating_tips(self) -> str | None:
        """Return a string with exactly 2 tips from the rotation, or fewer if not enough."""
        n = len(self._tips)
        if n == 0:
            return None
        if n == 1:
            return self._tips[0]
        offset = self._tip_rotation_index % n
        tip1 = self._tips[offset]
        tip2 = self._tips[(offset + 1) % n]
        return f"{tip1}{_TIP_SEPARATOR}{tip2}"

    def _get_one_rotating_tip(self) -> str | None:
        """Return the single leading tip for the current rotation."""
        if not self._tips:
            return None
        return self._tips[self._tip_rotation_index % len(self._tips)]

    @staticmethod
    def _render_right_span(status: StatusSnapshot) -> str:
        current_toast = _current_toast("right")
        if current_toast is None:
            return format_context_status(
                status.context_usage,
                status.context_tokens,
                status.max_context_tokens,
            )
        return current_toast.message




# --- COMPATIBILITY LAYER ---


from coderai.ui.shell.slash import completion_entries, resolve_command

AVAILABLE_SLASH_COMMANDS = completion_entries()


def _get_saved_session_ids(project_root: str) -> list[str]:
    """Retrieve saved session IDs from workspace index for autocompletion."""
    try:
        from coderai.soul.session.store import JsonlSessionStore

        store = JsonlSessionStore(project_root)
        data = store.load_index()
        entries = data.get("entries", [])
        ids: list[str] = []
        for e in entries:
            if isinstance(e, dict) and e.get("id"):
                sid = str(e["id"])
                ids.append(sid)
                if len(sid) > 16 and sid[:16] not in ids:
                    ids.append(sid[:16])
        return ids
    except Exception:
        pass
    return []


def _get_discovered_skill_names(project_root: str) -> list[str]:
    """Retrieve skill names discovered in workspace and global directories."""
    try:
        from coderai.skill import list_skills

        skills = list_skills(project_root)
        return [
            str(skill.get("name") or "")
            for skill in skills
            if isinstance(skill, dict) and skill.get("name")
        ]
    except Exception:
        return []


class CoderAICompleter:
    """Tab-autocompleter for slash commands, models, sub-arguments, and @file workspace paths."""

    def __init__(self, project_root: str, get_active_model: Any = None) -> None:
        self.project_root = project_root
        self.get_active_model = get_active_model

    def complete(self, text: str, state: int) -> str | None:
        """Readline completion callback function."""
        try:
            import readline

            get_buf = getattr(readline, "get_line_buffer", None)
            raw_buf = get_buf() if callable(get_buf) else ""
            line_buffer = raw_buf if raw_buf else text
        except Exception:
            line_buffer = text

        if not line_buffer and text:
            line_buffer = text

        options: list[str] = []

        # 1. @file autocomplete anywhere in line
        if "@" in line_buffer:
            at_idx = line_buffer.rfind("@")
            file_query = line_buffer[at_idx + 1 :]
            if " " not in file_query:
                matching_files = suggest_workspace_files(file_query, self.project_root, limit=20)
                options = [f"@{f}" for f in matching_files]
                if state < len(options):
                    return options[state]
                return None

        # 2. Slash command autocomplete at start of line
        stripped_line = line_buffer.lstrip()
        if stripped_line.startswith("/"):
            tokens = stripped_line.split()

            if len(tokens) <= 1 and not stripped_line.endswith(" "):
                cmd_prefix = tokens[0] if tokens else "/"
                all_cmds = [cmd for cmd, _ in AVAILABLE_SLASH_COMMANDS]
                matching_cmds = fuzzy_filter(cmd_prefix, all_cmds, limit=30)
                if state < len(matching_cmds):
                    return matching_cmds[state] + " "
                return None

            lead_cmd = tokens[0].lower()
            arg_prefix = tokens[1] if len(tokens) > 1 else ""
            command = resolve_command(lead_cmd)
            if command and command.subcommands:
                matching_subs = fuzzy_filter(arg_prefix, list(command.subcommands))
                if state < len(matching_subs):
                    return matching_subs[state]
                return None

            # Sub-argument completion for /model
            if lead_cmd in ("/model",):
                from coderai.ui.shell.session_picker import CURATED_MODELS

                all_models = [name for name, _, _ in CURATED_MODELS]
                matching_models = fuzzy_filter(arg_prefix, all_models, limit=15)
                if state < len(matching_models):
                    return matching_models[state]
                return None

            # Sub-argument completion for /skill
            if lead_cmd in ("/skill",):
                skill_names = _get_discovered_skill_names(self.project_root)
                matching_skills = fuzzy_filter(arg_prefix, skill_names, limit=15)
                if state < len(matching_skills):
                    return matching_skills[state]
                return None

            # Sub-argument completion for session IDs: /resume, /fork, /delete, /rm, /rename
            if lead_cmd in ("/resume", "/fork", "/delete", "/rm", "/rename"):
                session_ids = _get_saved_session_ids(self.project_root)
                matching_ids = fuzzy_filter(arg_prefix, session_ids, limit=15)
                if state < len(matching_ids):
                    return matching_ids[state]
                return None

            # Sub-argument completion for /help and /?
            if lead_cmd in ("/help", "/?"):
                all_topics = [cmd.lstrip("/") for cmd, _ in AVAILABLE_SLASH_COMMANDS] + [
                    "shortcuts",
                    "keyboard",
                    "editor",
                    "paste",
                ]
                matching_topics = fuzzy_filter(arg_prefix, all_topics, limit=20)
                if state < len(matching_topics):
                    return matching_topics[state]
                return None

        if state < len(options):
            return options[state]
        return None


def get_history_file_path(project_root: str | None = None) -> pathlib.Path:
    """Return path to persistent history file.

    Kimi parity: per-workspace history via md5(project_root) hash, fallback to global.
    """
    if project_root:
        import hashlib

        try:
            h = hashlib.md5(str(project_root).encode()).hexdigest()[:12]
            hist_dir = pathlib.Path.home() / ".coderai" / "history"
            hist_dir.mkdir(parents=True, exist_ok=True)
            return hist_dir / f"{h}.history"
        except Exception:
            pass
    hist_dir = pathlib.Path.home() / ".coderai"
    try:
        hist_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return hist_dir / "history"


def setup_readline(project_root: str, get_active_model: Any = None) -> bool:
    """Configure readline for persistent command history and tab completion."""
    try:
        import readline

        parse_and_bind = getattr(readline, "parse_and_bind", None)
        if callable(parse_and_bind):
            # Configure completion key
            if "libedit" in getattr(readline, "__doc__", ""):
                parse_and_bind("bind ^I rl_complete")
            else:
                parse_and_bind("tab: complete")

        # Set delimiter characters so @ and / are recognized cleanly
        set_delims = getattr(readline, "set_completer_delims", None)
        if callable(set_delims):
            set_delims(" \t\n`!@#$%^&*()=+[{]}\\|;:'\",<>?")

        completer = CoderAICompleter(project_root, get_active_model)
        set_comp = getattr(readline, "set_completer", None)
        if callable(set_comp):
            set_comp(completer.complete)

        # Load history
        hist_file = get_history_file_path()
        read_hist = getattr(readline, "read_history_file", None)
        set_hist_len = getattr(readline, "set_history_length", None)
        if hist_file.is_file() and callable(read_hist):
            try:
                read_hist(str(hist_file))
                if callable(set_hist_len):
                    set_hist_len(1000)
            except Exception:
                pass

        # Save history on exit
        write_hist = getattr(readline, "write_history_file", None)

        def _save_history() -> None:
            if callable(write_hist):
                try:
                    write_hist(str(get_history_file_path()))
                except Exception:
                    pass

        atexit.register(_save_history)
        return True
    except Exception:
        return False


# --- from coderai/cli/file_mention.py ---
"""Workspace file mention parser and context expansion (@file)."""


import os
import pathlib
import re

FILE_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_\-./\\]+(?::L?\d+(?:-\d+)?)?)")
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


def _find_matching_file(project_root: str, file_ref: str) -> pathlib.Path | None:
    """Find matching file by exact relative path, absolute path, or filename search."""
    root = pathlib.Path(project_root).resolve()

    # 1. Exact path relative to root
    exact = (root / file_ref).resolve()
    if exact.is_file():
        return exact

    # 2. Absolute path if provided
    if pathlib.Path(file_ref).is_absolute() and pathlib.Path(file_ref).is_file():
        return pathlib.Path(file_ref).resolve()

    # 3. Filename search in workspace tree
    target_name = pathlib.Path(file_ref).name.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            if f.lower() == target_name:
                return (pathlib.Path(dirpath) / f).resolve()

    return None


def _parse_line_range(spec: str) -> tuple[str, int | None, int | None]:
    """Parse '@file.py:10-20' or '@file.py:L10-L20' or '@file.py:15'."""
    if ":" not in spec:
        return spec, None, None
    path_part, range_part = spec.split(":", 1)
    range_part = range_part.replace("L", "").replace("l", "").strip()
    if "-" in range_part:
        start_str, end_str = range_part.split("-", 1)
        try:
            return path_part, int(start_str), int(end_str)
        except ValueError:
            return path_part, None, None
    try:
        single = int(range_part)
        return path_part, single, single
    except ValueError:
        return path_part, None, None


def read_file_mention_snippet(
    file_path: pathlib.Path, start_line: int | None, end_line: int | None
) -> str:
    """Read full file or slice of file content."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Error reading {file_path.name}: {e}]"

    lines = text.splitlines()
    if start_line is not None and end_line is not None:
        start_idx = max(1, start_line)
        end_idx = min(len(lines), end_line)
        sliced = lines[start_idx - 1 : end_idx]
        return "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=start_idx))

    return text


def expand_file_mentions(prompt: str, project_root: str) -> tuple[str, list[str]]:
    """Expand all @file mentions and @session references in the user prompt into embedded contexts."""
    from coderai.utils.common.session_reference import resolve_session_references

    matches = FILE_MENTION_PATTERN.findall(prompt)
    attached_files: list[str] = []
    snippets: list[str] = []

    for match in matches:
        if match.startswith("session:"):
            continue
        file_ref, start_line, end_line = _parse_line_range(match)
        target_path = _find_matching_file(project_root, file_ref)
        if target_path and target_path.is_file():
            rel_path = str(target_path.relative_to(pathlib.Path(project_root).resolve()))
            attached_files.append(rel_path)
            content = read_file_mention_snippet(target_path, start_line, end_line)
            range_info = f" (lines {start_line}-{end_line})" if start_line and end_line else ""
            snippet_block = f"--- Attached Context: {rel_path}{range_info} ---\n{content}\n--- End Attached Context ---"
            snippets.append(snippet_block)

    _, session_refs, session_context = resolve_session_references(project_root, prompt)
    if session_context:
        snippets.append(session_context)
        for sref in session_refs:
            if sref.get("resolved"):
                attached_files.append(f"session:{sref['sessionId']}")

    if not snippets:
        return prompt, []

    expanded_prompt = prompt + "\n\n" + "\n\n".join(snippets)
    return expanded_prompt, attached_files


def _list_files_git(project_root: str, limit: int = 1000) -> list[str] | None:
    """Fast git ls-files path with index mtime cache (Kimi LocalFileMentionCompleter parity)."""
    import subprocess
    import time as _t

    try:
        git_index = pathlib.Path(project_root) / ".git" / "index"
        idx_mtime = git_index.stat().st_mtime if git_index.exists() else None
        cached = getattr(_list_files_git, "_cached", None)
        if (
            cached
            and cached.get("idx_mtime") == idx_mtime
            and _t.monotonic() - cached.get("ts", 0) < 5
        ):
            return cached["files"][:limit]
        res = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode != 0:
            return None
        files = [file_path.strip() for file_path in res.stdout.splitlines() if file_path.strip()][
            :limit
        ]
        setattr(
            _list_files_git,
            "_cached",
            {"files": files, "idx_mtime": idx_mtime, "ts": _t.monotonic()},
        )
        return files
    except Exception:
        return None


def suggest_workspace_files(query: str, project_root: str, limit: int = 15) -> list[str]:
    """Search and fuzzy rank workspace files for autocompletion.

    Phase2: git-aware fast path + basename re-rank (Kimi parity).
    """
    root = pathlib.Path(project_root).resolve()
    query_clean = query.lstrip("@")
    # Try git first
    all_files: list[str] | None = _list_files_git(project_root, limit=1000)
    if all_files is None:
        all_files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for f in filenames:
                if f.startswith(".") and not query_clean.startswith("."):
                    continue
                full = pathlib.Path(dirpath) / f
                rel = str(full.relative_to(root))
                all_files.append(rel)
                if len(all_files) >= 1000:
                    break
            if len(all_files) >= 1000:
                break

    # Prioritize shallow files
    all_files.sort(key=lambda p: (p.count(os.sep), len(p)))


    # Initial fuzzy filter
    candidates = (
        fuzzy_filter(query_clean, all_files, limit=limit * 2)
        if query_clean
        else all_files[: limit * 2]
    )

    # Basename re-rank: exact basename == query -> top, prefix -> next (Kimi parity)
    if query_clean:
        ql = query_clean.lower()

        def _rank(f: str) -> tuple[int, int, int, str]:
            base = os.path.basename(f).lower()
            exact = 0 if base == ql else 1
            prefix = 0 if base.startswith(ql) else 1
            # also boost if query without path matches basename substring
            sub = 0 if ql in base else 1
            return (exact, prefix, sub, f)

        candidates = sorted(candidates, key=_rank)

    return candidates[:limit]


# ---------------------------------------------------------------------------
# File mention completer & PTK prompt session helpers
# ---------------------------------------------------------------------------


class FileMentionCompleter(Completer):
    """@-file completer — thin wrapper over coderai.ui.shell.prompt.

    Canonical file list / fuzzy logic lives in file_mention.py
    (git ls-files 5s TTL + walk fallback 1000 cap + basename re-rank).
    This completer only handles trigger detection (@) and PTK Completion
    yielding.
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
            from coderai.ui.shell.prompt import suggest_workspace_files

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


def get_bottom_toolbar_tokens(
    project_root: str,
    plan_mode: bool,
    active_model: str | None = None,
    extra_info: str | None = None,
    tokens: int = 0,
    turns: int = 0,
    mcp_count: int = 0,
    active_agent: str | None = None,
) -> list[tuple[str, str]]:
    """Return prompt_toolkit FormattedText for bottom toolbar."""
    global _tip_index, _tip_last_rotate
    toolbar_tokens: list[tuple[str, str]] = []

    # Active Model badge
    if active_model:
        toolbar_tokens.append(("class:toolbar.model", f" {active_model} "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # Active Agent Role badge
    if active_agent and active_agent != "default":
        toolbar_tokens.append(("class:toolbar.git", f" role: {active_agent} "))
        toolbar_tokens.append(("class:toolbar.sep", " · "))

    # Token Usage / Context Window %
    if active_model and tokens > 0:
        try:

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
                from coderai.ui.shell.slash import _COMMANDS

                slash_objs = list(_COMMANDS)
            except Exception:
                try:
                    from coderai.ui.shell.slash import completion_entries

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

            # Key bindings (Kimi keyboard.md parity):
            # c-j / escape-enter newline, s-tab plan toggle, c-o external
            # editor, c-s steer flag, c-x agent/shell mode flag.
            kb = KeyBindings()
            self.shell_mode = False
            self.steer_requested = False
            self._external_editor_cb: Any = None

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

            @kb.add("c-x")
            def _toggle_shell_mode(event: Any) -> None:  # type: ignore
                """Kimi Ctrl-X parity: toggle agent/shell mode indicator."""
                self.shell_mode = not self.shell_mode
                if hasattr(event, "app") and event.app is not None:
                    event.app.invalidate()

            @kb.add("c-s")
            def _steer(event: Any) -> None:  # type: ignore
                """Kimi Ctrl-S parity: mark steer — inject input into running turn."""
                self.steer_requested = True
                if hasattr(event, "app") and event.app is not None:
                    try:
                        event.app.exit(result=event.current_buffer.text)
                    except Exception:
                        pass

            @kb.add("c-o")
            def _external_editor(event: Any) -> None:  # type: ignore
                """Kimi Ctrl-O parity: open $VISUAL/$EDITOR for the current buffer."""
                buf = event.current_buffer
                try:
                    from coderai.utils.editor import open_external_editor

                    composed = open_external_editor(buf.text)
                    if composed:
                        buf.text = composed
                        buf.cursor_position = len(composed)
                except Exception:
                    pass

            @kb.add("c-e")
            def _expand_pager(event: Any) -> None:  # type: ignore
                """Kimi Ctrl-E parity: no-op in input (handled in approval panel)."""

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
                role = getattr(self, "_agent_role", None)
                if self.get_session_stats and callable(self.get_session_stats):
                    try:
                        st = self.get_session_stats()
                        if isinstance(st, dict):
                            toks = st.get("tokens", toks)
                            t_count = st.get("turns", t_count)
                            mcp_cnt = st.get("mcp_count", mcp_cnt)
                            role = st.get("agent_role", role)
                    except Exception:
                        pass
                return get_bottom_toolbar_tokens(
                    self.project_root,
                    self.plan_mode,
                    active_model=model,
                    tokens=toks,
                    turns=t_count,
                    mcp_count=mcp_cnt,
                    active_agent=role,
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
            agent_role: str | None = None,
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
            if agent_role is not None:
                self._agent_role = agent_role

        def _get_prompt_message(self) -> list[tuple[str, str]]:
            """Return dynamic formatted prompt tokens (Kimi: ✨/💫 agent, 📋 plan, $ shell)."""
            if getattr(self, "shell_mode", False):
                return [("class:prompt", "$ ")]
            if self.plan_mode:
                return [("class:prompt.plan", "📋 "), ("class:prompt", "❯ ")]
            return [("class:prompt", "❯ ")]

        def pop_steer(self) -> bool:
            """Consume the Ctrl-S steer flag (Kimi parity)."""
            flag = bool(getattr(self, "steer_requested", False))
            self.steer_requested = False
            return flag

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

    if not HAS_PTK or project_root is None or not os.isatty(1):
        # Fallback to legacy readline path (no prompt_toolkit)

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


# --- from coderai/cli/status_bar.py ---
"""Dynamic status line and prompt bar for interactive REPL."""


from typing import Any

from rich.text import Text


_ENGINE = None  # lazy: StatuslineEngine defined in statusline section below


def _engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = StatuslineEngine()  # resolved from module globals at call time
    return _ENGINE


def format_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
    turns: int = 0,
    mcp_count: int = 0,
) -> Text:
    """Format a dynamic status bar: [Model: <name>] [Tokens: <active> (<% of max>)] [Plan: ON/OFF] [Git: <branch>]."""
    return _engine().format_default_status_bar(
        model, active_tokens, plan_mode, branch, turns=turns, mcp_count=mcp_count
    )


def render_status_bar(
    console: Any | None,
    model: str,
    active_tokens: int,
    plan_mode: bool,
    project_root: str,
    turns: int = 0,
    mcp_count: int = 0,
    settings: dict[str, Any] | None = None,
) -> None:
    """Render the status bar line above the REPL input prompt."""
    engine = StatuslineEngine(settings) if settings else _ENGINE
    engine.render(
        console,
        model,
        active_tokens,
        plan_mode,
        project_root,
        turns=turns,
        mcp_count=mcp_count,
    )


# --- from coderai/cli/statusline.py ---
"""Pluggable statusline engine

Supports:
- Default dynamic gauge (Model, Tokens, Context Window %, Plan Mode, Git Branch, Turns, MCP count)
- Configurable `command` provider (executes shell command with timeout, ANSI stripping, and TTL cache)
- Configurable `module` provider (loads python module/callable or function with TTL cache)
- ANSI escape stripping and refresh timers
"""


import importlib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.text import Text

from coderai.config import get_default_context_window

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    if not text:
        return ""
    return ANSI_ESCAPE_PATTERN.sub("", text)


_GIT_STATUS_CACHE: dict[str, tuple[float, str | None, bool, int, int]] = {}
_GIT_CACHE_TTL = 3.0  # seconds


def get_git_status(project_root: str) -> tuple[str | None, bool]:
    """Retrieve the current active git branch and dirty status with TTL caching."""
    now = time.monotonic()
    cached = _GIT_STATUS_CACHE.get(project_root)
    if cached and now - cached[0] < _GIT_CACHE_TTL:
        return cached[1], cached[2]

    branch: str | None = None
    is_dirty = False
    ahead = behind = 0
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "-b"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if res.returncode == 0:
            lines = res.stdout.splitlines()
            if lines:
                first = lines[0]
                if first.startswith("## "):
                    header = first[3:].strip()
                    # e.g. "main...origin/main [ahead 1, behind 2]" or "main" or "HEAD (no branch)"
                    if "..." in header:
                        branch_part = header.split("...", 1)[0].strip()
                    else:
                        branch_part = header.split(" ", 1)[0].strip()
                    if branch_part and branch_part != "HEAD (no branch)":
                        branch = branch_part

                    m = re.search(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]", first)
                    if m:
                        ahead = int(m.group(1) or 0)
                        behind = int(m.group(2) or 0)
                if len(lines) > 1 or (len(lines) == 1 and not lines[0].startswith("## ")):
                    is_dirty = True
        else:
            # Fallback simple branch query
            res_b = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res_b.returncode == 0 and res_b.stdout.strip():
                branch = res_b.stdout.strip()
    except Exception:
        pass

    _GIT_STATUS_CACHE[project_root] = (now, branch, is_dirty, ahead, behind)
    return branch, is_dirty


def get_git_branch(project_root: str) -> str | None:
    """Retrieve the current active git branch name formatted with dirty flag, or None."""
    branch, is_dirty = get_git_status(project_root)
    if branch:
        return f"{branch}*" if is_dirty else branch
    return None


def get_git_branch_cached(project_root: str) -> str:
    """Get the active git branch name with dirty status marker if uncommitted changes exist, or 'no-git'."""
    b = get_git_branch(project_root)
    return b if b else "no-git"


def get_git_detailed_status(project_root: str) -> tuple[str | None, bool, int, int]:
    """Retrieve branch, dirty, ahead, behind with caching."""
    get_git_status(project_root)
    cached = _GIT_STATUS_CACHE.get(project_root)
    if cached:
        return cached[1], cached[2], cached[3], cached[4]
    return None, False, 0, 0


def make_mini_bar(pct: float, width: int = 8) -> str:
    """Generate a compact unicode progress bar."""
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    return "■" * filled + "□" * (width - filled)


def compute_token_gauge(active_tokens: int, model: str) -> tuple[str, str, float]:
    """Compute token display string, color style, and percentage used of context window."""
    ctx_window = get_default_context_window(model)
    pct = (active_tokens / ctx_window * 100) if ctx_window > 0 else 0.0

    if ctx_window >= 1024 * 1024:
        window_str = f"{ctx_window // (1024 * 1024)}M"
    elif ctx_window >= 1024:
        window_str = f"{ctx_window // 1024}k"
    else:
        window_str = str(ctx_window)

    pct_formatted = f"{pct:.0f}%" if pct >= 1 or active_tokens == 0 else f"{pct:.1f}%"
    display = f"{active_tokens:,} ({pct_formatted} of {window_str})"

    if pct < 60:
        style = "green"
    elif pct < 80:
        style = "yellow"
    else:
        style = "bold red"

    return display, style, pct


@dataclass
class StatuslineCacheEntry:
    value: str
    timestamp: float


class StatuslineEngine:
    """Pluggable statusline engine supporting custom command and module providers."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        self._cache: dict[str, StatuslineCacheEntry] = {}
        self._default_ttl: float = 3.0  # seconds

    def _get_provider_config(self) -> dict[str, Any] | None:
        cfg = self.settings.get("statusline")
        if isinstance(cfg, dict):
            return cfg
        if isinstance(cfg, str):
            return {"type": "command", "command": cfg}
        return None

    def execute_command_provider(
        self, command: str, project_root: str, ttl: float | None = None
    ) -> str:
        """Run a configured command provider with caching and ANSI stripping."""
        cache_key = f"cmd:{command}:{project_root}"
        now = time.time()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        cached = self._cache.get(cache_key)
        if cached and (now - cached.timestamp) < effective_ttl:
            return cached.value

        env = dict(os.environ)
        env["PAGER"] = "cat"
        env["NO_COLOR"] = "1"

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=2.0,
                env=env,
            )
            raw = res.stdout.strip() if res.returncode == 0 else ""
            clean = strip_ansi(raw).replace("\r\n", " ").replace("\n", " ").strip()
            self._cache[cache_key] = StatuslineCacheEntry(value=clean, timestamp=now)
            return clean
        except Exception:
            return ""

    def execute_module_provider(
        self, module_spec: str, context: dict[str, Any], ttl: float | None = None
    ) -> str:
        """Run a configured python module provider with caching and ANSI stripping."""
        cache_key = f"mod:{module_spec}"
        now = time.time()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        cached = self._cache.get(cache_key)
        if cached and (now - cached.timestamp) < effective_ttl:
            return cached.value

        try:
            if ":" in module_spec:
                mod_name, func_name = module_spec.split(":", 1)
            else:
                mod_name, func_name = module_spec, "render_statusline"

            mod = importlib.import_module(mod_name)
            func = getattr(mod, func_name, None)
            if callable(func):
                res = func(context)
                clean = strip_ansi(str(res or "")).replace("\r\n", " ").replace("\n", " ").strip()
                self._cache[cache_key] = StatuslineCacheEntry(value=clean, timestamp=now)
                return clean
        except Exception:
            pass
        return ""

    def format_default_status_bar(
        self,
        model: str,
        active_tokens: int,
        plan_mode: bool,
        branch: str,
        turns: int = 0,
        mcp_count: int = 0,
        term_width: int = 80,
    ) -> Text:
        """Format the default dynamic powerline gauge, adapting gracefully to terminal width."""
        plan_label = "ON" if plan_mode else "OFF"
        tokens_display, token_style, pct = compute_token_gauge(active_tokens, model)
        mini_bar = make_mini_bar(pct, width=5)

        is_compact = term_width < 80
        is_very_compact = term_width < 60

        bar = Text()
        # Model Segment
        bar.append(" ", style="default")
        bar.append(f"Model: {model}", style="bold cyan")

        # Divider
        bar.append(" │ ", style="dim")

        # Tokens Segment with Mini Bar
        base_color = token_style.replace("bold ", "")
        if is_very_compact:
            bar.append(f"{pct:.0f}% ctx", style=f"bold {base_color}")
        elif is_compact:
            bar.append(f"Tokens: {tokens_display}", style=f"bold {base_color}")
        else:
            bar.append(f"Tokens: {tokens_display}", style=f"bold {base_color}")
            bar.append(f" [{mini_bar}]", style=f"dim {base_color}")

        # Plan Mode Segment
        bar.append(" │ ", style="dim")
        bar.append("Plan: ", style="dim")
        bar.append(plan_label, style="bold yellow" if plan_mode else "dim")

        # Git Branch Segment
        if branch and branch != "no-git":
            bar.append(" │ ", style="dim")
            bar.append("Git: ", style="dim magenta")
            bar.append(branch, style="bold magenta")

        # Optional Turns Segment (only if ample width)
        if turns > 0 and not is_compact:
            bar.append(" │ ", style="dim")
            bar.append(f"Turns: {turns}", style="bold")

        # Optional MCP Segment (only if ample width)
        if mcp_count > 0 and not is_compact:
            bar.append(" │ ", style="dim")
            bar.append(f"MCP: {mcp_count}", style="bold green")

        return bar

    def render(
        self,
        console: Any | None,
        model: str,
        active_tokens: int,
        plan_mode: bool,
        project_root: str,
        turns: int = 0,
        mcp_count: int = 0,
    ) -> None:
        """Render the active statusline line above the prompt."""
        active_console = console or Console()
        provider_cfg = self._get_provider_config()

        if provider_cfg:
            ptype = provider_cfg.get("type", "command")
            ttl = float(provider_cfg.get("ttl", self._default_ttl))

            if ptype == "command" and provider_cfg.get("command"):
                custom_text = self.execute_command_provider(
                    provider_cfg["command"], project_root, ttl=ttl
                )
                if custom_text:
                    active_console.print()
                    active_console.print(Text(f" {custom_text}", style="dim cyan"))
                    return
            elif ptype == "module" and provider_cfg.get("module"):
                ctx = {
                    "model": model,
                    "active_tokens": active_tokens,
                    "plan_mode": plan_mode,
                    "project_root": project_root,
                    "turns": turns,
                    "mcp_count": mcp_count,
                }
                custom_text = self.execute_module_provider(provider_cfg["module"], ctx, ttl=ttl)
                if custom_text:
                    active_console.print()
                    active_console.print(Text(f" {custom_text}", style="dim cyan"))
                    return

        # Fallback to default statusline
        import shutil

        term_width = 80
        if (
            active_console is not None
            and isinstance(getattr(active_console, "width", None), int)
            and active_console.width > 0
        ):
            term_width = active_console.width
        else:
            term_width = shutil.get_terminal_size(fallback=(80, 24)).columns

        branch = get_git_branch_cached(project_root)
        bar = self.format_default_status_bar(
            model,
            active_tokens,
            plan_mode,
            branch,
            turns=turns,
            mcp_count=mcp_count,
            term_width=term_width,
        )
        active_console.print(bar)


_DEFAULT_ENGINE = StatuslineEngine()


def format_default_status_bar(
    model: str,
    active_tokens: int,
    plan_mode: bool,
    branch: str,
    turns: int = 0,
    mcp_count: int = 0,
    term_width: int = 80,
) -> Text:
    """Format the default dynamic gauge via module helper."""
    return _DEFAULT_ENGINE.format_default_status_bar(
        model,
        active_tokens,
        plan_mode,
        branch,
        turns=turns,
        mcp_count=mcp_count,
        term_width=term_width,
    )


def format_streamlined_status_bar(
    model: str,
    active_tokens: int,
    project_root: str,
    thinking_level: str = "high",
    plan_mode: bool = False,
    branch: str = "",
    term_width: int = 80,
) -> Text:
    """Format a clean, gold-standard 2-row or 1-row status bar matching screenshot aesthetics."""
    # Shorten CWD: replace user home with ~
    import os

    home = os.path.expanduser("~")
    display_cwd = project_root
    if project_root.startswith(home):
        display_cwd = "~" + project_root[len(home) :]

    ctx_window = get_default_context_window(model)
    pct = (active_tokens / ctx_window * 100) if ctx_window > 0 else 0.0

    if ctx_window >= 1024 * 1024:
        max_str = f"{ctx_window // (1024 * 1024)}M"
    elif ctx_window >= 1024:
        max_str = f"{ctx_window // 1024}k"
    else:
        max_str = str(ctx_window)

    tok_str = f"{active_tokens / 1000:.1f}k" if active_tokens >= 1000 else str(active_tokens)

    bar = Text()
    # Left segment: Model, thinking, cwd, git branch
    bar.append(f"{model} ", style="bold white")
    if thinking_level:
        bar.append(f"thinking: {thinking_level} ", style="dim")
    if plan_mode:
        bar.append("plan ", style="bold #38bdf8")
    bar.append(f"{display_cwd}", style="dim cyan")
    if branch and branch != "no-git":
        bar.append(f" ({branch})", style="dim magenta")

    # Right segment: Shortcut hints
    hints = "@: mention files | ! to run a shell command"
    left_len = len(bar.plain)
    hints_len = len(hints)
    gap = max(2, term_width - left_len - hints_len)
    bar.append(" " * gap)
    bar.append(hints, style="dim")

    # Second line: context gauge right-aligned
    ctx_info = f"context: {pct:.0f}% ({tok_str}/{max_str})"
    ctx_gap = max(0, term_width - len(ctx_info))
    bar.append("\n" + " " * ctx_gap)
    bar.append(ctx_info, style="dim")

    return bar


def render_statusline(
    console: Any | None,
    model: str,
    active_tokens: int,
    plan_mode: bool,
    project_root: str,
    turns: int = 0,
    mcp_count: int = 0,
    settings: dict[str, Any] | None = None,
) -> None:
    """Render statusline using configured or default engine."""
    engine = StatuslineEngine(settings) if settings else _DEFAULT_ENGINE
    engine.render(
        console,
        model,
        active_tokens,
        plan_mode,
        project_root,
        turns=turns,
        mcp_count=mcp_count,
    )


# --- from coderai/cli/input_engine.py (buffering half) ---
FENCE_PATTERN = re.compile(r"^```", re.MULTILINE)


TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def count_code_fences(text: str) -> int:
    """Count occurrences of triple-backtick markdown fences in text."""
    return len(FENCE_PATTERN.findall(text))


def count_triple_quotes(text: str) -> int:
    """Count occurrences of triple quotes (\"\"\" or ''') in text."""
    return len(TRIPLE_QUOTE_PATTERN.findall(text))


def is_multiline_incomplete(buffer_lines: list[str]) -> bool:
    """Determine if current multi-line input buffer requires further continuation lines."""
    if not buffer_lines:
        return False

    full_text = "\n".join(buffer_lines)

    # 1. Trailing backslash indicates explicit line continuation
    last_line = buffer_lines[-1]
    if last_line.endswith("\\") and not last_line.endswith("\\\\"):
        return True

    # 2. Odd number of ``` indicates an open code block fence
    fences = count_code_fences(full_text)
    if fences % 2 != 0:
        return True

    # 3. Odd number of triple quotes (\"\"\" or ''')
    triple_quotes = count_triple_quotes(full_text)
    if triple_quotes % 2 != 0:
        return True

    return False


def normalize_multiline_input(text: str) -> str:
    """Normalize multi-line input text (strip trailing carriage returns, resolve backslash continuations, unwrap clean envelopes)."""
    if not text:
        return ""

    # Normalize CRLF to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # If the text is wrapped in an outer triple-quote envelope (exactly 2 triple quotes total at start and end),
    # unwrap the outer shell used as the interactive multiline delimiter.
    trimmed = text.strip()
    if (
        trimmed.startswith('"""')
        and trimmed.endswith('"""')
        and len(trimmed) >= 6
        and count_triple_quotes(trimmed) == 2
    ):
        text = trimmed[3:-3].strip()
    elif (
        trimmed.startswith("'''")
        and trimmed.endswith("'''")
        and len(trimmed) >= 6
        and count_triple_quotes(trimmed) == 2
    ):
        text = trimmed[3:-3].strip()

    lines = text.split("\n")
    processed_lines: list[str] = []

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        # Count trailing backslashes to determine if it is an escaped backslash or a line continuation
        num_trailing_slashes = len(line) - len(line.rstrip("\\"))
        if num_trailing_slashes % 2 == 1 and idx + 1 < len(lines):
            # Odd number of trailing backslashes: line continuation
            joined = line[:-1].rstrip() + " " + lines[idx + 1].lstrip()
            lines[idx + 1] = joined
            idx += 1
            continue
        processed_lines.append(line)
        idx += 1

    result = "\n".join(processed_lines).strip()
    return result


def read_paste_mode(
    input_func: Callable[[str], str] = input,
    prompt_label: str = "paste (enter line with ::: or Ctrl-D to finish)> ",
) -> str:
    """Read multiline paste mode until explicit delimiter ':::' or EOF."""
    print(
        "Entered multiline paste mode. Paste your text, then type ':::' on a new line or press Ctrl-D to finish."
    )
    lines: list[str] = []
    while True:
        try:
            line = input_func("... ")
            if line.strip() == ":::":
                break
            lines.append(line)
        except (EOFError, KeyboardInterrupt):
            break
    return "\n".join(lines).strip()


def _is_multiline_trigger(line: str) -> bool:
    """Check if line ends with Kimi-style multiline triggers (\\, ```, or triple-quote)."""
    stripped = line.rstrip()
    if stripped.endswith("\\") and not stripped.endswith("\\\\"):
        return True
    return False


def read_user_turn(
    prompt: str = "coderai> ",
    continuation_prompt: str = "... ",
    input_func: Callable[[str], str] = input,
) -> str:
    """Read a user turn with Kimi-parity multiline support.

    Supports:
    - Trailing \\ continuation
    - ``` code fences (auto-continuation)
    - Triple-quote blocks
    - Ctrl-J / Alt-Enter style: if user types '...' hint, continue
    - Styled prompt indicator with history navigation via readline Up/Down
    - Paste protection: bracketed large pastes kept as single turn, Tab handled by completer

    Args:
        prompt: Initial prompt label.
        continuation_prompt: Prompt displayed for continuation lines.
        input_func: Input function (defaults to built-in input).

    Returns:
        Normalized input string.
    """
    # ponytail: NO_COLOR respected implicitly (no raw ANSI emitted here)
    first_line = input_func(prompt)
    buffer = [first_line]

    while is_multiline_incomplete(buffer):
        try:
            next_line = input_func(continuation_prompt)
            buffer.append(next_line)
        except (EOFError, KeyboardInterrupt):
            break

    raw_input = "\n".join(buffer)
    return normalize_multiline_input(raw_input)


PROMPT_STYLES = {
    "default": "❯ ",
    "plan": "[plan] ❯ ",
    "compacting": "◐ compacting... ❯ ",
}


def styled_prompt(plan_mode: bool = False, compacting: bool = False) -> str:
    """Return styled prompt indicator (Kimi parity)."""
    if compacting:
        return PROMPT_STYLES["compacting"]
    if plan_mode:
        return PROMPT_STYLES["plan"]
    return PROMPT_STYLES["default"]

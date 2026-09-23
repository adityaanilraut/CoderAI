"""Completers for CoderAI interactive prompt session.

Includes:
- SlashCommandCompleter: Fuzzy slash-command & subargument completions.
- FileMentionCompleter: Lightweight @-file completion wrapper.
- LocalFileMentionCompleter: Deep / top-level @-file workspace completions.
- fuzzy_filter / fuzzy_score: Fuzzy candidate matching helpers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar, override

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyCompleter,
    WordCompleter,
)
from prompt_toolkit.document import Document

T = TypeVar("T")


def fuzzy_score(query: str, candidate: str) -> tuple[bool, int]:
    """Score fuzzy match between query and candidate string."""
    q_lower = query.lower()
    c_lower = candidate.lower()

    if not q_lower:
        return True, 0

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
            score += 15
            if c_idx == prev_c_idx + 1:
                score += 30
            if c_idx == 0:
                score += 50
            elif candidate[c_idx - 1] in ("/", "\\", "_", "-", ".", " ", ":"):
                score += 40
            elif candidate[c_idx].isupper() and not candidate[c_idx - 1].isupper():
                score += 35

            prev_c_idx = c_idx
            q_idx += 1

    if q_idx == q_len:
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
            cmds = list(available_commands)
            normalized: list[Any] = []
            for c in cmds:
                if isinstance(c, (list, tuple)) and len(c) == 2 and isinstance(c[0], str):

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
            self._available_commands = sorted(normalized, key=lambda c: getattr(c, "name", str(c)))
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
            self._available_commands = []
            self._command_lookup = {}
            words = []

        self._word_completer = WordCompleter(words, ignore_case=True, match_middle=True)
        self._fuzzy = FuzzyCompleter(self._word_completer)

    def should_complete(self, document: Document) -> bool:
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return False
        return True

    def _display_name(self, cmd: Any, trigger: str) -> str:
        dn_attr = getattr(cmd, "display_name", None)
        if callable(dn_attr):
            return str(dn_attr(trigger))
        if isinstance(dn_attr, str):
            return dn_attr
        return f"/{getattr(cmd, 'name', str(cmd))}"

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
                arg_typed = text.split()[-1] if text.split() else ""

            candidates: list[tuple[str, str]] = []

            if lead_cmd in ("/model",):
                from coderai.ui.shell.session_picker import CURATED_MODELS

                for m in CURATED_MODELS:
                    candidates.append((m["id"], f"{m['name']} — {m['desc']}"))
            elif lead_cmd in ("/effort", "/reasoning"):
                for tier in ("off", "low", "medium", "high", "xhigh", "max"):
                    candidates.append((tier, f"Reasoning effort: {tier}"))
            elif lead_cmd in ("/plan",):
                for sub in ("on", "off", "view", "clear", "apply", "reset"):
                    candidates.append((sub, f"Plan mode: {sub}"))
            elif lead_cmd in ("/thinking", "/raw"):
                for sub in ("full", "summary", "lite", "normal", "on", "off"):
                    candidates.append((sub, f"Thinking display: {sub}"))
            elif lead_cmd in ("/mcp",):
                for sub in ("prompts", "resources", "reconnect"):
                    candidates.append((sub, f"MCP {sub}"))
            elif lead_cmd in ("/permission", "/permissions"):
                for sub in (
                    "read-only",
                    "workspace-write",
                    "danger-full-access",
                    "local-network-read",
                    "unrestricted-read",
                ):
                    candidates.append((sub, f"Permission preset: {sub}"))
            elif lead_cmd in ("/goal",):
                for sub in ("list", "add", "done", "cancel", "start"):
                    candidates.append((sub, f"Goal action: {sub}"))
            elif lead_cmd in ("/jobs", "/job"):
                for sub in ("list", "kill", "logs"):
                    candidates.append((sub, f"Background job: {sub}"))
            elif lead_cmd in ("/theme",):
                for sub in ("dark", "light"):
                    candidates.append((sub, f"Theme: {sub}"))
            elif lead_cmd in ("/agent", "/role"):
                try:
                    from coderai.subagents.discovery import discover_agent_specs

                    discovered = discover_agent_specs(self.project_root)
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
        # Case 3: /skill:<name> + /flow:<name> colon dispatch.
        if typed.startswith(("skill:", "flow:")):
            prefix, _, partial = typed.partition(":")
            try:
                from coderai.skill import list_skills

                skills = list_skills(self.project_root)
                names = [
                    str(sk.get("name")) for sk in skills if isinstance(sk, dict) and sk.get("name")
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


class LocalFileMentionCompleter(Completer):
    """Offer fuzzy `@` path completion by indexing workspace files.

    File discovery and ignore rules are delegated to
    :mod:`coderai.utils.file_filter`.
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
        self._is_git: bool | None = None
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
            candidates = list(self._fuzzy.get_completions(mention_doc, complete_event))
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
                return (cat,)

            candidates.sort(key=_rank)
            yield from candidates
        finally:
            self._fragment_hint = None


class FileMentionCompleter(Completer):
    """@-file completer — thin wrapper over workspace file suggestions."""

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

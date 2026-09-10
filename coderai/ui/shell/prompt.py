# Ported from coderai/cli/completer.py - kimi structure (ui/shell/prompt.py).
"""Readline autocompletion and command history for CoderAI interactive REPL."""

from __future__ import annotations

import atexit
import pathlib
from typing import Any

from coderai.ui.shell.slash import completion_entries, resolve_command

AVAILABLE_SLASH_COMMANDS = completion_entries()


def _get_saved_session_ids(project_root: str) -> list[str]:
    """Retrieve saved session IDs from workspace index for autocompletion."""
    try:
        from coderai.core.session_store import JsonlSessionStore

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
        from coderai.core.skill import list_skills

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
    from coderai.core.common.session_reference import resolve_session_references

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


# --- from coderai/cli/fuzzy.py ---
"""Fuzzy matching and ranking heuristics for CoderAI autocompleters, file mentions, and menus."""


from collections.abc import Callable
from typing import TypeVar

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


# --- from coderai/cli/prompt_session.py ---
"""Prompt Toolkit session — Phase2 port of Kimi ui/shell/prompt.py (lean).

Provides:
- SlashCommandCompleter (fuzzy, WordCompleter, should_complete)
- FileMentionCompleter (git ls-files + walk fallback, basename re-rank)
- Bottom toolbar (git branch/status cache, cwd truncate, plan/mode badges, tip rotation)
- CoderAIPromptSession (PromptSession wrapper, history per-workspace, key bindings)

Pony: ~380 LOC vs Kimi 2259 — keeps core UX, drops placeholder/media pipeline (Phase5).
"""


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

                elif lead_cmd in ("/skill",) or lead_cmd.startswith(("/skill:", "/flow:")):
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
            # Case 3: /skill:<name> + /flow:<name> colon dispatch (Kimi parity).
            if typed.startswith(("skill:", "flow:")):
                prefix, _, partial = typed.partition(":")
                try:
                    from coderai.core.skill import list_skills

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

from coderai.core.settings import get_default_context_window

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


# --- from coderai/cli/toast.py ---
"""Toast dedup — Phase5 port of Kimi ui/shell/prompt.py:1131 toast().

Per left/right deque, topic dedup, immediate prepend.
ponytail: no UI rendering; just queue management for approval/question dedup.
"""


import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

_IDLE_REFRESH_INTERVAL = 1.0


@dataclass(slots=True)
class _ToastEntry:
    topic: str | None
    message: str
    expires_at: float


_toast_queues: dict[Literal["left", "right"], deque[_ToastEntry]] = {
    "left": deque(),
    "right": deque(),
}


def toast(
    message: str,
    duration: float = 5.0,
    topic: str | None = None,
    immediate: bool = False,
    position: Literal["left", "right"] = "left",
) -> None:
    q = _toast_queues[position]
    duration = max(duration, _IDLE_REFRESH_INTERVAL)
    entry = _ToastEntry(topic=topic, message=message, expires_at=time.monotonic() + duration)
    if topic is not None:
        for existing in list(q):
            if existing.topic == topic:
                q.remove(existing)
    if immediate:
        q.appendleft(entry)
    else:
        q.append(entry)


def _current_toast(position: Literal["left", "right"] = "left") -> _ToastEntry | None:
    q = _toast_queues[position]
    now = time.monotonic()
    while q and q[0].expires_at <= now:
        q.popleft()
    if not q:
        return None
    return q[0]


def clear_toasts(position: Literal["left", "right"] | None = None) -> None:
    if position is None:
        for q in _toast_queues.values():
            q.clear()
    else:
        _toast_queues[position].clear()


# compat aliases
_toast_queues_left = _toast_queues["left"]
_toast_queues_right = _toast_queues["right"]


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

from __future__ import annotations

import atexit
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HAS_PTK = True

from prompt_toolkit import PromptSession

try:
    from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
except ImportError:  # pragma: no cover - minimal installs without `media` extra
    PyperclipClipboard = None  # type: ignore[assignment,misc]
from prompt_toolkit.completion import (
    merge_completers,
)
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.utils import get_cwidth

from coderai.ui.shell import placeholders as prompt_placeholders

AttachmentCache = prompt_placeholders.AttachmentCache
CachedAttachment = prompt_placeholders.CachedAttachment
_parse_attachment_kind = prompt_placeholders.parse_attachment_kind

PROMPT_SYMBOL = "✨"
PROMPT_SYMBOL_SHELL = "$"
PROMPT_SYMBOL_THINKING = "💫"
PROMPT_SYMBOL_PLAN = "📋"


class CwdLostError(OSError):
    """Raised when the working directory no longer exists (e.g. external drive unplugged)."""


from coderai.ui.shell.prompt_completers import (
    FileMentionCompleter as FileMentionCompleter,
    LocalFileMentionCompleter as LocalFileMentionCompleter,
    SlashCommandCompleter as SlashCommandCompleter,
    fuzzy_filter as fuzzy_filter,
    fuzzy_score as fuzzy_score,
)


_MAX_CWD_COLS = 30
_MAX_BRANCH_COLS = 22


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
            if at_idx > 0:
                _prev = line_buffer[at_idx - 1]
                if _prev.isalnum() or _prev in (".", "-", "_", "`", "'", '"', ":", "@", "#", "~"):
                    return None
            file_query = line_buffer[at_idx + 1 :]
            if " " not in file_query:
                matching_files = suggest_workspace_files(file_query, self.project_root, limit=20)
                # "@" is a readline completer delim, so the word being
                # replaced starts after it: return the bare path.
                options = list(matching_files)
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
            if lead_cmd.startswith(("/skill:", "/flow:")):
                # ":" is a readline delim, so only the suffix is replaced.
                _base, _, _suffix = lead_cmd.partition(":")
                skill_names = _get_discovered_skill_names(self.project_root)
                matching_skills = fuzzy_filter(_suffix, skill_names, limit=15)
                if state < len(matching_skills):
                    return matching_skills[state]
                return None
            command = resolve_command(lead_cmd)
            if command and command.subcommands:
                matching_subs = fuzzy_filter(arg_prefix, list(command.subcommands))
                if state < len(matching_subs):
                    return matching_subs[state]
                return None

            # Sub-argument completion for /model
            if lead_cmd in ("/model", "/models"):
                from coderai.ui.shell.session_picker import get_available_models

                all_models = [name for name, _, _ in get_available_models(refresh_openrouter=False)]
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

    Per-workspace history via md5(project_root) hash, fallback to global.
    """
    if project_root:
        import hashlib

        from coderai.share import get_share_dir

        try:
            h = hashlib.md5(str(project_root).encode()).hexdigest()[:12]
            hist_dir = get_share_dir() / "history"
            hist_dir.mkdir(parents=True, exist_ok=True)
            return hist_dir / f"{h}.history"
        except Exception:
            pass
    from coderai.share import get_share_dir

    hist_dir = get_share_dir()
    try:
        hist_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return hist_dir / "history"


_READLINE_SAVE_REGISTERED = False


def setup_readline(project_root: str, get_active_model: Any = None) -> bool:
    """Configure readline for persistent command history and tab completion."""
    global _READLINE_SAVE_REGISTERED
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

        # Load history (same per-workspace path as prompt_toolkit history).
        hist_file = get_history_file_path(project_root or None)
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

        def _save_history(hist_path: str = str(hist_file)) -> None:
            if callable(write_hist):
                try:
                    write_hist(hist_path)
                except Exception:
                    pass

        # Register the exit saver once: repeated REPL setups would stack
        # duplicate atexit saves (same file written N times on exit).
        if not _READLINE_SAVE_REGISTERED:
            atexit.register(_save_history)
            _READLINE_SAVE_REGISTERED = True
        return True
    except Exception:
        return False


"""Workspace file mention parser and context expansion (@file)."""


import pathlib

FILE_MENTION_PATTERN = re.compile(
    r'(?<![\w@])@((?:"[^"\n]+"|\'[^\'\n]+\'|[^\s@,;!?<>]+)(?::[Ll]?\d+(?:-[Ll]?\d+)?)?)'
)
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


class AmbiguousFileMention(ValueError):
    def __init__(self, reference: str, paths: list[pathlib.Path]):
        self.reference, self.paths = reference, paths
        super().__init__(
            f"Ambiguous file {reference!r}; choose an exact path: "
            + ", ".join(str(path) for path in paths[:10])
        )


def _find_matching_file(project_root: str, file_ref: str) -> pathlib.Path | None:
    """Find matching file by exact relative path, absolute path, or filename search."""
    root = pathlib.Path(project_root).resolve()

    # 1. Exact path relative to root
    exact = (root / pathlib.Path(file_ref).expanduser()).resolve()
    if exact.is_file():
        return exact

    # 2. Absolute path if provided
    if pathlib.Path(file_ref).is_absolute() and pathlib.Path(file_ref).is_file():
        return pathlib.Path(file_ref).resolve()

    # 3. Filename search in workspace tree
    target_name = pathlib.Path(file_ref).name.lower()
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            if f.lower() == target_name:
                matches.append((pathlib.Path(dirpath) / f).resolve())
    if len(matches) > 1:
        raise AmbiguousFileMention(file_ref, matches)
    return matches[0] if matches else None


def _parse_line_range(spec: str) -> tuple[str, int | None, int | None]:
    """Only a trailing numeric range is a range; drive letters/colons are paths."""
    match = re.fullmatch(r"(.+):[Ll]?(\d+)(?:-[Ll]?(\d+))?", spec)
    if match:
        path, start, end = match.groups()
        first, last = int(start), int(end or start)
        if first < 1 or last < first:
            raise ValueError("File line ranges must start at 1 and end at or after the start.")
        return path.strip("\"'"), first, last
    return spec.strip("\"'"), None, None


def read_file_mention_snippet(
    file_path: pathlib.Path, start_line: int | None, end_line: int | None
) -> str:
    """Read full file or slice of file content."""
    if file_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(
            f"Attached file exceeds 2 MiB: {file_path}. Select a smaller file or use a tool to read it."
        )
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Cannot read attached file {file_path}: {e}") from e

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
            try:
                rel_path = str(target_path.relative_to(pathlib.Path(project_root).resolve()))
            except ValueError:
                rel_path = str(target_path)
            attached_files.append(rel_path)
            content = read_file_mention_snippet(target_path, start_line, end_line)
            range_info = f" (lines {start_line}-{end_line})" if start_line and end_line else ""
            snippet_block = f"--- Attached Context: {rel_path}{range_info} ---\n{content}\n--- End Attached Context ---"
            snippets.append(snippet_block)
        else:
            raise ValueError(f"Attached file not found: {file_ref}")

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
    """Fast git ls-files path with index mtime cache."""
    import subprocess
    import time as _t

    try:
        git_index = pathlib.Path(project_root) / ".git" / "index"
        idx_mtime = git_index.stat().st_mtime if git_index.exists() else None
        cached = getattr(_list_files_git, "_cached", None)
        if (
            cached
            and cached.get("root") == str(pathlib.Path(project_root).resolve())
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
            {
                "files": files,
                "root": str(pathlib.Path(project_root).resolve()),
                "idx_mtime": idx_mtime,
                "ts": _t.monotonic(),
            },
        )
        return files
    except Exception:
        return None


def suggest_workspace_files(query: str, project_root: str, limit: int = 15) -> list[str]:
    """Search and fuzzy rank workspace files for autocompletion.

    Phase2: git-aware fast path + basename re-rank.
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

    # Basename re-rank: exact basename == query -> top, prefix -> next
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
    yolo: bool = False,
    afk: bool = False,
    width: int | None = None,
    state: str = "idle",
    execution_mode: str = "agent",
    permission: str = "ask",
    context_budget: int | None = None,
    reasoning_effort: str | None = None,
) -> list[tuple[str, str]]:
    """Return prompt_toolkit FormattedText for bottom toolbar."""
    from shutil import get_terminal_size
    from prompt_toolkit.utils import get_cwidth

    budget = width if width is not None else get_terminal_size((80, 24)).columns
    permission_label = (
        "YOLO+AFK" if yolo and afk else "YOLO" if yolo else "AFK" if afk else permission
    )
    segments = [
        ("class:toolbar", f"{execution_mode} {state}"),
        ("class:toolbar.plan", "plan: ON" if plan_mode else "plan: off"),
        ("class:toolbar.yolo", permission_label),
    ]
    if extra_info:
        segments.append(("class:toolbar.extra", extra_info))
    optional = []
    if active_model:
        # Drop routing prefixes, not the distinguishing end of a model name.
        optional.append(("class:toolbar.model", active_model.rsplit("/", 1)[-1]))
    if active_agent and active_agent != "default":
        optional.append(("class:toolbar.role", f"role: {active_agent}"))
    if tokens:
        pct = (
            tokens / context_budget * 100
            if context_budget
            else compute_token_gauge(tokens, active_model or "")[2]
        )
        optional.append(("class:toolbar.tokens", f"{tokens:,} est {pct:.0f}%"))
    if reasoning_effort:
        optional.append(("class:toolbar", f"effort:{reasoning_effort}"))
    cached = _GIT_STATUS_CACHE.get(project_root)
    if cached and cached[1]:
        optional.append(
            ("class:toolbar.git", _format_git_badge(cached[1], cached[2], cached[3], cached[4]))
        )
    optional.append(("class:toolbar.cwd", _shorten_cwd(project_root)))
    if turns:
        optional.append(("class:toolbar.turns", f"turns: {turns}"))
    if mcp_count:
        optional.append(("class:toolbar.mcp", f"mcp: {mcp_count}"))

    def cells(text):
        return sum(get_cwidth(char) for char in text)

    used = sum(cells(text) for _, text in segments) + 3 * (len(segments) - 1) + 1
    for style, value in optional:
        remaining = budget - used - 3
        if remaining < 8:
            break
        value = _truncate_right(value, min(24, remaining))
        segments.append((style, value))
        used += cells(value) + 3
    result = [("class:toolbar", " ")]
    for i, (style, value) in enumerate(segments):
        if i:
            result.append(("class:toolbar.sep", " · "))
        result.append((style, value))
    # Critical badges use compact labels when even their full form won't fit.
    if sum(cells(text) for _, text in result) > budget:
        critical = (
            f"{execution_mode} {state} plan:{'ON' if plan_mode else 'off'} {permission_label}"
        )
        if extra_info:
            compact_extra = extra_info.replace("queued:", "q:").replace("paused:", "pause:")
            if cells(critical + " " + compact_extra) <= budget:
                critical += " " + compact_extra
        result = [("class:toolbar", _truncate_right(critical, max(0, budget)))]
    return result


def _get_history_file(project_root: str) -> Path:
    """Per-workspace history via md5 (CoderAI completer canonical path)."""
    try:
        return get_history_file_path(project_root)
    except Exception:
        from coderai.share import get_share_dir

        h = hashlib.md5(str(project_root).encode()).hexdigest()[:12]
        hist_dir = get_share_dir() / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        return hist_dir / f"{h}.history"


class _PasteSafeFileHistory(FileHistory):
    """FileHistory that stores the collapsed display token for large pastes.

    prompt_toolkit persists every accepted buffer verbatim, so a pasted
    blob would flood history (and history recall) with megabytes of text.
    Large inputs use persistent private payloads with unique identities.
    """

    def store_string(self, string: str) -> None:
        try:
            from coderai.ui.shell.placeholders import (
                normalize_pasted_text,
                should_placeholderize_pasted_text,
            )

            normalized = normalize_pasted_text(string)
            if should_placeholderize_pasted_text(normalized):
                from coderai.ui.shell.placeholders import get_placeholder_manager

                collapsed = get_placeholder_manager().maybe_placeholderize_pasted_text(normalized)
                if collapsed != normalized:
                    super().store_string(collapsed)
                    return
        except Exception:
            pass
        super().store_string(string)

    def replace_last(self, project_root: str, replacement: str) -> None:
        """Update persistent and already-loaded recall after attaching images."""
        _rewrite_last_history_entry(project_root, replacement)
        if self._loaded_strings:
            self._loaded_strings[0] = replacement


def _rewrite_last_history_entry(project_root: str, replacement: str) -> None:
    """Replace the most recent history entry with `replacement`.

    Used after a large paste was accepted: prompt_toolkit already appended
    the raw blob, so the trailing entry is rewritten to the collapsed
    display token. Best effort; failures are swallowed by the caller.
    """
    hist = _get_history_file(project_root)
    if not hist.exists():
        return
    raw = hist.read_bytes().decode("utf-8", errors="replace").split("\n")
    # FileHistory format: entries separated by `# <timestamp>` lines, each
    # entry line prefixed with `+`. Drop the last entry block, keep the rest.
    last_sep = None
    for i in range(len(raw) - 1, -1, -1):
        if raw[i].startswith("# "):
            last_sep = i
            break
    if last_sep is None:
        return
    import datetime

    entry = [f"# {datetime.datetime.now()}", *(f"+{line}" for line in replacement.split("\n"))]
    hist.write_text("\n".join([*raw[:last_sep], *entry, ""]), encoding="utf-8")


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
            additional_roots: Any | None = None,
        ) -> None:
            self.project_root = project_root
            self.get_active_model = get_active_model
            self.plan_mode = plan_mode
            self.get_session_stats = get_session_stats
            self.on_plan_mode_toggle = on_plan_mode_toggle
            self._tokens: int = 0
            self._turns: int = 0
            self._mcp_count: int = 0
            self._yolo: bool = False
            self._afk: bool = False
            self.on_interrupt: Any = None
            self.preview: Any = None
            self.modal = False
            self.inspect_requested = False
            self.attachments_requested = False
            self.controller_owned = False
            self.accessible: Any = None
            self.external_editor_context: Any = None

            # Build completers — pass canonical SlashCommand objects with aliases
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
            self._file_completer = FileMentionCompleter(project_root, additional_roots)
            self._completer = merge_completers(
                [self._slash_completer, self._file_completer], deduplicate=True
            )

            # History per-workspace (paste-safe: huge pastes collapse to
            # display tokens so history recall never floods the buffer).
            hist_file = _get_history_file(project_root)
            # FileHistory expects file to exist; prompt_toolkit handles creation
            try:
                hist_file.touch(exist_ok=True)
            except Exception:
                pass
            self._history = _PasteSafeFileHistory(str(hist_file))

            # Key bindings:
            # c-j / escape-enter newline, s-tab plan toggle, c-o external
            # editor, c-s steer flag, c-x agent/shell mode flag.
            from coderai.ui.shell.shortcuts import bind_shortcut

            kb = KeyBindings()

            def bind(name):
                return bind_shortcut(kb, name)

            self.shell_mode = False
            self.steer_requested = False
            self._external_editor_cb: Any = None

            @bind("complete")
            def _tab_complete(event: Any) -> None:
                if self.modal:
                    return
                buf = event.current_buffer
                if buf.complete_state:
                    buf.complete_next()
                else:
                    buf.start_completion(select_first=True)

            @bind("newline")
            def _newline(event: Any) -> None:
                event.current_buffer.insert_text("\n")

            @bind("plan")
            def _toggle_plan_mode(event: Any) -> None:  # type: ignore
                if self.modal:
                    return
                self.plan_mode = not self.plan_mode
                if self.on_plan_mode_toggle and callable(self.on_plan_mode_toggle):
                    try:
                        self.on_plan_mode_toggle(self.plan_mode)
                    except Exception:
                        pass
                if hasattr(event, "app") and event.app is not None:
                    event.app.invalidate()

            @bind("shell")
            def _toggle_shell_mode(event: Any) -> None:  # type: ignore
                """Ctrl-X: toggle agent/shell mode indicator."""
                if self.modal:
                    return
                self.shell_mode = not self.shell_mode
                if hasattr(event, "app") and event.app is not None:
                    event.app.invalidate()

            @bind("steer")
            def _steer(event: Any) -> None:  # type: ignore
                """Ctrl-S: mark steer — inject input into running turn."""
                if self.modal:
                    return
                self.steer_requested = True
                if hasattr(event, "app") and event.app is not None:
                    try:
                        event.app.exit(result=event.current_buffer.text)
                    except Exception:
                        pass

            @bind("editor")
            async def _external_editor(event: Any) -> None:  # type: ignore
                """Ctrl-O: open $VISUAL/$EDITOR for the current buffer."""
                if self.modal:
                    return
                buf = event.current_buffer
                try:
                    from coderai.utils.editor import open_external_editor

                    from prompt_toolkit.application import run_in_terminal
                    from coderai.ui.shell.placeholders import get_placeholder_manager

                    original = buf.text
                    manager = get_placeholder_manager()

                    def _run() -> None:
                        composed = open_external_editor(manager.expand_for_editor(original))
                        if composed is not None:
                            buf.text = manager.refold_after_editor(composed, original)
                            buf.cursor_position = len(buf.text)

                    from contextlib import nullcontext

                    context = (
                        self.external_editor_context()
                        if self.external_editor_context
                        else nullcontext()
                    )
                    with context:
                        await run_in_terminal(_run, in_executor=True)
                except (OSError, ValueError) as exc:
                    from coderai.utils.logging import logger

                    logger.warning("External editor failed: {error}", error=str(exc))

            @bind("interrupt")
            def _ctrl_c_clear(event: Any) -> None:  # type: ignore
                """Ctrl-C: bash-like — clear a non-empty line, cancel if empty.

                Default prompt_toolkit raises KeyboardInterrupt, which drops
                the line via exception paths and conflates "clear line" with
                "abort prompt". Clearing in place keeps the prompt alive; an
                empty buffer exits with "" (cancel) without raising, so
                Ctrl-C can never submit half-typed text to the turn.
                """
                try:
                    buf = event.current_buffer
                    if self.modal:
                        event.app.exit(exception=KeyboardInterrupt())
                    elif self.on_interrupt and self.on_interrupt():
                        event.app.invalidate()
                    elif buf.text:
                        buf.reset()
                    else:
                        event.app.exit(result="")
                except Exception:
                    pass

            @bind("output")
            def _expand_pager(event: Any) -> None:  # type: ignore
                """Expand retained tool/subagent output in the terminal pager."""
                if self.modal:
                    return
                if not self.controller_owned:
                    from prompt_toolkit.application import run_in_terminal
                    from coderai.ui.shell.app import _STREAM_STATE

                    if _STREAM_STATE.has_expandable_panel():
                        run_in_terminal(_STREAM_STATE._show_expandable_panel_content)
                    return
                self.inspect_requested = True
                event.app.exit(result=event.current_buffer.text)

            @bind("attachments")
            def _attachment_tray(event: Any) -> None:
                if self.modal or not self.controller_owned:
                    return
                self.attachments_requested = True
                event.app.exit(result=event.current_buffer.text)

            @bind("cancel")
            def _escape_modal(event: Any) -> None:
                if self.modal:
                    event.app.exit(exception=KeyboardInterrupt())

            @bind("exit")
            def _eof(event: Any) -> None:
                if self.modal or not event.current_buffer.text:
                    event.app.exit(exception=EOFError())
                else:
                    event.current_buffer.delete()

            self._kb = kb

            # Style is resolved on each draw so /theme dark|light repaints the
            # bar and the completion menu together. Every toolbar class shares
            # one background; a partial override used to punch holes in the strip.
            def _session_style() -> Style:
                if os.getenv("NO_COLOR") is not None or (self.accessible and self.accessible()):
                    return Style.from_dict({})
                try:
                    from coderai.ui.theme import get_prompt_session_styles

                    return Style.from_dict(get_prompt_session_styles())
                except Exception:
                    return Style.from_dict(
                        {
                            "toolbar": "bg:#1e1e2e #cdd6f4",
                            "prompt": "bold",
                        }
                    )

            self._style = DynamicStyle(_session_style)

            def _toolbar_callback() -> list[tuple[str, str]]:
                if self.accessible and self.accessible():
                    return []
                model = self.get_active_model() if self.get_active_model else None
                toks = self._tokens
                t_count = self._turns
                mcp_cnt = self._mcp_count
                role = getattr(self, "_agent_role", None)
                yolo_on = getattr(self, "_yolo", False)
                afk_on = getattr(self, "_afk", False)
                st = {}
                if self.get_session_stats and callable(self.get_session_stats):
                    try:
                        st = self.get_session_stats()
                        if isinstance(st, dict):
                            toks = st.get("tokens", toks)
                            t_count = st.get("turns", t_count)
                            mcp_cnt = st.get("mcp_count", mcp_cnt)
                            role = st.get("agent_role", role)
                            yolo_on = bool(st.get("yolo", yolo_on))
                            afk_on = bool(st.get("afk", afk_on))
                    except Exception:
                        pass
                return get_bottom_toolbar_tokens(
                    self.project_root,
                    st.get("plan_mode", self.plan_mode),
                    active_model=model,
                    tokens=toks,
                    turns=t_count,
                    mcp_count=mcp_cnt,
                    active_agent=role,
                    yolo=yolo_on,
                    afk=afk_on,
                    width=self._session.app.output.get_size().columns,
                    state=st.get("state", "idle") if self.get_session_stats else "idle",
                    execution_mode=st.get(
                        "execution_mode", "shell" if self.shell_mode else "agent"
                    ),
                    permission=st.get("permission", "ask") if self.get_session_stats else "ask",
                    context_budget=st.get("context_limit") if self.get_session_stats else None,
                    extra_info=st.get("pending") if self.get_session_stats else None,
                    reasoning_effort=st.get("reasoning_effort"),
                )

            self._session: PromptSession[Any] = PromptSession(
                completer=self._completer,
                history=self._history,
                key_bindings=kb,
                style=self._style,
                complete_in_thread=True,
                complete_while_typing=True,
                reserve_space_for_menu=0,
                bottom_toolbar=_toolbar_callback,  # type: ignore[arg-type]
            )

        def set_plan_mode(self, plan_mode: bool) -> None:
            self.update_plan_mode(plan_mode)

        def update_plan_mode(self, plan_mode: bool) -> None:
            self.plan_mode = plan_mode

        def update_session_stats(
            self,
            tokens: int | None = None,
            turns: int | None = None,
            mcp_count: int | None = None,
            plan_mode: bool | None = None,
            agent_role: str | None = None,
            yolo: bool | None = None,
            afk: bool | None = None,
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
            if yolo is not None:
                self._yolo = bool(yolo)
            if afk is not None:
                self._afk = bool(afk)

        def _get_slash_preview(self) -> list[tuple[str, str]]:
            """Reserve clean headroom above the input line so CompletionsMenu floats cleanly above."""
            if not hasattr(self, "_session") or self.modal:
                return []
            try:
                buf = self._session.default_buffer
                text = buf.text.lstrip()
                if not text.startswith("/") and not text.startswith("@"):
                    return []

                doc = buf.document
                from prompt_toolkit.completion import CompleteEvent

                comps = list(self._completer.get_completions(doc, CompleteEvent()))
                if not comps:
                    return []

                num_lines = min(7, len(comps))
                return [("", "\n" * (num_lines + 1))]
            except Exception:
                return []

        def _get_prompt_message(self) -> list[tuple[str, str]]:
            """Return dynamic formatted prompt tokens (✨/💫 agent, 📋 plan, $ shell)."""
            prefix = self.preview() if self.preview and not self.modal else []
            slash_preview = self._get_slash_preview()
            if slash_preview:
                prefix = prefix + slash_preview
            if getattr(self, "shell_mode", False):
                return prefix + [("class:prompt", "$ ")]
            if self.plan_mode:
                return prefix + [("class:prompt.plan", "plan "), ("class:prompt", "> ")]
            return prefix + [("class:prompt", "> ")]

        def pop_steer(self) -> bool:
            """Consume the Ctrl-S steer flag."""
            flag = bool(getattr(self, "steer_requested", False))
            self.steer_requested = False
            return flag

        def remember_submission(self, text: str) -> None:
            self._history.replace_last(self.project_root, text)

        async def prompt_async(
            self, message: Any = None, *, default: str = "", cursor: int | None = None
        ) -> str:
            """Async prompt with styled message and live plan/build toggle support."""
            try:
                # Use patch_stdout to not interfere with Live
                from prompt_toolkit.patch_stdout import patch_stdout

                msg = self._get_prompt_message if message in (None, "❯ ", "[plan] ❯ ") else message
                with patch_stdout(raw=True):
                    from prompt_toolkit.document import Document

                    document = (
                        Document(default, cursor_position=min(len(default), max(0, cursor)))
                        if cursor is not None
                        else default
                    )
                    text = await self._session.prompt_async(msg, default=document)  # type: ignore[arg-type]
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

    # NOTE: gate on stdin (not stdout fd 1): `cmd | coderai` or piped
    # input with a tty stdout must take the readline fallback, otherwise
    # prompt_toolkit blocks waiting on a non-interactive stdin.
    if not HAS_PTK or project_root is None or not sys.stdin.isatty():
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
        except KeyboardInterrupt:
            # Cancel the turn: interrupted input must never submit a partial command.
            raise
        except EOFError:
            break
    return normalize_multiline_input("\n".join(buf))


def is_ptk_available() -> bool:
    return HAS_PTK


"""Dynamic status line and prompt bar for interactive REPL."""


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


"""Pluggable statusline engine

Supports:
- Default dynamic gauge (Model, Tokens, Context Window %, Plan Mode, Git Branch, Turns, MCP count)
- Configurable `command` provider (executes shell command with timeout, ANSI stripping, and TTL cache)
- Configurable `module` provider (loads python module/callable or function with TTL cache)
- ANSI escape stripping and refresh timers
"""


import importlib
import re
import time

from rich.console import Console

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

        from coderai.utils.subprocess_env import scrub_subprocess_env

        env = scrub_subprocess_env(dict(os.environ))
        env["PAGER"] = "cat"
        env["NO_COLOR"] = "1"

        try:
            import shlex as _shlex

            argv = _shlex.split(command, posix=True)
            if not argv:
                return ""
            res = subprocess.run(
                argv,
                shell=False,
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


FENCE_PATTERN = re.compile(r"^```", re.MULTILINE)


TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def count_code_fences(text: str) -> int:
    """Count triple-backtick markdown fence markers in text.

    Indented markers still open/close a fence, and a single line carrying
    both markers (e.g. ``` code ```) counts twice.
    """
    total = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            total += stripped.count("```")
    return total


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
    """Check if line ends with multiline triggers (\\, ```, or triple-quote)."""
    stripped = line.rstrip()
    if stripped.endswith("\\") and not stripped.endswith("\\\\"):
        return True
    return False


def read_user_turn(
    prompt: str = "coderai> ",
    continuation_prompt: str = "... ",
    input_func: Callable[[str], str] = input,
) -> str:
    """Read a user turn with multiline support.

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
    """Return styled prompt indicator."""
    if compacting:
        return PROMPT_STYLES["compacting"]
    if plan_mode:
        return PROMPT_STYLES["plan"]
    return PROMPT_STYLES["default"]

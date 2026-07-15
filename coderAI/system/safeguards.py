"""Safety and validation guards for autonomous agent execution.

Provides reusable checks that prevent the agent from:
- Running interactive commands via non-interactive pipes
- Operating in empty/invalid project directories
- Leaking git operations to parent repositories
- Staging junk files (.DS_Store, __pycache__, .coderAI/, etc.)
"""

import asyncio
import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from coderAI.system.redaction import redact_text

logger = logging.getLogger(__name__)


# Categorised interactive binaries.  ``is_interactive_command()`` uses the
# category to decide whether arguments (a script file, ``-c``, ``-e``) make
# the command non-interactive.  The ``__contains__`` check on the values
# (frozensets) below answers ``binary in _INTERACTIVE_BINARIES``.
# Adding a binary here automatically makes it available for all lookups;
# no separate subsets to maintain.
_INTERACTIVE_COMMAND_CATEGORIES: Dict[str, frozenset[str]] = {
    "interpreter": frozenset(
        {
            "python",
            "python3",
            "python2",
            "node",
            "bun",
            "lua",
            "luajit",
            "julia",
            "ruby",
            "irb",
            "pry",
            "r",
            "R",
            "scala",
            "ghci",
            "erl",
            "iex",
        }
    ),
    "shell": frozenset(
        {
            "bash",
            "zsh",
            "sh",
            "fish",
            "csh",
            "tcsh",
        }
    ),
    "always_interactive": frozenset(
        {
            "vim",
            "nvim",
            "vi",
            "nano",
            "emacs",
            "pico",
            "ed",
            "less",
            "more",
            "top",
            "htop",
            "btop",
            "glances",
            "nmon",
        }
    ),
    "db_network": frozenset(
        {
            "psql",
            "mysql",
            "sqlite3",
            "mongosh",
            "mongo",
            "redis-cli",
            "ssh",
            "telnet",
            "ftp",
            "sftp",
        }
    ),
    "other": frozenset(
        {
            "nix-shell",
            "coderai",
        }
    ),
}

# Union of all categories for O(1) membership.
_INTERACTIVE_BINARIES: frozenset[str] = frozenset(
    b for s in _INTERACTIVE_COMMAND_CATEGORIES.values() for b in s
)
_SHELL_BINARIES: frozenset[str] = _INTERACTIVE_COMMAND_CATEGORIES["shell"]
_INTERPRETER_BINARIES: frozenset[str] = _INTERACTIVE_COMMAND_CATEGORIES["interpreter"]
_ALWAYS_INTERACTIVE_BINARIES: frozenset[str] = _INTERACTIVE_COMMAND_CATEGORIES["always_interactive"]
_DATABASE_NETWORK_CLIS: frozenset[str] = _INTERACTIVE_COMMAND_CATEGORIES["db_network"]

# Patterns that indicate interactive flags (e.g. docker run -it, docker exec -it)
_INTERACTIVE_FLAG_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bdocker\s+(?:run|exec)(?:\s+-[a-zA-Z0-9=-]*)*\s+-[a-z]*i[a-z]*t\b"),
    re.compile(r"\bdocker\s+(?:run|exec)(?:\s+-[a-zA-Z0-9=-]*)*\s+-[a-z]*t[a-z]*i\b"),
    re.compile(r"\bdocker\s+(?:run|exec)(?:\s+-[a-zA-Z0-9=-]*)*\s+--interactive\b"),
)

# Flags / suffixes that make otherwise-interactive commands non-interactive
_NON_INTERACTIVE_INDICATORS = (
    " -c ",
    " -c'",
    ' -c"',  # python -c, bash -c, etc.
    " -e ",
    " -e'",
    ' -e"',  # node -e, perl -e, ruby -e
    " --eval ",
    " --eval=",
    " -m  ",  # python -m pytest (with space)
    " -m",  # python -mpytest (concatenated)
    " --version",
    " -V",
    " --help",  # --help is unambiguous (vs -h which may be a host flag)
    " --check",
    " --dry-run",
    " -f ",  # psql -f script.sql, etc.
    "<<",  # shell heredoc (finite script)
    " <",  # file redirect (e.g. psql < script.sql)
)

# Database / network CLIs where -h is typically a host flag (not --help).
# See _INTERACTIVE_COMMAND_CATEGORIES["db_network"] above for the actual set.


def _token_is_shell_dash_c(token: str) -> bool:
    """True if a shell argv token runs a -c script (incl. combined flags like -lc)."""
    if token in ("-c",) or token.startswith("--command"):
        return True
    if token.startswith("-") and not token.startswith("--") and len(token) > 1:
        return "c" in token[1:]
    return False


def is_interactive_command(command: str) -> bool:
    """Detect if a command is likely interactive and requires a TTY.

    Returns True for commands that would hang when executed with piped
    stdout/stderr (no TTY). This includes bare REPL invocations, editors,
    system monitors, and database CLIs.

    Commands with arguments that make them non-interactive (e.g. ``python -c``,
    ``python script.py``, ``node -e``) return False.
    """
    if not command or not command.strip():
        return False

    cmd_stripped = command.strip()

    # Check for interactive flag patterns (e.g. docker run -it)
    cmd_lower = cmd_stripped.lower()
    for pattern in _INTERACTIVE_FLAG_PATTERNS:
        if pattern.search(cmd_lower):
            return True

    # Check if non-interactive indicators are present
    for indicator in _NON_INTERACTIVE_INDICATORS:
        if indicator in cmd_lower:
            return False

    # Extract the base binary name
    # Handle: /usr/bin/python, python3, "python", env python, etc.
    parts = cmd_stripped.split()
    if not parts:
        return False

    # Skip prefix commands (e.g. "env python", "sudo python", "time python")
    _PREFIX_COMMANDS: frozenset[str] = frozenset(
        {"env", "/usr/bin/env", "sudo", "nohup", "time", "nice", "doas"}
    )
    idx = 0
    while idx < len(parts) and parts[idx] in _PREFIX_COMMANDS:
        idx += 1

    if idx >= len(parts):
        return False

    binary = os.path.basename(parts[idx].strip("'\""))
    binary_lower = binary.lower()

    # Check if binary matches an interactive binary (exact or versioned variant)
    # e.g. "python3.12" starts with "python3", "vim.tiny" starts with "vim"
    if binary not in _INTERACTIVE_BINARIES and binary_lower not in _INTERACTIVE_BINARIES:
        # Also check for versioned/suffixed variants (e.g. python3.12, psql15, mysql80, vim.tiny)
        matched = False
        for base in _INTERACTIVE_BINARIES:
            if binary_lower.startswith(base) and (
                len(binary) == len(base)
                or (
                    len(binary) > len(base)
                    and (binary[len(base)].isdigit() or binary[len(base)] in (".", "-"))
                    and not binary[len(base)].isalpha()
                )
            ):
                matched = True
                break
        if not matched:
            return False

    # Context-aware -h check: only treat -h as non-interactive for non-database/non-network binaries
    if binary_lower not in _DATABASE_NETWORK_CLIS:
        if any(arg in ("-h", "--help") for arg in parts):
            return False

    # Binary IS in the interactive set — check if it has arguments that
    # make it non-interactive (e.g. a script filename)
    remaining_args = parts[idx + 1 :]

    # Bare invocation (no args) → interactive
    if not remaining_args:
        return True

    # If the first "real" arg is a flag that we already checked above,
    # we would have returned False. So remaining args are positional
    # (e.g. a filename) → non-interactive for interpreters
    first_arg = remaining_args[0]

    # For shells: -c / -lc / bash script.sh vs interactive shell
    if binary_lower in _SHELL_BINARIES:
        if any(_token_is_shell_dash_c(a) for a in remaining_args):
            return False
        if not first_arg.startswith("-"):
            return False  # bash script.sh
        return True

    # For interpreters: stdin (-), script file, or flags
    if binary_lower in _INTERPRETER_BINARIES:
        if first_arg == "-":
            return False  # read script from stdin (incl. heredoc after shell expansion)
        if first_arg == "-c" or first_arg.startswith("-c"):
            return False  # python -c "code", python -cprint(1), etc.
        if not first_arg.startswith("-"):
            return False  # script filename
        return True

    # Remaining cases (TUIs, editors, DB/network CLIs) are interactive by
    # default; non-interactive forms (e.g. ``psql -f``, ``mysql < script``)
    # were filtered earlier by _NON_INTERACTIVE_INDICATORS.
    return True


# Files that indicate a real project
PROJECT_INDICATORS: Set[str] = {
    "package.json",
    "tsconfig.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Rakefile",
    "mix.exs",
    "CMakeLists.txt",
    "Makefile",
    "Justfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Dockerfile",
    "composer.json",
    ".sln",
    ".csproj",
    "stack.yaml",
    "cabal.project",
}

# Directories that indicate source code
SOURCE_DIRECTORIES: Set[str] = {
    "src",
    "lib",
    "app",
    "pkg",
    "cmd",
    "internal",
    "source",
    "sources",
    "components",
    "pages",
    "routes",
    "views",
    "controllers",
    "models",
    "test",
    "tests",
    "spec",
    "specs",
}

# Junk files that should be ignored when assessing directory content
JUNK_FILES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".gitkeep",
    ".keep",
}


def project_sanity_check(directory: str = ".") -> Dict[str, Any]:
    """Verify that a directory contains a real project worth operating on.

    Returns a dict with:
        is_valid_project: bool — True if meaningful project files are found
        detected_files: list — Project indicator files found
        detected_source_dirs: list — Source directories found
        reasons: list — Human-readable reasons if invalid
    """
    dir_path = Path(directory).resolve()

    if not dir_path.exists():
        return {
            "is_valid_project": False,
            "detected_files": [],
            "detected_source_dirs": [],
            "reasons": [f"Directory does not exist: {dir_path}"],
        }

    if not dir_path.is_dir():
        return {
            "is_valid_project": False,
            "detected_files": [],
            "detected_source_dirs": [],
            "reasons": [f"Path is not a directory: {dir_path}"],
        }

    detected_files: List[str] = []
    detected_source_dirs: List[str] = []
    has_git = (dir_path / ".git").exists()

    # Check for project indicator files
    for indicator in PROJECT_INDICATORS:
        if (dir_path / indicator).exists():
            detected_files.append(indicator)

    # Check for source directories
    for src_dir in SOURCE_DIRECTORIES:
        candidate = dir_path / src_dir
        try:
            if candidate.is_dir() and any(candidate.iterdir()):
                detected_source_dirs.append(src_dir)
        except OSError:
            pass

    # Check for any source code files (top-level scan only, for performance)
    has_source_files = False
    source_extensions = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".ex",
        ".exs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".swift",
        ".kt",
        ".scala",
        ".hs",
        ".ml",
    }
    try:
        for item in dir_path.iterdir():
            if item.is_file() and item.suffix in source_extensions:
                has_source_files = True
                break
    except PermissionError:
        pass

    is_valid = bool(detected_files) or bool(detected_source_dirs) or has_source_files

    reasons: List[str] = []
    if not is_valid:
        # Build helpful reasons
        try:
            all_items = [
                item.name
                for item in dir_path.iterdir()
                if item.name not in JUNK_FILES and not item.name.startswith(".")
            ]
        except PermissionError:
            all_items = []

        if not all_items:
            reasons.append("Directory is empty or contains only junk/hidden files.")
        else:
            reasons.append(
                f"No project indicators found ({', '.join(sorted(PROJECT_INDICATORS)[:5])}...). "
                f"Directory contains: {', '.join(sorted(all_items[:10]))}"
            )
        if not has_git:
            reasons.append("No .git directory found — this may not be a repository.")

    return {
        "is_valid_project": is_valid,
        "detected_files": detected_files,
        "detected_source_dirs": detected_source_dirs,
        "has_git": has_git,
        "reasons": reasons,
    }


async def resolve_git_root(working_dir: str = ".") -> Dict[str, Any]:
    """Resolve the git repository root for a given working directory.

    Returns:
        git_root: Absolute path to the git repo root (or None)
        matches_expected: True if git_root == resolve(working_dir)
        warning: A warning string if scope mismatch detected
    """
    resolved_dir = str(Path(working_dir).resolve())
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--show-toplevel",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return {
                "git_root": None,
                "matches_expected": False,
                "warning": f"Not a git repository: {stderr.decode('utf-8', errors='replace').strip()}",
            }

        git_root = stdout.decode("utf-8", errors="replace").strip()
        resolved_git_root = os.path.normpath(git_root)
        resolved_target = os.path.normpath(resolved_dir)
        matches = resolved_target.startswith(resolved_git_root)

        warning = None
        if not matches:
            warning = (
                f"Git scope mismatch: working_dir={resolved_dir} "
                f"but git root={git_root}. Operations may affect "
                f"files outside the intended project."
            )
            logger.warning(warning)

        return {
            "git_root": resolved_git_root,
            "matches_expected": matches,
            "warning": warning,
        }
    except Exception as e:
        return {
            "git_root": None,
            "matches_expected": False,
            "warning": f"Failed to resolve git root: {e}",
        }


async def get_current_branch(working_dir: str = ".") -> Optional[str]:
    """Get the current git branch name, or None if not in a repo / detached HEAD."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            branch = stdout.decode("utf-8", errors="replace").strip()
            return branch if branch != "HEAD" else None  # detached HEAD
        return None
    except Exception as e:
        logger.warning(f"Failed to get current branch: {e}")
        return None


# Patterns of files/directories that should NEVER be staged autonomously.
# Uses fnmatch-style glob patterns.
JUNK_PATTERNS: tuple[str, ...] = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "__pycache__",
    "*.py[cod]",
    "*$py.class",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".coverage",
    "htmlcov",
    ".coderAI",
    ".env",
    ".env.local",
    "*.egg-info",
    "*.egg",
    "*.so",
    "*.dylib",
    "*.dll",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    "dist",
    "build",
    "*.swp",
    "*.swo",
    "*~",
)


def filter_stageable_files(
    files: List[str],
) -> Tuple[List[str], List[str]]:
    """Filter a list of files to remove junk that should not be staged.

    Args:
        files: List of file paths (relative or absolute) to evaluate.

    Returns:
        Tuple of (allowed_files, rejected_files).
    """
    allowed: List[str] = []
    rejected: List[str] = []

    for filepath in files:
        # Check each path component against junk patterns
        parts = Path(filepath).parts
        is_junk = False

        for part in parts:
            for pattern in JUNK_PATTERNS:
                if fnmatch.fnmatch(part.lower(), pattern.lower()):
                    is_junk = True
                    break
            if is_junk:
                break

        if is_junk:
            rejected.append(filepath)
        else:
            allowed.append(filepath)

    if rejected:
        logger.info(
            f"Staging filter: rejected {len(rejected)} file(s): "
            f"{', '.join(rejected[:10])}"
            + (f" (+{len(rejected) - 10} more)" if len(rejected) > 10 else "")
        )

    return allowed, rejected


_TRUNCATION_MARKER = "... [truncated {omitted} chars] ..."


def truncate_output(
    text: str,
    *,
    max_chars: int,
    mode: str = "head_tail",
    marker: str = _TRUNCATION_MARKER,
) -> Tuple[str, bool]:
    """Cap oversized tool output, returning ``(text, was_truncated)`` (Phase 4.7).

    Single shared implementation for the git / terminal / testing tools, which
    previously each rolled their own (divergent thresholds, markers, and
    head-vs-head+tail behaviour). ``search.py`` keeps its own pagination-style
    truncation — it is genuinely different.

    * ``mode="head_tail"`` (default) keeps the first and last ``max_chars // 2``
      characters and elides the middle. Preferred for build/test/command output
      where the tail carries the summary or the failing assertion.
    * ``mode="head"`` keeps only the leading ``max_chars`` characters.

    ``marker`` is formatted with ``{omitted}`` — the number of characters
    removed — and placed at the elision point (middle for ``head_tail``, end for
    ``head``), fenced with blank lines. ``max_chars <= 0`` disables truncation.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    if mode == "head":
        omitted = len(text) - max_chars
        rendered = marker.format(omitted=omitted)
        return f"{text[:max_chars]}\n\n{rendered}", True

    if mode != "head_tail":
        raise ValueError(f"Unknown truncation mode: {mode!r}")

    half = max_chars // 2
    head = text[:half]
    tail = text[-half:] if half else ""
    omitted = len(text) - len(head) - len(tail)
    rendered = marker.format(omitted=omitted)
    return f"{head}\n\n{rendered}\n\n{tail}", True


def sanitize_for_log(text: str) -> str:
    """Backward-compatible bridge to the authoritative text redactor."""
    return redact_text(text)

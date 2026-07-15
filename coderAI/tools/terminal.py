"""Terminal tools for command execution."""

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from coderAI.core.services import get_services
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.proc import (
    command_argv,
    kill_process_group,
    new_session_kwargs,
    run_scrubbed,
    scrub_env,
)
from coderAI.system.sandbox import prepare_sandbox_launch
from coderAI.system.safeguards import is_interactive_command, truncate_output
from coderAI.tools.base import SUBPROCESS_TIMEOUT_MARGIN_SECONDS, Tool

logger = logging.getLogger(__name__)

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


def _build_blocked_regexes(patterns):
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


def _fetched_output_files(argv: List[str]) -> "set[str]":
    """Basenames of files a ``curl``/``wget`` argv writes to disk."""
    base = os.path.basename(argv[0]).lower()
    if base not in ("curl", "wget"):
        return set()
    files: "set[str]" = set()
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
    fetched: "set[str]" = set()
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

# Common command aliases: (original, replacement)
# Applied when the original is not found on PATH but the replacement is.
_COMMAND_ALIASES = [
    ("python", "python3"),
    ("pip", "pip3"),
]


def _normalize_command(command: str) -> str:
    """Normalize command for safety checks: strip, collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", command.strip()).lower()


_SHELL_METACHARS = re.compile(r"[|><&;$*?~`\\]")


def _needs_shell(command: str) -> bool:
    """Check whether *command* requires a shell to interpret metacharacters.

    Characters inside single-quoted strings do NOT trigger shell mode (e.g.
    ``echo '$HOME'`` runs fine with ``create_subprocess_exec``).
    """
    # Strip single-quoted segments, then check the remainder
    stripped = re.sub(r"'[^']*'", "", command)
    return bool(_SHELL_METACHARS.search(stripped))


# Shells that can wrap arbitrary commands — we extract the inner command
# and re-check it against the blocklist/dangerous prefixes.
_SHELL_WRAPPERS = ("bash -c ", "sh -c ", "zsh -c ", "/bin/bash -c ", "/bin/sh -c ", "/bin/zsh -c ")


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


def _resolve_working_dir(working_dir: str) -> "tuple[Optional[Path], Optional[str]]":
    """Resolve *working_dir* against the project root, rejecting escapes.

    By default the terminal tools may not ``cd`` outside the project: a
    mis-quoted path in a tool-generated command should not end up running
    ``rm``-ish things in the user's home directory. Set
    ``CODERAI_ALLOW_OUTSIDE_PROJECT=1`` to opt out when you genuinely need
    cross-repo access.

    Returns ``(Path, None)`` on success and ``(None, err)`` on rejection.
    """
    try:
        cfg = get_services().config
        project_root = Path(getattr(cfg, "project_root", ".") or ".").resolve()
    except Exception:
        # Config unavailable → treat the current directory as the project
        # root; the scope check below still runs against it.
        logger.debug("project_root config unavailable, using cwd", exc_info=True)
        project_root = Path.cwd().resolve()

    candidate = Path(working_dir).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve()
    except Exception as e:
        return None, f"Invalid working_dir {working_dir!r}: {e}"

    try:
        cfg_allow_outside = bool(getattr(get_services().config, "allow_outside_project", False))
    except Exception:
        # Fail closed: if config can't be read, keep project-scope enforcement on.
        logger.debug("allow_outside_project config unavailable, failing closed", exc_info=True)
        cfg_allow_outside = False
    if os.environ.get("CODERAI_ALLOW_OUTSIDE_PROJECT") == "1" or cfg_allow_outside:
        return resolved, None

    try:
        resolved.relative_to(project_root)
    except ValueError:
        return None, (
            f"Refusing to run outside project root. working_dir={working_dir!r} "
            f"resolves to {resolved}, which is not under {project_root}. "
            "Set CODERAI_ALLOW_OUTSIDE_PROJECT=1 to override."
        )
    return resolved, None


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


def _rewrite_command_aliases(command: str) -> str:
    """Rewrite common command aliases when the original isn't on PATH.

    For example, on macOS and newer Linux distros ``python`` often doesn't
    exist but ``python3`` does.  This helper transparently rewrites the
    command so that the LLM-generated commands work out of the box.
    """
    for original, replacement in _COMMAND_ALIASES:
        # Only rewrite if the command *starts* with the original token
        # (e.g. "python foo.py", "python -m pytest") and `original` is
        # missing from PATH while `replacement` is available.
        if command == original or command.startswith(original + " "):
            if shutil.which(original) is None and shutil.which(replacement) is not None:
                command = replacement + command[len(original) :]
                logger.debug(f"Rewrote '{original}' → '{replacement}' in command")
                break
    return command


@dataclass
class _PreparedCommand:
    """Outcome of the safety pipeline: what to spawn, how, and where.

    ``spawn_cmd`` is a string for shell spawns and an argv list for exec
    spawns — its type IS the shell-vs-exec decision.
    """

    command: str  # alias-rewritten command, for tracking / result payloads
    working_dir: str
    spawn_cmd: Union[str, List[str]]


def _prepare_command(
    command: str, working_dir: str, *, background: bool = False
) -> Union[Dict[str, Any], _PreparedCommand]:
    """Shared block/interactive/dangerous/alias/shlex pipeline for the run tools.

    Resolves the working dir, applies the safety checks, rewrites aliases, and
    splits the command for exec-vs-shell spawning. Returns the finished error
    result dict when a check fails, otherwise the prepared command.
    """
    kind = "background command" if background else "command"

    resolved_cwd, cwd_err = _resolve_working_dir(working_dir)
    if cwd_err:
        return {"success": False, "error": cwd_err, "error_code": ToolErrorCode.SCOPE}

    # Block known destructive commands (these are never allowed)
    if is_command_blocked(command):
        return {
            "success": False,
            "error": f"Command blocked for safety: {command}",
            "error_code": ToolErrorCode.BLOCKED,
            "blocked": True,
        }

    # Block interactive commands that would hang without a TTY
    if is_interactive_command(command):
        logger.warning(f"Blocked interactive {kind}: {command}")
        hint = (
            "Interactive commands cannot run in the background."
            if background
            else "Use an interactive terminal session instead."
        )
        return {
            "success": False,
            "error": f"Command appears interactive (requires TTY/user input): {command!r}. {hint}",
            "error_code": ToolErrorCode.INTERACTIVE,
            "interactive": True,
        }

    # Log dangerous commands (actual confirmation is handled by
    # requires_confirmation + the confirmation callback in ToolRegistry)
    if is_command_dangerous(command):
        logger.warning(f"Executing potentially dangerous {kind}: {command}")

    # Rewrite common aliases (e.g. python -> python3 on macOS)
    command = _rewrite_command_aliases(command)

    # Use exec (no shell) for simple commands; only shell syntax needs a shell
    spawn_cmd: Union[str, List[str]]
    if _needs_shell(command):
        spawn_cmd = command
    else:
        try:
            spawn_cmd = shlex.split(command)
        except ValueError:
            return {
                "success": False,
                "error": (
                    f"Command has malformed quoting — cannot split safely: "
                    f"{command!r}. Check for unmatched quotes."
                ),
                "error_code": ToolErrorCode.MALFORMED_COMMAND,
            }

    return _PreparedCommand(command, str(resolved_cwd), spawn_cmd)


class RunCommandParams(BaseModel):
    command: str = Field(..., description="Shell command to execute")
    working_dir: str = Field(
        ".", description="Working directory for the command (default: current)"
    )
    timeout: int = Field(60, description="Timeout in seconds (default: 60)")
    input: Optional[str] = Field(
        None, description="Optional text to send to the process stdin (max 64KB)"
    )


class RunCommandTool(Tool):
    """Tool for executing shell commands with safety checks."""

    name = "run_command"
    description = (
        "Execute a shell command and return its output. Dangerous commands require confirmation."
    )
    parameters_model = RunCommandParams
    requires_confirmation = True
    # Arbitrary command execution — no blanket allow; scope by command-prefix.
    high_risk_no_blanket = True
    approval_scope = "command"
    timeout = None
    category = "terminal"

    def resolve_timeout(self, arguments: Dict[str, Any]) -> Optional[float]:
        # Mirror execute()'s clamp so the executor's outer cap always sits
        # SUBPROCESS_TIMEOUT_MARGIN_SECONDS above run_scrubbed's inner timeout
        # — previously a run_command(timeout=600) was killed at the outer 120s,
        # bypassing the process-group SIGTERM→SIGKILL escalation.
        try:
            requested = int(arguments.get("timeout", 60))
        except (TypeError, ValueError):
            requested = 60
        return float(max(1, min(requested, 3600))) + SUBPROCESS_TIMEOUT_MARGIN_SECONDS

    async def execute(  # type: ignore[override]
        self,
        command: str,
        working_dir: str = ".",
        timeout: int = 60,
        input: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute shell command with safety checks.

        Uses direct argv for simple commands and an explicit shell argv for
        complex syntax; both pass through the shared OS sandbox boundary.
        """
        try:
            timeout = max(1, min(timeout, 3600))

            prep = _prepare_command(command, working_dir)
            if isinstance(prep, dict):
                return prep
            command = prep.command

            stdin_bytes = None
            if input is not None:
                stdin_bytes = input.encode("utf-8", errors="replace")[:65536]  # cap at 64KB

            # Spawn with a scrubbed environment (secrets never reach a
            # model-authored command) and process-group isolation; run_scrubbed
            # enforces the timeout — group SIGTERM → SIGKILL, reaping any
            # backgrounded grandchildren — and returns partial output on expiry.
            returncode, stdout, stderr, timed_out = await run_scrubbed(
                prep.spawn_cmd,
                cwd=prep.working_dir,
                timeout=timeout,
                shell=isinstance(prep.spawn_cmd, str),
                stdin=stdin_bytes,
            )

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            if timed_out:
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout} seconds",
                    "error_code": ToolErrorCode.TIMEOUT,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "returncode": returncode,
                }

            # Truncate very large output to prevent context overflow (Phase 4.7:
            # shared head+tail helper — the tail carries the command's summary).
            max_output = get_services().config.max_command_output
            stdout_str, _ = truncate_output(stdout_str, max_chars=max_output)
            stderr_str, _ = truncate_output(stderr_str, max_chars=max_output)

            return {
                "success": returncode == 0,
                "returncode": returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "command": command,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class RunBackgroundParams(BaseModel):
    command: str = Field(..., description="Shell command to execute in background")
    working_dir: str = Field(
        ".", description="Working directory for the command (default: current)"
    )
    capture_output: bool = Field(
        False,
        description="Capture stdout/stderr for later retrieval via read_bg_output (default: false)",
    )


class BgProcessInfo:
    """Tracks a background process plus its output buffer when capture is enabled."""

    def __init__(self, process: asyncio.subprocess.Process, command: str = ""):
        self.process = process
        self.command = command
        self.stdout_buf: List[str] = []
        self.stderr_buf: List[str] = []
        self._buf_bytes = 0
        self._max_buf_bytes = 65536  # 64KB total cap
        self._reader_tasks: "List[asyncio.Task[None]]" = []

    def cancel_readers(self) -> None:
        for task in self._reader_tasks:
            task.cancel()
        self._reader_tasks.clear()

    def _append(self, buf: List[str], data: str) -> None:
        if self._buf_bytes >= self._max_buf_bytes:
            return
        encoded = data.encode("utf-8", errors="replace")
        remaining = self._max_buf_bytes - self._buf_bytes
        chunk = encoded[:remaining]
        self._buf_bytes += len(chunk)
        buf.append(chunk.decode("utf-8", errors="replace"))


# Module-level registry of all tracked background processes.
_tracked_bg_processes: Dict[int, BgProcessInfo] = {}


class RunBackgroundTool(Tool):
    """Tool for starting background processes."""

    name = "run_background"
    description = "Start a command in the background (for long-running processes like servers)"
    parameters_model = RunBackgroundParams
    requires_confirmation = True
    # Arbitrary command execution — no blanket allow; scope by command-prefix.
    high_risk_no_blanket = True
    approval_scope = "command"
    category = "terminal"

    def __init__(self):
        super().__init__()
        # Instance shares the module-level process registry
        self._processes: Dict[int, BgProcessInfo] = _tracked_bg_processes

    async def execute(  # type: ignore[override]
        self, command: str, working_dir: str = ".", capture_output: bool = False
    ) -> Dict[str, Any]:
        """Start background process with tracking.

        Uses the same exec-vs-shell heuristic as ``run_command``. Both forms are
        normalized to argv before the configured OS sandbox wrapper is applied.
        """
        try:
            # Reap finished entries, then enforce the global cap — the tracked
            # registry was previously unbounded.
            self.cleanup_finished()
            try:
                cap = int(getattr(get_services().config, "max_background_processes", 10))
            except Exception:
                cap = 10
            if len(self._processes) >= cap:
                return {
                    "success": False,
                    "error": (
                        f"Too many tracked background processes ({len(self._processes)} running, "
                        f"cap {cap}). Use list_processes to inspect them and kill_process to "
                        "stop ones you no longer need before starting more."
                    ),
                    "error_code": ToolErrorCode.TOOL_ERROR,
                }

            prep = _prepare_command(command, working_dir, background=True)
            if isinstance(prep, dict):
                return prep
            command = prep.command

            stdout_target = (
                asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
            )
            stderr_target = (
                asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
            )

            # Detached lifetime (we track it rather than await it), so we can't
            # use run_scrubbed. Apply its env, process-group, and sandbox pieces
            # directly instead.
            argv = command_argv(prep.spawn_cmd, shell=isinstance(prep.spawn_cmd, str))
            launch = prepare_sandbox_launch(argv, cwd=prep.working_dir)
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                stdout=stdout_target,
                stderr=stderr_target,
                cwd=prep.working_dir,
                env=scrub_env(),
                **new_session_kwargs(),
            )

            # Track the process
            _ensure_atexit_cleanup()
            info = BgProcessInfo(process, command)
            self._processes[process.pid] = info

            if capture_output:
                # Start reader tasks to accumulate output
                async def _read_stream(stream, buf_list):
                    while True:
                        line_bytes = await stream.readline()
                        if not line_bytes:
                            break
                        info._append(buf_list, line_bytes.decode("utf-8", errors="replace"))

                if process.stdout:
                    t = asyncio.create_task(_read_stream(process.stdout, info.stdout_buf))
                    info._reader_tasks.append(t)
                if process.stderr:
                    t = asyncio.create_task(_read_stream(process.stderr, info.stderr_buf))
                    info._reader_tasks.append(t)

            return {
                "success": True,
                "pid": process.pid,
                "command": command,
                "capture_output": capture_output,
                "message": "Process started in background",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    def get_tracked_processes(self) -> Dict[int, BgProcessInfo]:
        """Get all tracked background processes for this instance."""
        return self._processes

    def cleanup_finished(self) -> int:
        """Clean up finished processes from tracking. Returns count removed."""
        finished = [
            pid for pid, info in self._processes.items() if info.process.returncode is not None
        ]
        for pid in finished:
            info = self._processes[pid]
            info.cancel_readers()
            del self._processes[pid]
        return len(finished)

    def terminate_all(self) -> int:
        """Forcefully terminate all remaining tracked processes."""
        terminated = 0
        for pid, info in dict(self._processes).items():
            info.cancel_readers()
            if info.process.returncode is None:
                try:
                    kill_process_group(info.process)
                    terminated += 1
                except Exception:
                    logger.debug(
                        "Failed to kill background process during terminate_all", exc_info=True
                    )
        self._processes.clear()
        return terminated


def _cleanup_all_background():
    """Terminate background processes from the shared module-level registry."""
    for pid, info in dict(_tracked_bg_processes).items():
        info.cancel_readers()
        if info.process.returncode is None:
            try:
                kill_process_group(info.process)
            except Exception:
                logger.debug(
                    "Failed to kill background process during atexit cleanup", exc_info=True
                )
    _tracked_bg_processes.clear()


_atexit_registered = False


def _ensure_atexit_cleanup():
    """Register cleanup handler the first time a background process is started."""
    global _atexit_registered
    if not _atexit_registered:
        import atexit

        atexit.register(_cleanup_all_background)
        _atexit_registered = True


# ---------------------------------------------------------------------------
# Process management: list background processes and kill by PID
# ---------------------------------------------------------------------------


class ListProcessesParams(BaseModel):
    pass


class ListProcessesTool(Tool):
    """List all background processes started by run_background."""

    name = "list_processes"
    description = (
        "List all background processes currently tracked by the agent "
        "(started via run_background). Shows PID, command, and running status."
    )
    category = "terminal"
    parameters_model = ListProcessesParams
    is_read_only = True

    async def execute(self) -> Dict[str, Any]:  # type: ignore[override]
        try:
            processes = []
            for pid, info in _tracked_bg_processes.items():
                processes.append(
                    {
                        "pid": pid,
                        "running": info.process.returncode is None,
                        "returncode": info.process.returncode,
                        "command": info.command,
                    }
                )
            return {
                "success": True,
                "processes": processes,
                "count": len(processes),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


class KillProcessParams(BaseModel):
    pid: int = Field(..., description="Process ID (PID) to terminate")
    force: bool = Field(False, description="Send SIGKILL instead of SIGTERM (force kill)")


class KillProcessTool(Tool):
    """Terminate a background process by PID."""

    name = "kill_process"
    description = (
        "Terminate a background process that was started with run_background. "
        "Sends SIGTERM by default; use force=true for SIGKILL."
    )
    category = "terminal"
    parameters_model = KillProcessParams
    requires_confirmation = True

    async def execute(self, pid: int, force: bool = False) -> Dict[str, Any]:  # type: ignore[override]
        try:
            info = _tracked_bg_processes.get(pid)
            if info is None:
                return {
                    "success": False,
                    "error": (
                        f"No tracked background process with PID {pid}. "
                        "Only processes started by run_background can be terminated. "
                        "Use run_command with 'kill' if you need to signal an external process."
                    ),
                }

            if info.process.returncode is not None:
                return {
                    "success": False,
                    "error": f"Process {pid} has already exited (returncode={info.process.returncode}).",
                }

            # Signal the whole process group, not just the leader — background
            # jobs are spawned with their own group (``new_session_kwargs``), so
            # a backgrounded grandchild (``bash -c 'sleep 1000 & wait'``) would
            # otherwise be orphaned. ``kill_process_group`` falls back to a
            # direct kill on Windows / when the group can't be resolved, so this
            # stays cross-platform (SIGKILL is POSIX-only; the helper maps it).
            kill_process_group(info.process, signal.SIGKILL if force else signal.SIGTERM)
            try:
                await asyncio.wait_for(info.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                kill_process_group(info.process, signal.SIGKILL)
                await info.process.wait()
            del _tracked_bg_processes[pid]
            return {
                "success": True,
                "pid": pid,
                "signal": "SIGKILL" if force else "SIGTERM",
                "message": f"Sent {'SIGKILL' if force else 'SIGTERM'} to process {pid}.",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }


# ---------------------------------------------------------------------------
# Read background process output
# ---------------------------------------------------------------------------


class ReadBgOutputParams(BaseModel):
    pid: int = Field(..., description="Process ID (PID) of the background process")


class ReadBgOutputTool(Tool):
    """Read captured output from a background process started with capture_output=True."""

    name = "read_bg_output"
    description = (
        "Read captured stdout/stderr from a background process started with capture_output=True"
    )
    category = "terminal"
    parameters_model = ReadBgOutputParams
    is_read_only = True

    async def execute(self, pid: int) -> Dict[str, Any]:  # type: ignore[override]
        try:
            info = _tracked_bg_processes.get(pid)
            if info is None:
                return {
                    "success": False,
                    "error": (
                        f"No tracked background process with PID {pid}. "
                        "Only processes started by run_background with capture_output=True "
                        "can have their output read."
                    ),
                }

            stdout_text = "".join(info.stdout_buf)
            stderr_text = "".join(info.stderr_buf)
            running = info.process.returncode is None

            return {
                "success": True,
                "pid": pid,
                "running": running,
                "returncode": info.process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

"""Subprocess-hardening helpers: env scrubbing + process-group isolation.

Shared by the terminal (`run_command`/`run_background`), `python_repl`, and
`package_manager` tools (and, later, the hooks runner) so that:

* secret-bearing environment variables never leak into a child process we
  spawn on the model's behalf (an injected command should not be able to read
  ``$OPENAI_API_KEY`` out of the environment), and
* a timed-out or cancelled command takes its whole process group down with it
  instead of orphaning grandchildren — e.g. ``bash -c 'sleep 1000 & wait'``
  leaves the inner ``sleep`` running if you only signal the direct child.

Like :mod:`coderAI.system.fsperms`, these helpers degrade gracefully on
Windows (``start_new_session`` → ``CREATE_NEW_PROCESS_GROUP``; ``killpg`` →
``taskkill /T /F``) so the callers stay cross-platform.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# ── Environment scrubbing ────────────────────────────────────────────────────

# Names/patterns of environment variables that carry credentials and must
# never reach a subprocess launched for the model. This is a *denylist* drop
# (Phase 1): every non-matching var is preserved so builds/tests still work.
# Phase 2.4 layers an allowlisted env on top of this for the hooks runner.
_SECRET_ENV_PATTERNS = (
    re.compile(r"API_?KEY", re.IGNORECASE),
    re.compile(r"SECRET", re.IGNORECASE),
    re.compile(r"TOKEN", re.IGNORECASE),
    re.compile(r"PASSWORD", re.IGNORECASE),
    re.compile(r"PASSWD", re.IGNORECASE),
    re.compile(r"CREDENTIAL", re.IGNORECASE),
    re.compile(r"_KEY$", re.IGNORECASE),
    re.compile(r"PRIVATE_KEY", re.IGNORECASE),
    re.compile(r"^AWS_", re.IGNORECASE),
    re.compile(r"^AZURE_", re.IGNORECASE),
    re.compile(r"^GOOGLE_APPLICATION_CREDENTIALS$", re.IGNORECASE),
    re.compile(r"^GCP_", re.IGNORECASE),
    re.compile(r"^OPENAI_", re.IGNORECASE),
    re.compile(r"^ANTHROPIC_", re.IGNORECASE),
    re.compile(r"^GEMINI_", re.IGNORECASE),
    re.compile(r"^DEEPSEEK_", re.IGNORECASE),
    re.compile(r"^GROQ_", re.IGNORECASE),
    re.compile(r"^HF_", re.IGNORECASE),
    re.compile(r"^HUGGINGFACE", re.IGNORECASE),
    re.compile(r"^NPM_TOKEN$", re.IGNORECASE),
    re.compile(r"^PYPI_", re.IGNORECASE),
    re.compile(r"^SLACK_", re.IGNORECASE),
    re.compile(r"^DOCKER_", re.IGNORECASE),
    re.compile(r"^STRIPE_", re.IGNORECASE),
)


def is_secret_env_var(name: str) -> bool:
    """True if *name* looks like it carries a credential and should be scrubbed."""
    return any(p.search(name) for p in _SECRET_ENV_PATTERNS)


def scrub_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return a copy of the environment with secret-bearing variables removed.

    Pass *base* to scrub a specific mapping; otherwise ``os.environ`` is used.
    Only credential-looking names are dropped — ``PATH``/``HOME``/``LANG`` and
    everything else the child legitimately needs are preserved.
    """
    env: Dict[str, str] = dict(os.environ if base is None else base)
    for name in list(env):
        if is_secret_env_var(name):
            del env[name]
    return env


# ── Minimal allowlisted env for the hooks runner (Phase 2.4) ──────────────────

# Hooks execute repo-supplied shell commands, so — unlike ``scrub_env``'s
# denylist — they get an *allowlist*: only these variables (plus ``LC_*`` and
# whatever the caller adds explicitly) survive. Everything else, including any
# not-yet-known credential var, is dropped. Keep this to genuinely inert,
# broadly-needed process/locale/toolchain vars.
_HOOK_ENV_ALLOWLIST = frozenset(
    {
        # POSIX process / shell basics
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "TMPDIR",
        "TMP",
        "TEMP",
        "HOSTNAME",
        "DISPLAY",
        # locale / terminal
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "COLORTERM",
        # Windows equivalents
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
    }
)


def build_hook_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return a *minimal allowlisted* environment for hook subprocesses.

    Unlike :func:`scrub_env` (which keeps everything not matching a secret
    pattern), this keeps only :data:`_HOOK_ENV_ALLOWLIST` names and ``LC_*``
    locale vars. Callers layer their own ``CODERAI_*`` context on top. This is
    the fail-closed base for repo-supplied hook commands so a credential-bearing
    variable can never leak into one — even a name no denylist anticipated.
    """
    src = os.environ if base is None else base
    env: Dict[str, str] = {}
    for name, val in src.items():
        upper = name.upper()
        if upper in _HOOK_ENV_ALLOWLIST or upper.startswith("LC_"):
            env[name] = val
    return env


# ── Process-group isolation ──────────────────────────────────────────────────


def new_session_kwargs() -> Dict[str, Any]:
    """kwargs for ``create_subprocess_exec``/``_shell`` that isolate the child.

    POSIX: ``start_new_session=True`` makes the child a session/group leader so
    the whole tree can be signalled via ``killpg``. Windows:
    ``CREATE_NEW_PROCESS_GROUP`` gives the equivalent handle for ``taskkill``.
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def kill_process_group(process: Any, sig: int = signal.SIGKILL) -> None:
    """Best-effort kill of the entire process group led by *process*.

    Falls back to killing only the direct child when the group cannot be
    resolved (already-reaped process, permission error, or Windows without a
    process-group handle). Never raises.
    """
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if pid is None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            _kill_direct(process)
        return

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        # Group already gone or not a group leader — fall back to the child.
        _kill_direct(process)


def _kill_direct(process: Any) -> None:
    try:
        process.kill()
    except Exception:
        logger.debug("failed to kill process %r", getattr(process, "pid", None), exc_info=True)

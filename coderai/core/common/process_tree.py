"""Process tree management, escalated killing, and secure environment scrubbing."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_SIGTERM = getattr(signal, "SIGTERM", signal.SIGTERM)
_SIGINT = getattr(signal, "SIGINT", signal.SIGINT)

SENSITIVE_ENV_PATTERN = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL|PRIVATE)", re.IGNORECASE
)


def scrubbed_parent_env(
    base_env: dict[str, str] | None = None,
    allow_keys: set[str] | None = None,
) -> dict[str, str]:
    """Return an environment dictionary stripped of sensitive secrets/tokens."""
    source = base_env if base_env is not None else os.environ
    allowed = allow_keys or set()
    cleaned: dict[str, str] = {}
    for k, v in source.items():
        if k in allowed:
            cleaned[k] = v
            continue
        if SENSITIVE_ENV_PATTERN.search(k):
            continue
        cleaned[k] = v
    return cleaned


def is_process_alive(pid: int) -> bool:
    """Check if process is still alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def kill_process_tree(pid: int, sig: int = _SIGKILL) -> bool:
    """Kill a process and its child processes."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5
            )
            return True

        # On POSIX:
        # Avoid killing our own process group
        try:
            my_pgid = os.getpgid(0)
            target_pgid = os.getpgid(pid)
            if target_pgid != my_pgid and target_pgid == pid:
                os.killpg(target_pgid, sig)
                return True
        except Exception:
            pass

        # Try to find and kill child processes
        try:
            children = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if children.returncode == 0:
                for cpid_str in children.stdout.strip().split():
                    try:
                        cpid = int(cpid_str)
                        if cpid > 0 and cpid != os.getpid():
                            kill_process_tree(cpid, sig)
                    except (ValueError, OSError):
                        pass
        except Exception:
            pass

        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        return True
    except Exception:
        return False


def escalated_kill_process_tree(
    pid: int,
    int_grace_sec: float = 0.5,
    term_grace_sec: float = 0.5,
) -> bool:
    """3-stage escalated teardown matching DeepSeek Harness specification: SIGINT -> SIGTERM -> SIGKILL."""
    if not isinstance(pid, int) or pid <= 0 or not is_process_alive(pid):
        return True

    if sys.platform == "win32":
        return kill_process_tree(pid)

    # Stage 1: SIGINT (graceful interrupt)
    kill_process_tree(pid, _SIGINT)
    deadline = time.time() + int_grace_sec
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.05)

    # Stage 2: SIGTERM (polite termination)
    kill_process_tree(pid, _SIGTERM)
    deadline = time.time() + term_grace_sec
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.05)

    # Stage 3: SIGKILL (hard immediate kill)
    kill_process_tree(pid, _SIGKILL)
    return not is_process_alive(pid)

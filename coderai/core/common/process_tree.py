"""Process-tree kill — port of deepcode core/src/common/process-tree.ts."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def kill_process_tree(pid: int, sig: int = signal.SIGKILL) -> bool:
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

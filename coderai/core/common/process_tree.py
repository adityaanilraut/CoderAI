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
        try:
            os.killpg(os.getpgid(pid), sig)
        except Exception:
            os.kill(pid, sig)
        return True
    except Exception:
        return False

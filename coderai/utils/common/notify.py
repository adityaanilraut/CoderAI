"""Task completion notification helper."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from typing import Any


def format_duration_seconds(duration_ms: float | int) -> str:
    safe_ms = max(0, duration_ms) if math.isfinite(duration_ms) else 0
    return str(int(math.floor(safe_ms / 1000)))


def build_notify_env(
    duration_ms: float | int,
    base_env: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, str]:
    ctx = context or {}
    env = dict(base_env or os.environ)
    env["DURATION"] = format_duration_seconds(duration_ms)

    env.pop("STATUS", None)
    env.pop("FAIL_REASON", None)
    env.pop("BODY", None)
    env.pop("TITLE", None)

    if ctx.get("status"):
        env["STATUS"] = str(ctx["status"])
    if ctx.get("failReason"):
        env["FAIL_REASON"] = str(ctx["failReason"])
    if ctx.get("body"):
        env["BODY"] = str(ctx["body"])
    if ctx.get("title"):
        env["TITLE"] = str(ctx["title"])

    return env


def launch_notify_script(
    notify_path: str | None,
    duration_ms: float | int,
    working_directory: str | None = None,
    configured_env: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    command_path = notify_path.strip() if notify_path else ""
    if not command_path:
        return

    merged_env = {**os.environ, **(configured_env or {})}
    env = build_notify_env(duration_ms, merged_env, context)

    kwargs: dict[str, Any] = {
        "cwd": working_directory or os.getcwd(),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        # First attempt: directly execute
        if os.path.isfile(command_path) and os.access(command_path, os.X_OK):
            subprocess.Popen([command_path], **kwargs)
        elif sys.platform != "win32" and os.path.isfile(command_path):
            # Fall back to /bin/sh so plain shell scripts run without explicit chmod +x
            subprocess.Popen(["/bin/sh", command_path], **kwargs)
        else:
            # If command_path is a shell command line (e.g. "osascript -e ...")
            subprocess.Popen(command_path, shell=True, **kwargs)
    except Exception:
        # Ignore notification execution failures
        pass

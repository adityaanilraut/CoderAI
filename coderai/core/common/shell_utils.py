"""Shell helpers — port of deepcode core/src/common/shell-utils.ts."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys

WINDOWS_GIT_LOCATIONS = [
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
]
WINDOWS_BASH_LOCATIONS = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

NUL_REDIRECT_REGEX = re.compile(r"(\d?&?>+\s*)[Nn][Uu][Ll](?=\s|$|[|&;)\n])")
_cached_git_bash_path: str | None = None


def find_git_bash_path() -> str | None:
    global _cached_git_bash_path
    if _cached_git_bash_path:
        return _cached_git_bash_path

    for c in WINDOWS_BASH_LOCATIONS:
        if pathlib.Path(c).exists():
            _cached_git_bash_path = c
            return c

    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        _cached_git_bash_path = found
        return found
    return None


def resolve_shell_path() -> str:
    if sys.platform == "win32":
        gb = find_git_bash_path()
        if gb:
            return gb
        return "bash"

    env_shell = os.environ.get("SHELL")
    if env_shell and get_shell_kind(env_shell) != "unknown":
        return env_shell
    return shutil.which("bash") or shutil.which("sh") or "/bin/bash"


def get_shell_kind(shell_path: str) -> str:
    exe = pathlib.PurePath(shell_path.replace("\\", "/")).name.lower()
    if exe in ("bash", "bash.exe"):
        return "bash"
    if exe in ("zsh", "zsh.exe"):
        return "zsh"
    return "unknown"


def build_shell_init_command(shell_path: str) -> str | None:
    kind = get_shell_kind(shell_path)
    if kind == "zsh":
        return 'ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"; if [ -f "$ZSHRC" ]; then { . "$ZSHRC"; } >/dev/null 2>&1; fi'
    if kind == "bash":
        return 'BASHRC="${BASH_ENV:-$HOME/.bashrc}"; if [ -f "$BASHRC" ]; then { . "$BASHRC"; } >/dev/null 2>&1; fi'
    return None


def build_disable_extglob_command(shell_path: str) -> str | None:
    kind = get_shell_kind(shell_path)
    if kind == "bash":
        return "shopt -u extglob 2>/dev/null || true"
    if kind == "zsh":
        return "setopt NO_EXTENDED_GLOB 2>/dev/null || true"
    return None


def rewrite_windows_null_redirect(command: str) -> str:
    return NUL_REDIRECT_REGEX.sub(r"\1/dev/null", command)


def windows_path_to_posix_path(windows_path: str) -> str:
    if windows_path.startswith(r"\\"):
        return windows_path.replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):[/\\]", windows_path)
    if drive_match:
        drive_letter = drive_match.group(1).lower()
        return f"/{drive_letter}{windows_path[2:].replace(chr(92), '/')}"
    return windows_path.replace("\\", "/")


def posix_path_to_windows_path(posix_path: str) -> str:
    if posix_path.startswith("//"):
        return posix_path.replace("/", "\\")
    cygdrive_match = re.match(r"^/cygdrive/([A-Za-z])(/|$)", posix_path)
    if cygdrive_match:
        drive_letter = cygdrive_match.group(1).upper()
        rest = posix_path[len(f"/cygdrive/{cygdrive_match.group(1)}") :]
        return f"{drive_letter}:{(rest or chr(92)).replace('/', chr(92))}"
    drive_match = re.match(r"^/([A-Za-z])(/|$)", posix_path)
    if drive_match:
        drive_letter = drive_match.group(1).upper()
        rest = posix_path[2:]
        return f"{drive_letter}:{(rest or chr(92)).replace('/', chr(92))}"
    return posix_path.replace("/", "\\")


def to_native_cwd(shell_cwd: str) -> str:
    if sys.platform != "win32":
        return shell_cwd
    return posix_path_to_windows_path(shell_cwd)


def build_shell_env(shell_path: str | None, configured_env: dict | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(configured_env or {})
    if shell_path:
        env["SHELL"] = shell_path
    env["GIT_EDITOR"] = "true"
    env["PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env

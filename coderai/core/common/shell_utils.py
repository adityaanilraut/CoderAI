""""""

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

    # Prioritize standard bash for POSIX compliance and clean subshell execution
    bash_path = shutil.which("bash") or "/bin/bash"
    if pathlib.Path(bash_path).exists():
        return bash_path

    env_shell = os.environ.get("SHELL")
    if env_shell and get_shell_kind(env_shell) != "unknown":
        return env_shell
    return shutil.which("sh") or "/bin/sh"


def get_shell_kind(shell_path: str) -> str:
    exe = pathlib.PurePath(shell_path.replace("\\", "/")).name.lower()
    if exe in ("bash", "bash.exe"):
        return "bash"
    if exe in ("zsh", "zsh.exe"):
        return "zsh"
    return "unknown"


def build_shell_init_command(shell_path: str) -> str | None:
    py_alias = "if ! command -v python >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then alias python=python3 2>/dev/null || true; fi"
    kind = get_shell_kind(shell_path)
    if kind == "zsh":
        return f'ZSHRC="${{ZDOTDIR:-$HOME}}/.zshrc"; if [ -f "$ZSHRC" ]; then {{ . "$ZSHRC"; }} >/dev/null 2>&1; fi; {py_alias}'
    if kind == "bash":
        return f'BASHRC="${{BASH_ENV:-$HOME/.bashrc}}"; if [ -f "$BASHRC" ]; then {{ . "$BASHRC"; }} >/dev/null 2>&1; fi; {py_alias}'
    return py_alias


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


ENV_OVERRIDES: dict[str, str] = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
}

SENSITIVE_ENV_EXACT_NAMES: set[str] = {
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CODERAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITLAB_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
}

SENSITIVE_ENV_PREFIXES = ("AWS_SECRET", "CODERAI_SECRET", "GITHUB_SECRET")
SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_SECRET_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN")


def is_sensitive_env_var(key: str) -> bool:
    """Determine whether an environment variable name holds sensitive secrets."""
    upper = key.upper()
    if upper in SENSITIVE_ENV_EXACT_NAMES:
        return True
    if any(upper.startswith(prefix) for prefix in SENSITIVE_ENV_PREFIXES):
        return True
    if any(upper.endswith(suffix) for suffix in SENSITIVE_ENV_SUFFIXES):
        return True
    if (
        "TOKEN" in upper
        or "SECRET" in upper
        or "PRIVATE_KEY" in upper
        or "API_KEY" in upper
        or "PASSWORD" in upper
    ):
        return True
    return False


def scrub_subprocess_env(
    env: dict[str, str], preserve_keys: set[str] | None = None
) -> dict[str, str]:
    """Return a sanitized copy of env with ambient sensitive API keys and secrets removed."""
    preserve = preserve_keys or set()
    cleaned: dict[str, str] = {}
    for k, v in env.items():
        if k in preserve or not is_sensitive_env_var(k):
            cleaned[k] = v
    return cleaned


def build_shell_env(
    shell_path: str | None,
    configured_env: dict | None = None,
    scrub_ambient: bool = True,
) -> dict[str, str]:
    """Construct child shell environment, scrubbing ambient host credentials unless explicitly configured."""
    if scrub_ambient:
        env = scrub_subprocess_env(
            dict(os.environ), preserve_keys=set((configured_env or {}).keys())
        )
    else:
        env = dict(os.environ)

    env.update(configured_env or {})
    env.update(ENV_OVERRIDES)
    if shell_path:
        env["SHELL"] = shell_path

    # Prepend Python runtime path so python/python3 points to current interpreter
    py_dir = str(pathlib.Path(sys.executable).parent)
    current_path = env.get("PATH", "")
    if py_dir not in current_path.split(os.pathsep):
        env["PATH"] = f"{py_dir}{os.pathsep}{current_path}"

    return env

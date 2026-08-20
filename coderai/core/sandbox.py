"""OS sandbox + permission-preset mapping (dsh sandbox-policy / sandbox-local).

Presets: read-only, workspace-write, danger-full-access. The first two wrap bash
spawns (Seatbelt on macOS, bwrap on Linux). Plan-mode force-ask is unchanged.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
from typing import Any

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX_MODE = "danger-full-access"  # preserve CoderAI allowAll unless a preset is set

READ_SCOPES = ("read-in-cwd", "read-out-cwd", "query-git-log")
WRITE_SCOPES = (
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "mutate-git-log",
)

PRESET_SCOPE_MAP: dict[str, dict[str, Any]] = {
    "read-only": {
        "allow": ["read-in-cwd", "query-git-log"],
        "deny": [
            "write-in-cwd",
            "write-out-cwd",
            "delete-in-cwd",
            "delete-out-cwd",
            "mutate-git-log",
        ],
        "ask": ["read-out-cwd", "network", "mcp"],
        "defaultMode": "askAll",
        "sandbox": "read-only",
    },
    "workspace-write": {
        "allow": ["read-in-cwd", "write-in-cwd", "delete-in-cwd", "query-git-log"],
        "deny": ["write-out-cwd", "delete-out-cwd"],
        "ask": ["read-out-cwd", "mutate-git-log", "network", "mcp"],
        "defaultMode": "askAll",
        "sandbox": "workspace-write",
    },
    "danger-full-access": {
        "allow": [
            "read-in-cwd",
            "read-out-cwd",
            "write-in-cwd",
            "write-out-cwd",
            "delete-in-cwd",
            "delete-out-cwd",
            "query-git-log",
            "mutate-git-log",
            "network",
            "mcp",
        ],
        "deny": [],
        "ask": [],
        "defaultMode": "allowAll",
        "sandbox": "danger-full-access",
    },
}


def parse_sandbox_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    aliases = {
        "readonly": "read-only",
        "read": "read-only",
        "workspace": "workspace-write",
        "workspacewrite": "workspace-write",
        "write": "workspace-write",
        "danger": "danger-full-access",
        "full": "danger-full-access",
        "off": "danger-full-access",
        "none": "danger-full-access",
    }
    v = aliases.get(v, v)
    return v if v in SANDBOX_MODES else None


def preset_permissions(mode: str) -> dict[str, Any]:
    parsed = parse_sandbox_mode(mode) or DEFAULT_SANDBOX_MODE
    mapped = PRESET_SCOPE_MAP[parsed]
    return {
        "allow": list(mapped["allow"]),
        "deny": list(mapped["deny"]),
        "ask": list(mapped["ask"]),
        "defaultMode": mapped["defaultMode"],
        "preset": parsed,
        "sandbox": mapped["sandbox"],
    }


def apply_preset(permissions: dict[str, Any] | None, preset: str | None) -> dict[str, Any]:
    """Overlay an explicit allow/deny/ask list on top of a preset (preset is the base)."""
    base = dict(permissions or {})
    mode = parse_sandbox_mode(preset) or parse_sandbox_mode(base.get("preset"))
    if not mode:
        return {
            "allow": list(base.get("allow") or []),
            "deny": list(base.get("deny") or []),
            "ask": list(base.get("ask") or []),
            "defaultMode": base.get("defaultMode") or "allowAll",
            "preset": None,
            "sandbox": DEFAULT_SANDBOX_MODE,
        }
    mapped = preset_permissions(mode)
    extra_allow = [s for s in (base.get("allow") or []) if s not in mapped["deny"]]
    extra_deny = list(base.get("deny") or [])
    extra_ask = list(base.get("ask") or [])
    allow = list(mapped["allow"])
    deny = list(mapped["deny"])
    ask = list(mapped["ask"])
    for scope in extra_allow:
        if scope not in allow:
            allow.append(scope)
        if scope in deny:
            deny.remove(scope)
        if scope in ask:
            ask.remove(scope)
    for scope in extra_deny:
        if scope not in deny:
            deny.append(scope)
        if scope in allow:
            allow.remove(scope)
        if scope in ask:
            ask.remove(scope)
    for scope in extra_ask:
        if scope not in ask and scope not in deny:
            ask.append(scope)
            if scope in allow:
                allow.remove(scope)
    default_mode = base.get("defaultMode") or mapped["defaultMode"]
    return {
        "allow": allow,
        "deny": deny,
        "ask": ask,
        "defaultMode": default_mode,
        "preset": mode,
        "sandbox": mapped["sandbox"],
    }


def sandbox_policy_prompt(mode: str, workspace_root: str = "") -> str:
    parsed = parse_sandbox_mode(mode) or DEFAULT_SANDBOX_MODE
    if parsed == "read-only":
        return (
            "Current file policy: read-only. Any available operation enforced by the file sandbox "
            "cannot modify files in the standing mode. Do not refuse a required modification from this "
            "policy alone: try an available tool normally and follow any denial and escalation guidance it returns."
        )
    if parsed == "workspace-write":
        root = workspace_root or "the session workspace"
        return (
            f'Current file policy: workspace-write. Operations may modify files under the session workspace: "{root}". '
            "Some platform temporary areas may also be writable."
        )
    return "Current file policy: danger-full-access. The file sandbox does not restrict file modifications by available operations."


def _seatbelt_subpath(path: str) -> str:
    escaped = path.replace("\\", "/").replace('"', '\\"')
    return f'(subpath "{escaped}")'


def build_seatbelt_profile(mode: str, workspace_root: str) -> str:
    """Generate a macOS sandbox-exec (Seatbelt) profile for the given mode."""
    tmp_paths = ["/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/dev"]
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow process-info*)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read-metadata)",
        "(allow file-read*)",
        "(allow file-ioctl)",
        '(allow file-write-data (literal "/dev/null"))',
        '(allow file-write-data (literal "/dev/dtracehelper"))',
        "(allow ipc-posix-shm)",
        "(allow network-outbound)",
        "(allow network-inbound)",
        "(allow network-bind)",
    ]
    write_roots = list(tmp_paths)
    if mode == "workspace-write":
        write_roots.append(str(pathlib.Path(workspace_root).resolve()))
    if mode in ("read-only", "workspace-write"):
        for root in write_roots:
            lines.append(f"(allow file-write* {_seatbelt_subpath(root)})")
    else:
        lines.append("(allow file-write*)")
    return "\n".join(lines) + "\n"


def sandbox_available(mode: str | None = None) -> bool:
    parsed = parse_sandbox_mode(mode) if mode else "read-only"
    if parsed == "danger-full-access":
        return True
    if sys.platform == "darwin":
        return shutil.which("sandbox-exec") is not None
    if sys.platform.startswith("linux"):
        return shutil.which("bwrap") is not None
    return False


def wrap_sandbox_command(
    argv: list[str],
    *,
    mode: str | None,
    workspace_root: str,
    cwd: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (argv, metadata) wrapping `argv` in an OS sandbox when the mode requires it."""
    parsed = parse_sandbox_mode(mode) or DEFAULT_SANDBOX_MODE
    meta: dict[str, Any] = {"sandboxMode": parsed, "sandboxApplied": False}
    if parsed == "danger-full-access":
        return argv, meta
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = build_seatbelt_profile(parsed, workspace_root)
        handle = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False, encoding="utf-8")
        try:
            handle.write(profile)
            handle.flush()
        finally:
            handle.close()
        meta["sandboxApplied"] = True
        meta["sandboxBackend"] = "seatbelt"
        meta["sandboxProfile"] = handle.name
        return ["sandbox-exec", "-f", handle.name, *argv], meta
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        root = str(pathlib.Path(workspace_root).resolve())
        bwrap = [
            "bwrap",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
        ]
        if parsed == "workspace-write":
            bwrap.extend(["--bind", root, root])
        workdir = cwd or root
        bwrap.extend(["--chdir", workdir, *argv])
        meta["sandboxApplied"] = True
        meta["sandboxBackend"] = "bwrap"
        return bwrap, meta
    meta["sandboxSkipped"] = "no sandbox backend on this platform"
    return argv, meta

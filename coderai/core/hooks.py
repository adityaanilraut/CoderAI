"""Optional Claude/Codex-style PreToolUse hooks.

Looks up `hooks.PreToolUse` in resolved settings (or `.coderai/hooks.json`) and
runs matcher-selected commands before a tool executes. A non-zero exit or
`{"decision":"block"}` JSON on stdout is a deny. Missing config is a no-op.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any

from coderai.core.tools.types import ToolExecutionContext


def load_hook_config(project_root: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(settings, dict) and isinstance(settings.get("hooks"), dict):
        return settings["hooks"]
    for candidate in (
        pathlib.Path(project_root) / ".coderai" / "hooks.json",
        pathlib.Path(project_root) / ".claude" / "settings.json",
        pathlib.Path.home() / ".coderai" / "hooks.json",
    ):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            return data["hooks"]
        if isinstance(data, dict) and "PreToolUse" in data:
            return data
    return {}


def _matchers(config: dict[str, Any]) -> list[dict[str, Any]]:
    pre = config.get("PreToolUse") or config.get("preToolUse") or []
    if isinstance(pre, dict):
        pre = [pre]
    return [item for item in pre if isinstance(item, dict)]


def _matches(matcher: str, tool_name: str) -> bool:
    matcher = (matcher or "*").strip()
    if matcher in ("", "*", "any"):
        return True
    names = {part.strip() for part in matcher.replace(",", " ").split() if part.strip()}
    return tool_name in names or tool_name.lower() in {n.lower() for n in names}


def run_pre_tool_use(
    tool_name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
    settings: dict[str, Any] | None = None,
    timeout_s: float = 8.0,
) -> str:
    """Return 'allow' or 'deny'. Fail-open when no hooks are configured."""
    config = load_hook_config(context.project_root, settings)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": args,
            "cwd": context.project_root,
            "session_id": context.session_id,
        }
    )
    for item in _matchers(config):
        if not _matches(str(item.get("matcher") or "*"), tool_name):
            continue
        hooks = item.get("hooks") or item.get("commands") or []
        if isinstance(hooks, dict):
            hooks = [hooks]
        for hook in hooks:
            command = hook.get("command") if isinstance(hook, dict) else hook
            if not isinstance(command, str) or not command.strip():
                continue
            try:
                proc = subprocess.run(
                    command,
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    cwd=context.project_root,
                    shell=True,
                    env={**os.environ, "CODERAI_HOOK_EVENT": "PreToolUse"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            stdout = (proc.stdout or "").strip()
            if stdout:
                try:
                    parsed = json.loads(stdout)
                    if isinstance(parsed, dict) and str(parsed.get("decision", "")).lower() in {
                        "block",
                        "deny",
                    }:
                        return "deny"
                except json.JSONDecodeError:
                    if stdout.lower() in ("block", "deny"):
                        return "deny"
            if proc.returncode not in (0, None):
                return "deny"
    return "allow"

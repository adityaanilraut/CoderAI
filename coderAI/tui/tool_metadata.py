"""Tool display metadata for the Textual UI: category, risk labels, previews.

These helpers feed the approval modals and timeline cards. They are display
metadata only — the enforcement path (confirmation, allowlists, egress gating)
reads the tools' declared attributes directly and never consults this module.

Risk labels are derived from the declared per-tool attributes
(``requires_confirmation``, ``is_egress``, ``is_read_only``, ``safe``) so new
tools get an honest label without editing this file; ``_RISK_OVERRIDES`` holds
the few deliberate per-tool display judgments where the derived label would
misrepresent severity.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_SKILL_STEP_RE = re.compile(r"^\s*(\d+)[\.\)\-]\s+(.+)", re.MULTILINE)

_ARG_PREVIEW_LIMIT = 240
_RESULT_PREVIEW_LIMIT = 400


def parse_skill_steps(instructions: str) -> list[Dict[str, Any]]:
    steps: list[Dict[str, Any]] = []
    for m in _SKILL_STEP_RE.finditer(instructions):
        steps.append(
            {
                "index": int(m.group(1)),
                "label": m.group(2).strip()[:120],
            }
        )
    if len(steps) > 12:
        overflow = len(steps) - 12
        steps = steps[:12]
        steps.append({"index": 0, "label": f"… and {overflow} more steps"})
    return steps


def tool_category(name: str, registry: Optional[Any] = None) -> str:
    if registry is not None:
        tool = registry.get(name)
        if tool is not None:
            cat = getattr(tool, "category", None)
            if isinstance(cat, str) and cat != "other":
                return cat
    return "other"


# Deliberate per-tool display labels where the derived one would mislead:
# most of these require confirmation (→ derived "high") but are medium-severity
# in practice; delegate_task/get_accessibility_tree derive "low" yet spawn
# sub-agents / read OS-level UI state.
_RISK_OVERRIDES: Dict[str, str] = {
    "copy_file": "medium",
    "delegate_task": "medium",
    "download_file": "medium",
    "get_accessibility_tree": "medium",
    "git_cherry_pick": "medium",
    "git_merge": "medium",
    "http_request": "medium",
}


def tool_risk(name: str, registry: Optional[Any] = None) -> str:
    override = _RISK_OVERRIDES.get(name)
    if override is not None:
        return override
    if name.startswith("mcp__"):
        return "medium"
    tool = registry.get(name) if registry is not None else None
    if tool is not None:
        if getattr(tool, "requires_confirmation", False):
            return "high"
        if getattr(tool, "is_egress", False):
            return "medium"
        if getattr(tool, "is_read_only", False) or getattr(tool, "safe", False):
            return "low"
        return "high"
    return "medium"


_RISK_FACTOR_OVERRIDES: Dict[str, list[str]] = {
    "run_command": ["Could spawn child processes", "Writes to filesystem"],
    "run_background": ["Long-running process", "Could consume resources"],
    "write_file": ["Writes to filesystem", "Could overwrite existing files"],
    "search_replace": ["Modifies files in place", "Could leave dirty working tree"],
    "apply_diff": ["Modifies files in place", "Could leave dirty working tree"],
    "delete_file": ["Permanently deletes files", "Irreversible without git"],
    "move_file": ["Moves or renames files", "Could overwrite the destination"],
    "python_repl": ["Executes arbitrary Python", "Full access to the environment"],
    "package_manager": ["Installs code that runs build steps", "Modifies dependencies"],
    "git_commit": ["Creates permanent git history", "Could push on next sync"],
    "git_push": ["Transmits data to remote", "Affects shared repository"],
    "git_checkout": ["Switches working tree", "Could cause merge conflicts"],
    "git_reset": ["Destroys uncommitted work", "Irreversible without reflog"],
}


def tool_risk_factors(name: str, registry: Optional[Any] = None) -> list[str]:
    override = _RISK_FACTOR_OVERRIDES.get(name)
    if override is not None:
        return list(override)

    factors: list[str] = []
    if name.startswith("mcp__") or name.startswith("mcp_"):
        factors.append("Runs a third-party MCP server's code")

    tool = registry.get(name) if registry is not None else None
    if tool is not None:
        if getattr(tool, "is_egress", False) or getattr(tool, "network_gate", False):
            factors.append("Sends requests over the network")
        if getattr(tool, "high_risk_no_blanket", False):
            factors.append("Broad local effect depending on arguments")
        elif (
            getattr(tool, "requires_confirmation", False)
            and not getattr(tool, "is_read_only", False)
            and not getattr(tool, "safe", False)
        ):
            factors.append("Modifies files or system state")

    if not factors:
        factors.append("Review the arguments before allowing")
    return factors


def truncate_args(args: Dict[str, Any], limit: int, *, show_count: bool = False) -> Dict[str, Any]:
    if not isinstance(args, dict):
        return {"value": str(args)[:limit]}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > limit:
            suffix = f"… ({len(v)} chars total)" if show_count else "…"
            out[k] = v[:limit] + suffix
        else:
            out[k] = v
    return out


# Keys whose values are what actually runs (shell command, REPL code). The
# approval preview must never truncate these — an attacker could push the real
# payload past the cutoff so the user approves benign-looking arguments.
_NO_TRUNCATE_APPROVAL_KEYS = ("command", "code")


def preview_args_for_approval(arguments: Dict[str, Any]) -> Dict[str, Any]:
    preview = truncate_args(arguments, 800, show_count=True)
    if isinstance(arguments, dict):
        for key in _NO_TRUNCATE_APPROVAL_KEYS:
            val = arguments.get(key)
            if isinstance(val, str):
                preview[key] = val
    return preview


def arg_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    return truncate_args(args, _ARG_PREVIEW_LIMIT)


def result_preview(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return str(result)[:_RESULT_PREVIEW_LIMIT]

    for key in ("summary", "preview", "message", "output", "content", "path"):
        val = result.get(key)
        if val:
            s = str(val).splitlines()[0] if isinstance(val, str) else str(val)
            if len(s) > _RESULT_PREVIEW_LIMIT:
                s = s[:_RESULT_PREVIEW_LIMIT] + "…"
            return s

    for key in ("count", "matches", "file_count"):
        if key in result:
            return f"{result[key]} {key.replace('_', ' ')}"

    s = str({k: v for k, v in result.items() if k not in ("success", "error")})
    return s[:_RESULT_PREVIEW_LIMIT]

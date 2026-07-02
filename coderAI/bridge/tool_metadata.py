"""Tool category, risk, and preview helpers for the chat controller."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coderAI.system_prompt import _TOOL_SECTIONS

_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9 _#\-/]*\]")

_CATEGORY_MAP = {
    "File Operations": "filesystem",
    "Terminal": "terminal",
    "Git": "git",
    "Search & Analysis": "search",
    "Web": "web",
    "Memory (Persistent)": "memory",
    "Multi-Agent Delegation": "agent",
    "MCP (Model Context Protocol)": "mcp",
}

_TOOL_CATEGORY_FALLBACK: Dict[str, str] = {}
for _section_name, _tool_names in _TOOL_SECTIONS:
    _cat = _CATEGORY_MAP.get(_section_name, "other")
    for _t in _tool_names:
        _TOOL_CATEGORY_FALLBACK[_t] = _cat

_TOOL_CATEGORY_FALLBACK["mcp_connect"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_call_tool"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_list"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_disconnect"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_list_resources"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_read_resource"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_list_prompts"] = "mcp"
_TOOL_CATEGORY_FALLBACK["mcp_get_prompt"] = "mcp"
_TOOL_CATEGORY_FALLBACK["wikipedia_search"] = "web"
_TOOL_CATEGORY_FALLBACK["read_feed"] = "web"
_TOOL_CATEGORY_FALLBACK["sitemap_discover"] = "web"

_SKILL_STEP_RE = re.compile(r"^\s*(\d+)[\.\)\-]\s+(.+)", re.MULTILINE)

_HIGH_RISK = {
    "run_command",
    "run_background",
    "write_file",
    "search_replace",
    "apply_diff",
    "git_commit",
    "git_checkout",
    "git_stash",
    "git_push",
    "git_reset",
    "git_rebase",
    "git_revert",
    "delete_file",
    "move_file",
    "kill_process",
    "run_applescript",
    "click_ui_element",
    "type_keystrokes",
}
_MEDIUM_RISK = {
    "delegate_task",
    "download_file",
    "mcp_call_tool",
    "git_merge",
    "git_cherry_pick",
    "copy_file",
    "http_request",
    "get_accessibility_tree",
}

_ARG_PREVIEW_LIMIT = 240
_RESULT_PREVIEW_LIMIT = 400


def strip_rich_markup(text: Any) -> str:
    if text is None:
        return ""
    s = str(text)
    if "[" not in s:
        return s
    return _RICH_TAG_RE.sub("", s)


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
        steps.append({"index": 0, "label": f"\u2026 and {overflow} more steps"})
    return steps


def tool_category(name: str, registry: Optional[Any] = None) -> str:
    if registry is not None:
        tool = registry.get(name)
        if tool is not None:
            cat = getattr(tool, "category", None)
            if isinstance(cat, str) and cat != "other":
                return cat
    return _TOOL_CATEGORY_FALLBACK.get(name, "other")


def tool_risk(name: str, registry: Optional[Any] = None) -> str:
    """Risk label for the approval UI (Phase 4.3).

    Derives from the tool's declared safety flags when the registry is
    available, so a new tool is labelled honestly instead of defaulting to
    "low". Unknown / MCP proxy (``mcp__…``) tools are third-party and are never
    labelled "low".
    """
    if name in _HIGH_RISK:
        return "high"
    if name in _MEDIUM_RISK:
        return "medium"
    # MCP proxy tools run a third-party server's code — at least medium.
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
        # Mutating with no opt-out — treat as high (matches the confirm gate).
        return "high"
    # Unknown tool with no registry entry — be conservative, never "low".
    return "medium"


# Curated, high-signal risk factors shown on the approval card (Phase 4.6).
# This is the single source of the per-tool risk hints — the TUI's
# ``ApprovalScreen`` renders whatever list it is handed and no longer keeps its
# own copy. Tools not listed here fall back to attribute-derived factors below.
_RISK_FACTOR_OVERRIDES: Dict[str, list[str]] = {
    "run_command": ["Could spawn child processes", "Writes to filesystem"],
    "run_background": ["Long-running process", "Could consume resources"],
    "write_file": ["Writes to filesystem", "Could overwrite existing files"],
    "search_replace": ["Modifies files in place", "Could leave dirty working tree"],
    "apply_diff": ["Modifies files in place", "Could leave dirty working tree"],
    "multi_edit": ["Modifies files in place", "Could leave dirty working tree"],
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
    """Human-readable "why this needs approval" factors for the approval card.

    Single source (Phase 4.6): a curated table for high-signal tools, then a
    fallback derived from the tool's declared safety attributes
    (``is_egress`` / ``network_gate`` / ``high_risk_no_blanket`` / mutating).
    Kept UI-agnostic — the caller renders the returned strings.
    """
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


# Security-relevant argument keys that must be shown in full in the approval
# preview — truncating what actually runs would hide it (Phase 4.4). Covers
# both ``run_command``'s ``command`` and ``python_repl``'s ``code`` (an
# attacker could otherwise hide a payload past the 800-char truncation point).
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

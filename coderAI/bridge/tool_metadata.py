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


def tool_risk(name: str) -> str:
    if name in _HIGH_RISK:
        return "high"
    if name in _MEDIUM_RISK:
        return "medium"
    return "low"


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


def preview_args_for_approval(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return truncate_args(arguments, 800, show_count=True)


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

"""System prompt for the CoderAI agent.

The canonical default prompt is built from ``SYSTEM_PROMPT_INTRO`` + a **dynamic**
tool list from ``format_tools_markdown(registry)`` + ``SYSTEM_PROMPT_TAIL`` so
documented tools always match ``ToolRegistry`` (personas, web_tools_in_main, etc.).

Note: ``Agent._get_system_prompt`` separately appends the contents of any
``.coderAI/rules/*.md`` files to the composed prompt at session start. This file
does not handle that — it produces only the framework-level prompt. Project-level
rules are an extension hook, not part of the static prompt body.
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def _load_prompt(filename: str) -> str:
    """Load a system prompt MDX file, fallback to direct filesystem path if needed."""
    try:
        return (
            (importlib.resources.files("coderAI.prompts") / filename)
            .read_text(encoding="utf-8")
            .strip()
        )
    except Exception as e:
        logger.debug("Failed to load prompt via importlib.resources: %s", e)
        path = Path(__file__).parent / "prompts" / filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as ex:
            logger.error("Failed to load prompt from filesystem fallback: %s", ex)
            raise ex


# ---------------------------------------------------------------------------
# Static narrative sections (no per-tool list here)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_INTRO = _load_prompt("intro.mdx")
SYSTEM_PROMPT_INTERACTION = _load_prompt("interaction.mdx")
SYSTEM_PROMPT_OUTPUT_STYLE = _load_prompt("output_style.mdx")


def build_environment_section(
    model: str = "",
    working_directory: str = "",
    workspace_root: str = "",
    is_git_repo: bool = False,
    platform: str = "unknown",
    *,
    auto_approve: bool = False,
    persona_name: Optional[str] = None,
    persona_description: Optional[str] = None,
    active_plan: Optional[Dict[str, Any]] = None,
) -> str:
    """Build an environment-info block injected at the top of the system prompt.

    Mirrors OpenCode's ``SystemPrompt.environment()`` pattern: model identity +
    a structured ``<env>`` block with workspace metadata so the LLM knows
    its runtime context without having to deduce it.

    Optional keyword-only fields surface dynamic agent state:

    - ``auto_approve``: emits ``Mode: YOLO (auto-approve)`` vs ``confirm-on-mutate``.
    - ``persona_name`` / ``persona_description``: emits ``Persona: <name> — <desc>``.
    - ``active_plan``: a small dict (e.g. ``{"title", "completed", "total",
      "current_desc"}``) used to render ``Active plan: <title> (c/t steps,
      current: <desc>)``. The caller is responsible for keeping this short —
      we never dump the full step list here.
    """
    import datetime

    lines = ["<env>"]
    if model:
        lines.append(f"  Model: {model}")
    if working_directory:
        lines.append(f"  Working directory: {working_directory}")
    if workspace_root:
        lines.append(f"  Workspace root: {workspace_root}")
    lines.append(f"  Git repo: {'yes' if is_git_repo else 'no'}")
    lines.append(f"  Platform: {platform}")
    lines.append(f"  Date: {datetime.date.today().isoformat()}")
    lines.append(f"  Mode: {'YOLO (auto-approve)' if auto_approve else 'confirm-on-mutate'}")
    if persona_name:
        if persona_description:
            lines.append(f"  Persona: {persona_name} — {persona_description}")
        else:
            lines.append(f"  Persona: {persona_name}")
    if active_plan:
        title = active_plan.get("title") or "(untitled)"
        completed = active_plan.get("completed", 0)
        total = active_plan.get("total", 0)
        current_desc = active_plan.get("current_desc") or "—"
        lines.append(f"  Active plan: {title} ({completed}/{total} steps, current: {current_desc})")
    lines.append("</env>")
    return "\n".join(lines)


SYSTEM_PROMPT_TAIL = _load_prompt("tail.mdx")

# Ordered sections: (heading, tool names in preferred display order).
_TOOL_SECTIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "File Operations",
        (
            "read_file",
            "write_file",
            "search_replace",
            "multi_edit",
            "apply_diff",
            "list_directory",
            "glob_search",
            "move_file",
            "copy_file",
            "delete_file",
            "create_directory",
            "file_stat",
            "file_chmod",
            "file_chown",
            "file_readlink",
        ),
    ),
    (
        "Terminal",
        ("run_command", "run_background", "list_processes", "kill_process", "read_bg_output"),
    ),
    (
        "Git",
        (
            "git_add",
            "git_status",
            "git_diff",
            "git_commit",
            "git_log",
            "git_branch",
            "git_checkout",
            "git_stash",
            "git_push",
            "git_pull",
            "git_fetch",
            "git_merge",
            "git_rebase",
            "git_revert",
            "git_reset",
            "git_show",
            "git_remote",
            "git_blame",
            "git_cherry_pick",
            "git_tag",
        ),
    ),
    ("Search & Analysis", ("text_search", "grep", "symbol_search", "semantic_search")),
    ("Code Quality", ("lint", "format", "run_tests")),
    ("Refactoring", ("refactor",)),
    ("Package Management", ("package_manager",)),
    ("Vision", ("read_image",)),
    (
        "Web",
        (
            "web_search",
            "read_url",
            "download_file",
            "http_request",
            "wikipedia_search",
            "read_feed",
            "sitemap_discover",
        ),
    ),
    ("Memory (Persistent)", ("save_memory", "recall_memory", "delete_memory")),
    ("Project Context", ("project_context", "manage_context")),
    ("Task Management", ("manage_tasks",)),
    ("Multi-Agent Delegation", ("delegate_task",)),
    ("Skills", ("use_skill",)),
    ("Python REPL", ("python_repl",)),
    ("Planning", ("plan",)),
    ("Inter-Agent Notepad", ("notepad",)),
    (
        "MCP (Model Context Protocol)",
        (
            "mcp_connect",
            "mcp_disconnect",
            "mcp_call_tool",
            "mcp_list",
            "mcp_list_resources",
            "mcp_read_resource",
            "mcp_list_prompts",
            "mcp_get_prompt",
        ),
    ),
    ("Undo / Rollback", ("undo", "undo_history")),
    (
        "Desktop Automation (macOS)",
        ("run_applescript", "get_accessibility_tree", "click_ui_element", "type_keystrokes"),
    ),
    (
        "Browser Automation (Playwright)",
        (
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_select_option",
            "browser_get_content",
            "browser_screenshot",
            "browser_evaluate",
            "browser_wait",
            "browser_close",
        ),
    ),
)


def format_tools_markdown(registry: ToolRegistry) -> str:
    """Build the ``## Available Tools`` section from whatever is in *registry*."""
    lines: List[str] = [
        "## Available Tools",
        "",
        "Only use tools listed below. If a tool is not listed here, do not assume it exists.",
        "",
    ]
    seen: set[str] = set()

    for heading, names in _TOOL_SECTIONS:
        chunk: List[str] = []
        for name in names:
            tool = registry.get(name)
            if tool is None:
                continue
            seen.add(name)
            desc = (tool.description or "").strip() or "(no description)"
            chunk.append(f"- **{name}** — {desc}")
        if chunk:
            lines.append(f"### {heading}")
            lines.append("")
            lines.extend(chunk)
            lines.append("")

    other: List[str] = []
    for name in sorted(registry.tools.keys()):
        if name in seen:
            continue
        tool = registry.tools[name]
        desc = (tool.description or "").strip() or "(no description)"
        other.append(f"- **{name}** — {desc}")

    if other:
        lines.append("### Other")
        lines.append("")
        lines.extend(other)
        lines.append("")

    mcp_extra = _format_connected_mcp_tools_appendix()
    if mcp_extra:
        lines.append(mcp_extra)

    lines.append(
        "*After `mcp_connect`, additional functions appear as `mcp__<server>__<tool>`. Use those exact names.*"
    )
    return "\n".join(lines).rstrip() + "\n"


def _format_connected_mcp_tools_appendix() -> str:
    """List currently connected MCP tools so the model matches the function schema."""
    try:
        from .tools.mcp import mcp_client
    except Exception as e:
        logger.warning("Failed to format MCP tools appendix: %s", e)
        return ""

    if not getattr(mcp_client, "discovered_tools", None):
        return ""

    blocks: List[str] = [
        "### MCP (connected servers)",
        "",
        "These names mirror your function-calling schema (`mcp__<server>__<tool>`).",
        "",
    ]
    for t in mcp_client.discovered_tools:
        sname = t.get("server", "")
        tname = t.get("name", "")
        fn = f"mcp__{sname}__{tname}"
        desc = (t.get("description") or "").strip()
        blocks.append(f"- **{fn}** — [MCP: {sname}] {desc}".rstrip())

    blocks.append("")
    return "\n".join(blocks)


def compose_default_system_prompt(
    registry: ToolRegistry,
    env_section: str = "",
) -> str:
    """Default CoderAI system prompt.

    Order: env (optional) → INTRO → dynamic tool list → INTERACTION → OUTPUT_STYLE → TAIL.
    """
    parts = []
    if env_section:
        parts.append(env_section)
    parts.extend(
        [
            SYSTEM_PROMPT_INTRO,
            format_tools_markdown(registry),
            SYSTEM_PROMPT_INTERACTION,
            SYSTEM_PROMPT_OUTPUT_STYLE,
            SYSTEM_PROMPT_TAIL,
        ]
    )
    return "\n\n".join(parts)

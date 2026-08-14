"""Typed semantic catalog for the live native tool surface.

Tool implementations own execution and safety metadata.  This catalog owns the
cross-cutting meaning consumed by capability routing, objective completion, and
time-to-first-useful-action accounting.  Keeping those meanings in one row per
tool prevents the three consumers from drifting as the registry evolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CapabilityTag = Literal[
    "browser",
    "code_search",
    "context",
    "desktop",
    "execution",
    "git",
    "mcp_control",
    "memory",
    "packages",
    "quality",
    "session_context",
    "undo",
    "vision",
    "web",
    "workspace_edit",
]
EvidenceKind = Literal["read", "mutation", "verification", "internal"]
VerificationKind = Literal["tests", "lint", "format", "command"]


@dataclass(frozen=True)
class ToolSemantics:
    """Cross-cutting runtime meaning for one native tool."""

    name: str
    capabilities: frozenset[CapabilityTag]
    universal: bool = False
    useful_action: bool = True
    evidence_kind: EvidenceKind = "read"
    workspace_mutation: bool = False
    inspect_after_mutation: bool = False
    records_inspection: bool = False
    verification: VerificationKind | None = None


def _row(
    name: str,
    *capabilities: CapabilityTag,
    universal: bool = False,
    useful_action: bool = True,
    evidence_kind: EvidenceKind = "read",
    workspace_mutation: bool = False,
    inspect_after_mutation: bool = False,
    records_inspection: bool = False,
    verification: VerificationKind | None = None,
) -> ToolSemantics:
    return ToolSemantics(
        name=name,
        capabilities=frozenset(capabilities),
        universal=universal,
        useful_action=useful_action,
        evidence_kind=evidence_kind,
        workspace_mutation=workspace_mutation,
        inspect_after_mutation=inspect_after_mutation,
        records_inspection=records_inspection,
        verification=verification,
    )


# One row for every auto-discovered tool plus the two Agent-bound tools
# (manage_context and request_plan_amendment). Capability membership is declared
# here rather than repeated as name tuples in the router.
TOOL_SEMANTICS: tuple[ToolSemantics, ...] = (
    _row("read_file", "code_search", universal=True, records_inspection=True),
    _row("grep", "code_search", universal=True),
    _row("glob_search", "code_search", universal=True),
    _row("list_directory", "code_search", universal=True),
    _row("git_status", "git", universal=True),
    _row(
        "manage_tasks",
        "context",
        universal=True,
        useful_action=False,
        evidence_kind="internal",
    ),
    _row("delegate_task", "execution", universal=True),
    _row(
        "use_skill",
        "context",
        universal=True,
        useful_action=False,
        evidence_kind="internal",
    ),
    _row("symbol_search", "code_search"),
    _row("semantic_search", "code_search"),
    _row("directory_tree", "code_search"),
    _row("read_file_slice", "code_search"),
    _row("file_stat", "code_search"),
    _row("file_readlink", "code_search"),
    _row("workspace_status", "code_search", "git"),
    _row("context_stats", "session_context"),
    _row("export_session", "session_context"),
    _row(
        "apply_diff",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "write_file",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "search_replace",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "create_directory",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
    ),
    _row(
        "move_file",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "copy_file",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "delete_file",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
    ),
    _row(
        "file_chmod",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
    ),
    _row(
        "multi_edit",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row(
        "refactor",
        "workspace_edit",
        evidence_kind="mutation",
        workspace_mutation=True,
        inspect_after_mutation=True,
    ),
    _row("run_command", "execution", verification="command"),
    _row("run_background", "execution"),
    _row("write_bg_input", "execution"),
    _row("read_bg_output", "execution"),
    _row("list_processes", "execution"),
    _row("kill_process", "execution"),
    _row("python_repl", "execution"),
    _row("run_tests", "quality", evidence_kind="verification", verification="tests"),
    _row("lint", "quality", verification="lint"),
    _row("format", "quality", verification="format"),
    _row("git_diff", "git"),
    _row("git_log", "git"),
    _row("git_add", "git"),
    _row("git_commit", "git"),
    _row("git_branch", "git"),
    _row("web_search", "web"),
    _row("read_url", "web"),
    _row("download_file", "web"),
    _row("http_request", "web"),
    _row("browser_navigate", "browser"),
    _row("browser_snapshot", "browser"),
    _row("browser_click", "browser"),
    _row("browser_type", "browser"),
    _row("browser_select_option", "browser"),
    _row("browser_get_content", "browser"),
    _row("browser_screenshot", "browser"),
    _row("browser_evaluate", "browser"),
    _row("browser_wait", "browser"),
    _row("browser_close", "browser"),
    _row("run_applescript", "desktop"),
    _row("get_accessibility_tree", "desktop"),
    _row("click_ui_element", "desktop"),
    _row("type_keystrokes", "desktop"),
    _row("package_manager", "packages", evidence_kind="mutation", workspace_mutation=True),
    _row("save_memory", "memory"),
    _row("recall_memory", "memory"),
    _row("delete_memory", "memory"),
    _row("undo", "undo"),
    _row("undo_history", "undo"),
    _row("read_image", "vision"),
    _row("manage_context", "context"),
    _row("mcp_connect", "mcp_control"),
    _row("mcp_disconnect", "mcp_control"),
    _row("mcp_list", "mcp_control"),
    _row("mcp_list_resources", "mcp_control"),
    _row("mcp_read_resource", "mcp_control"),
    _row("mcp_list_prompts", "mcp_control"),
    _row("mcp_get_prompt", "mcp_control"),
    _row("submit_plan", "context", useful_action=False, evidence_kind="internal"),
    _row(
        "request_plan_amendment",
        "context",
        useful_action=False,
        evidence_kind="internal",
    ),
    _row("internal_recovery", "context", useful_action=False, evidence_kind="internal"),
)

SEMANTICS_BY_NAME: dict[str, ToolSemantics] = {row.name: row for row in TOOL_SEMANTICS}
if len(SEMANTICS_BY_NAME) != len(TOOL_SEMANTICS):
    raise RuntimeError("Tool semantics catalog contains duplicate names")

UNIVERSAL_TOOL_NAMES: tuple[str, ...] = tuple(
    row.name for row in TOOL_SEMANTICS if row.universal
)


def semantics_for(tool_name: str) -> ToolSemantics:
    """Return declared semantics, conservatively defaulting unknown extensions."""
    return SEMANTICS_BY_NAME.get(tool_name, ToolSemantics(tool_name, frozenset()))


def tools_for_capabilities(capabilities: frozenset[CapabilityTag]) -> frozenset[str]:
    """Expand declared capability tags to native tool names."""
    return frozenset(
        row.name for row in TOOL_SEMANTICS if row.capabilities.intersection(capabilities)
    )

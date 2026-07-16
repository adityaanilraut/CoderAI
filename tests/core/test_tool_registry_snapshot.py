"""Registry snapshot guard for tool discovery.

Pins the exact set of tool names that ``discover_tools`` auto-registers.
The Phase 2 module->package splits rely on ``pkgutil.walk_packages``
descending into the new subpackages; if a conversion silently drops (or
accidentally duplicates) a tool, this test fails with the precise diff.

When you intentionally add or remove a tool, update EXPECTED_TOOLS in the
same commit.
"""

from coderAI.tools.base import ToolRegistry
from coderAI.tools.discovery import discover_tools

EXPECTED_TOOLS = {
    "apply_diff",
    "browser_click",
    "browser_close",
    "browser_evaluate",
    "browser_get_content",
    "browser_navigate",
    "browser_screenshot",
    "browser_select_option",
    "browser_snapshot",
    "browser_type",
    "browser_wait",
    "click_ui_element",
    "copy_file",
    "create_directory",
    "delegate_task",
    "delete_file",
    "delete_memory",
    "download_file",
    "file_chmod",
    "file_readlink",
    "file_stat",
    "format",
    "get_accessibility_tree",
    "git_add",
    "git_branch",
    "git_commit",
    "git_diff",
    "git_log",
    "git_status",
    "glob_search",
    "grep",
    "http_request",
    "kill_process",
    "lint",
    "list_directory",
    "list_processes",
    "manage_tasks",
    "mcp_connect",
    "mcp_disconnect",
    "mcp_get_prompt",
    "mcp_list",
    "mcp_list_prompts",
    "mcp_list_resources",
    "mcp_read_resource",
    "move_file",
    "package_manager",
    "python_repl",
    "read_bg_output",
    "read_file",
    "read_image",
    "read_url",
    "recall_memory",
    "refactor",
    "run_applescript",
    "run_background",
    "run_command",
    "run_tests",
    "save_memory",
    "search_replace",
    "semantic_search",
    "symbol_search",
    "type_keystrokes",
    "undo",
    "undo_history",
    "use_skill",
    "web_search",
    "write_file",
}


def test_discovered_tool_names_match_snapshot():
    registry = ToolRegistry()
    discover_tools(registry)
    discovered = set(registry.tools.keys())

    missing = EXPECTED_TOOLS - discovered
    unexpected = discovered - EXPECTED_TOOLS
    assert not missing and not unexpected, (
        f"Tool registry drifted.\nMissing (dropped by discovery): {sorted(missing)}\n"
        f"Unexpected (new/renamed, update snapshot): {sorted(unexpected)}"
    )


def test_discovered_tool_count():
    registry = ToolRegistry()
    discover_tools(registry)
    assert len(registry.tools) == len(EXPECTED_TOOLS) == 67

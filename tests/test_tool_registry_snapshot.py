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
    "file_chown",
    "file_readlink",
    "file_stat",
    "format",
    "get_accessibility_tree",
    "git_add",
    "git_blame",
    "git_branch",
    "git_checkout",
    "git_cherry_pick",
    "git_commit",
    "git_diff",
    "git_fetch",
    "git_log",
    "git_merge",
    "git_pull",
    "git_push",
    "git_rebase",
    "git_remote",
    "git_reset",
    "git_revert",
    "git_show",
    "git_stash",
    "git_status",
    "git_tag",
    "glob_search",
    "grep",
    "http_request",
    "kill_process",
    "lint",
    "list_directory",
    "list_processes",
    "manage_tasks",
    "mcp_call_tool",
    "mcp_connect",
    "mcp_disconnect",
    "mcp_list",
    "move_file",
    "multi_edit",
    "notepad",
    "package_manager",
    "plan",
    "project_context",
    "python_repl",
    "read_bg_output",
    "read_feed",
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
    "sitemap_discover",
    "symbol_search",
    "text_search",
    "type_keystrokes",
    "undo",
    "undo_history",
    "use_skill",
    "web_search",
    "wikipedia_search",
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
    assert len(registry.tools) == len(EXPECTED_TOOLS) == 87

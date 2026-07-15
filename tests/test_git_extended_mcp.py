"""Bundled git_extended MCP server: discovery skip + stdio protocol smoke."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coderAI.tools.base import ToolRegistry
from coderAI.tools.discovery import discover_tools
from coderAI.tools.git_extended import EXTENDED_GIT_TOOLS
from coderAI.tools.mcp import (
    BUNDLED_GIT_EXTENDED_SERVER,
    MCPClient,
    bundled_mcp_servers,
    effective_mcp_servers,
)


EXTENDED_NAMES = {t.name for t in EXTENDED_GIT_TOOLS}


def test_extended_git_tools_not_auto_discovered() -> None:
    registry = ToolRegistry()
    discover_tools(registry)
    overlap = EXTENDED_NAMES & set(registry.tools)
    assert not overlap, f"extended git tools leaked into native registry: {sorted(overlap)}"


def test_core_git_tools_still_native() -> None:
    registry = ToolRegistry()
    discover_tools(registry)
    for name in (
        "git_status",
        "git_diff",
        "git_add",
        "git_commit",
        "git_log",
        "git_branch",
    ):
        assert name in registry.tools


def test_bundled_git_extended_in_effective_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coderAI.tools.mcp.mcp_servers_path",
        lambda: tmp_path / "mcp_servers.json",
    )
    data = effective_mcp_servers()
    assert BUNDLED_GIT_EXTENDED_SERVER in data["mcpServers"]
    entry = data["mcpServers"][BUNDLED_GIT_EXTENDED_SERVER]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "coderAI.mcp_servers.git_extended"]


def test_user_override_disables_bundled_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    BUNDLED_GIT_EXTENDED_SERVER: {
                        "transport": "stdio",
                        "command": "python3",
                        "args": ["-m", "coderAI.mcp_servers.git_extended"],
                        "disabled": True,
                    }
                }
            }
        )
    )
    monkeypatch.setattr("coderAI.tools.mcp.mcp_servers_path", lambda: path)
    data = effective_mcp_servers()
    assert data["mcpServers"][BUNDLED_GIT_EXTENDED_SERVER].get("disabled") is True


@pytest.mark.asyncio
async def test_git_extended_mcp_stdio_lists_and_calls_tools(tmp_path: Path) -> None:
    """End-to-end: connect via MCPClient and call git_status-like show tool."""
    # Use a real mini repo so git_show has something to inspect.
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo,
        check=True,
        env=env,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        env=env,
        capture_output=True,
    )
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        env=env,
        capture_output=True,
    )

    client = MCPClient()
    bundled = bundled_mcp_servers()[BUNDLED_GIT_EXTENDED_SERVER]
    result = await client.connect_stdio(
        BUNDLED_GIT_EXTENDED_SERVER,
        bundled["command"],
        bundled["args"],
    )
    assert result.get("success"), result
    discovered = set(result.get("tools") or [])
    assert EXTENDED_NAMES <= discovered

    call = await client.call_tool(
        BUNDLED_GIT_EXTENDED_SERVER,
        "git_show",
        {"ref": "HEAD", "repo_path": str(repo)},
    )
    assert call.get("success"), call
    # Content is JSON-encoded tool result
    payload = json.loads(call["content"])
    assert payload.get("success") is True
    assert "init" in payload.get("output", "") or payload.get("output")

    await client.disconnect(BUNDLED_GIT_EXTENDED_SERVER)

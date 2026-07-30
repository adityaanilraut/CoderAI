"""Tests for MCP config scopes, env expansion, and catalog presets."""

from __future__ import annotations

import json

from coderAI.tools.mcp_catalog import get_preset, list_presets
from coderAI.tools.mcp_config import (
    build_stdio_env,
    effective_mcp_servers,
    entry_content_hash,
    expand_env_string,
    import_mcp_servers_from_file,
    load_project_mcp_servers,
    normalize_server_entry,
    project_server_status,
    resolve_mcp_cwd,
    save_mcp_server_to_scope,
    set_project_approval,
)
from coderAI.tools.mcp import _shape_tool_result


def test_expand_env_string_basic(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert expand_env_string("x=${FOO}/y") == "x=bar/y"
    assert expand_env_string("${MISSING:-fallback}") == "fallback"
    assert "${MISSING}" in expand_env_string("${MISSING}")


def test_build_stdio_env_overlays_only_listed_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    env = build_stdio_env({"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}", "PLAIN": "ok"})
    assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_xxx"
    assert env["PLAIN"] == "ok"
    # Host secrets not listed in config must not appear
    assert "OPENAI_API_KEY" not in env


def test_resolve_mcp_cwd_relative(tmp_path):
    sub = tmp_path / "work"
    sub.mkdir()
    assert resolve_mcp_cwd("work", project_root=tmp_path) == sub.resolve()
    assert resolve_mcp_cwd(None, project_root=tmp_path) == tmp_path.resolve()


def test_normalize_claude_and_opencode_shapes():
    claude = normalize_server_entry(
        {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"A": "1"}}
    )
    assert claude["transport"] == "stdio"
    assert claude["env"]["A"] == "1"

    remote = normalize_server_entry({"type": "remote", "url": "https://example.com/mcp"})
    assert remote["transport"] == "http"

    oc = normalize_server_entry(
        {
            "type": "local",
            "command": ["npx", "-y", "x"],
            "environment": {"K": "V"},
            "enabled": False,
        }
    )
    assert oc["transport"] == "stdio"
    assert oc["command"] == "npx"
    assert oc["args"] == ["-y", "x"]
    assert oc["env"]["K"] == "V"
    assert oc["disabled"] is True


def test_project_scope_pending_until_approved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from coderAI.system.config import config_manager

    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "cfg")
    (tmp_path / "cfg").mkdir()

    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fetch": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-fetch"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # Untrusted: project servers ignored
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=False)
    assert "fetch" not in merged["mcpServers"]

    # Trusted but pending
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["fetch"]["_connect_blocked"] == "pending"
    assert merged["mcpServers"]["fetch"].get("disabled") is True

    entry = load_project_mcp_servers(tmp_path)["fetch"]
    set_project_approval("fetch", approved=True, entry=entry, project_root=tmp_path)
    assert project_server_status("fetch", entry, project_root=tmp_path) == "approved"
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["fetch"].get("_connect_blocked") is None
    assert not merged["mcpServers"]["fetch"].get("disabled")


def test_local_overrides_project_and_user(tmp_path, monkeypatch):
    from coderAI.system.config import config_manager

    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "cfg")
    (tmp_path / "cfg").mkdir()
    monkeypatch.chdir(tmp_path)

    save_mcp_server_to_scope(
        "svc", {"command": "npx", "args": ["user"]}, "user", project_root=tmp_path
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": "npx", "args": ["project"]}}}),
        encoding="utf-8",
    )
    set_project_approval(
        "svc",
        approved=True,
        entry={"command": "npx", "args": ["project"]},
        project_root=tmp_path,
    )
    save_mcp_server_to_scope(
        "svc", {"command": "npx", "args": ["local"]}, "local", project_root=tmp_path
    )

    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["svc"]["args"] == ["local"]
    assert merged["mcpServers"]["svc"]["_scope"] == "local"


def _disable_scope_fixture(tmp_path, monkeypatch):
    """Configure the same server name in the local, project, and user scopes."""
    from coderAI.system.config import config_manager

    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "cfg")
    (tmp_path / "cfg").mkdir()
    monkeypatch.chdir(tmp_path)


def test_disable_writes_to_the_scope_that_owns_the_entry(tmp_path, monkeypatch):
    """A user-scope flag is overwritten by any higher-precedence scope.

    ``effective_mcp_servers`` merges local → project → user → bundled, so writing
    ``disabled`` into ``mcp_servers.json`` for a local-scope server had no effect
    at all and the toggle appeared to do nothing.
    """
    _disable_scope_fixture(tmp_path, monkeypatch)
    from coderAI.tools.mcp import set_mcp_server_disabled

    save_mcp_server_to_scope(
        "svc", {"command": "npx", "args": ["local"]}, "local", project_root=tmp_path
    )

    assert set_mcp_server_disabled("svc", True, project_root=tmp_path) is True
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["svc"]["disabled"] is True
    assert merged["mcpServers"]["svc"]["_scope"] == "local"
    # The user-scope config must not have been used as the dumping ground.
    assert not (tmp_path / "cfg" / "mcp_servers.json").exists()

    assert set_mcp_server_disabled("svc", False, project_root=tmp_path) is True
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert not merged["mcpServers"]["svc"].get("disabled")


def test_disable_project_scope_server_marks_it_rejected(tmp_path, monkeypatch):
    """``.mcp.json`` is shared repo state, so the per-user approval store is used."""
    _disable_scope_fixture(tmp_path, monkeypatch)
    from coderAI.tools.mcp import set_mcp_server_disabled

    entry = {"command": "npx", "args": ["project"]}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"svc": entry}}), encoding="utf-8"
    )
    set_project_approval("svc", approved=True, entry=entry, project_root=tmp_path)

    assert set_mcp_server_disabled("svc", True, project_root=tmp_path) is True
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["svc"]["_connect_blocked"] == "rejected"
    assert merged["mcpServers"]["svc"]["disabled"] is True
    # The repo's own config file must be left untouched.
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["svc"] == entry

    assert set_mcp_server_disabled("svc", False, project_root=tmp_path) is True
    merged = effective_mcp_servers(project_root=tmp_path, workspace_trusted=True)
    assert merged["mcpServers"]["svc"].get("_connect_blocked") is None


def test_disable_unknown_server_still_reports_failure(tmp_path, monkeypatch):
    _disable_scope_fixture(tmp_path, monkeypatch)
    from coderAI.tools.mcp import set_mcp_server_disabled

    assert set_mcp_server_disabled("nope", True, project_root=tmp_path) is False


def test_entry_hash_changes_on_edit():
    a = {"command": "npx", "args": ["a"]}
    b = {"command": "npx", "args": ["b"]}
    assert entry_content_hash(a) != entry_content_hash(b)


def test_import_mcp_servers_from_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    imported = import_mcp_servers_from_file(path)
    assert imported["fs"]["transport"] == "stdio"
    assert imported["fs"]["command"] == "npx"


def test_catalog_presets():
    names = {p["name"] for p in list_presets()}
    assert {"filesystem", "github", "fetch", "memory", "context7", "sentry"} <= names
    gh = get_preset("github")
    assert "GITHUB_TOKEN" in gh["required_env"]
    assert gh["entry"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_TOKEN}"


def test_shape_tool_result_includes_non_text():
    shaped = _shape_tool_result(
        {
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "image", "mimeType": "image/png", "data": "abcd"},
                {"type": "resource", "uri": "file:///tmp/x"},
            ],
            "structuredContent": {"ok": True},
            "isError": False,
        }
    )
    assert shaped["success"] is True
    assert "hello " in shaped["content"]
    assert "MCP image" in shaped["content"]
    assert "MCP resource" in shaped["content"]
    assert "structuredContent" in shaped["content"]


def test_init_request_advertises_roots_and_elicitation():
    from coderAI.tools.mcp import MCPClient

    client = MCPClient()
    req = client._init_request(1)
    caps = req["params"]["capabilities"]
    assert "roots" in caps
    assert "elicitation" in caps
    assert req["params"]["protocolVersion"] == MCPClient.PREFERRED_PROTOCOL_VERSION


def test_list_changed_notification_refreshes_tools():
    import asyncio

    from coderAI.tools.mcp import MCPClient

    client = MCPClient()
    entry = {
        "transport": "stdio",
        "pending": {},
        "tools": [],
        "server_info": {"capabilities": {"tools": {}, "resources": {}, "prompts": {}}},
    }
    client.servers["srv"] = entry

    async def fake_paginate(server_name, ent, method, item_key, first_response=None):
        if method == "tools/list":
            return [
                {
                    "name": "new_tool",
                    "description": "d",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        return []

    client._paginate_entry = fake_paginate  # type: ignore[method-assign]

    async def fake_discover(server_name, ent):
        return [], []

    client._discover_extras_for_entry = fake_discover  # type: ignore[method-assign]

    asyncio.run(client._handle_notification("srv", entry, "notifications/tools/list_changed", {}))
    assert client._schemas_dirty is True
    assert any(t.get("name") == "new_tool" for t in client.discovered_tools)

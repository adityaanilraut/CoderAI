"""Coverage for the /mcp bridge handlers (list_mcp_servers, toggle_mcp_server)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import coderAI.tools.mcp as mcp_mod
from coderAI.tui.commands import _cmd_list_mcp_servers, _cmd_toggle_mcp_server


class FakeBridge:
    """Minimal UIBridge stand-in that records emitted events."""

    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))

    def names(self):
        return [e for e, _ in self.events]

    def last(self, name):
        for e, f in reversed(self.events):
            if e == name:
                return f
        return None


@pytest.fixture
def cfg(tmp_path):
    """Point the persisted MCP config at a temp file and yield a writer."""
    target = tmp_path / "mcp_servers.json"
    with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
        yield lambda data: mcp_mod.save_mcp_servers({"mcpServers": data})


def _fake_client(servers):
    client = MagicMock()
    client.servers = servers
    client.disconnect = AsyncMock(return_value={"success": True})
    client.connect_stdio = AsyncMock(return_value={"success": True, "tools_discovered": 3})
    client.connect_sse = AsyncMock(return_value={"success": True, "tools_discovered": 1})
    client.connect_http = AsyncMock(return_value={"success": True, "tools_discovered": 2})
    return client


def test_list_merges_connected_and_configured(cfg, monkeypatch):
    cfg({"off_srv": {"command": "npx", "args": [], "disabled": True}})
    client = _fake_client({"live_srv": {"transport": "stdio", "tools": [{"name": "t1"}]}})
    monkeypatch.setattr(mcp_mod, "mcp_client", client)

    bridge = FakeBridge()
    asyncio.run(_cmd_list_mcp_servers(bridge, {}))

    payload = bridge.last("available_mcp_servers")
    rows = {r["name"]: r for r in payload["servers"]}
    assert rows["live_srv"]["connected"] is True
    assert rows["live_srv"]["tools"] == 1
    assert rows["off_srv"]["connected"] is False
    assert rows["off_srv"]["disabled"] is True
    # Sorted by name, including the bundled git server.
    assert [r["name"] for r in payload["servers"]] == ["git_extended", "live_srv", "off_srv"]


def test_list_includes_bundled_server(cfg, monkeypatch):
    cfg({})
    monkeypatch.setattr(mcp_mod, "mcp_client", _fake_client({}))

    bridge = FakeBridge()
    asyncio.run(_cmd_list_mcp_servers(bridge, {}))

    rows = bridge.last("available_mcp_servers")["servers"]
    assert rows == [
        {
            "name": "git_extended",
            "connected": False,
            "disabled": False,
            "degraded": False,
            "tools": 0,
            "transport": "stdio",
        }
    ]
    assert "info" not in bridge.names()


def test_toggle_disconnects_connected_and_disables(cfg, monkeypatch):
    cfg({"fetch": {"command": "npx", "args": []}})
    client = _fake_client({"fetch": {"transport": "stdio", "tools": []}})
    monkeypatch.setattr(mcp_mod, "mcp_client", client)

    bridge = FakeBridge()
    asyncio.run(_cmd_toggle_mcp_server(bridge, {"server": "fetch"}))

    client.disconnect.assert_awaited_once_with("fetch")
    assert "off" in bridge.last("success")["message"]
    # Persisted as disabled so it won't auto-reconnect next session.
    assert mcp_mod.load_mcp_servers()["mcpServers"]["fetch"]["disabled"] is True


def test_toggle_connects_configured_and_enables(cfg, monkeypatch):
    cfg({"fetch": {"command": "npx", "args": ["x"], "disabled": True}})
    client = _fake_client({})  # not currently connected
    monkeypatch.setattr(mcp_mod, "mcp_client", client)

    bridge = FakeBridge()
    asyncio.run(_cmd_toggle_mcp_server(bridge, {"server": "fetch"}))

    client.connect_stdio.assert_awaited_once_with("fetch", "npx", ["x"])
    assert "on" in bridge.last("success")["message"]
    assert "disabled" not in mcp_mod.load_mcp_servers()["mcpServers"]["fetch"]


def test_toggle_connects_http_server(cfg, monkeypatch):
    # Regression: an http-transport server must re-enable via connect_http, not
    # fall through to connect_stdio (which would treat the URL as a command).
    cfg(
        {
            "remote": {
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "headers": {"Authorization": "Bearer x"},
                "disabled": True,
            }
        }
    )
    client = _fake_client({})  # not currently connected
    monkeypatch.setattr(mcp_mod, "mcp_client", client)

    bridge = FakeBridge()
    asyncio.run(_cmd_toggle_mcp_server(bridge, {"server": "remote"}))

    client.connect_http.assert_awaited_once_with(
        "remote", "https://mcp.example.com/mcp", {"Authorization": "Bearer x"}
    )
    client.connect_stdio.assert_not_awaited()
    assert "on" in bridge.last("success")["message"]
    assert "disabled" not in mcp_mod.load_mcp_servers()["mcpServers"]["remote"]


def test_toggle_unknown_server_warns(cfg, monkeypatch):
    cfg({})
    monkeypatch.setattr(mcp_mod, "mcp_client", _fake_client({}))

    bridge = FakeBridge()
    asyncio.run(_cmd_toggle_mcp_server(bridge, {"server": "ghost"}))

    assert bridge.last("warning") is not None
    assert "ghost" in bridge.last("warning")["message"]


def test_toggle_requires_name(cfg, monkeypatch):
    monkeypatch.setattr(mcp_mod, "mcp_client", _fake_client({}))

    bridge = FakeBridge()
    asyncio.run(_cmd_toggle_mcp_server(bridge, {}))

    assert "Usage" in bridge.last("warning")["message"]

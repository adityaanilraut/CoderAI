"""MCP discovery, dynamic disable, and transport lifetime guarantees."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from coderai.mcp.client import McpClient
from coderai.mcp.manager import McpManager, McpToolEntry


class FakeClient:
    instances = []
    started = None
    release = None

    def __init__(self, name, config):
        self.server_name = name
        self.connected = False
        self._tools = [{"name": "tool", "inputSchema": {"type": "object"}}]
        self.disconnect_count = 0
        self.instances.append(self)

    def set_on_disconnect(self, handler):
        self.on_disconnect = handler

    async def connect(self):
        if self.started is not None:
            self.started.add(self.server_name)
            await self.release.wait()
        self.connected = True

    async def disconnect(self):
        self.disconnect_count += 1
        self.connected = False

    def is_connected(self):
        return self.connected

    async def list_prompts(self, **kwargs):
        return []

    async def list_resources(self, **kwargs):
        return []


@pytest.fixture
def clients(monkeypatch):
    import coderai.mcp.manager as module

    FakeClient.instances = []
    FakeClient.started = None
    FakeClient.release = None
    monkeypatch.setattr(module, "McpClient", FakeClient)
    return FakeClient


@pytest.mark.asyncio
async def test_independent_servers_start_concurrently(clients):
    clients.started = set()
    clients.release = asyncio.Event()
    manager = McpManager()
    task = asyncio.create_task(
        manager.initialize({"slow": {"command": "x"}, "fast": {"command": "y"}})
    )
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if len(clients.started) == 2:
                break
        assert clients.started == {"slow", "fast"}
    finally:
        clients.release.set()
        await task
    assert len(manager.list_tools()) == 2


@pytest.mark.asyncio
async def test_dynamic_disable_disconnects_and_purges_tools(clients):
    manager = McpManager()
    await manager.initialize({"server": {"command": "x"}})
    old = clients.instances[0]
    assert manager.is_mcp_tool("mcp__server__tool")
    await manager.sync_servers({"server": {"command": "x", "enabled": False}})
    assert old.disconnect_count == 1
    assert not manager.is_mcp_tool("mcp__server__tool")
    assert manager.clients == []
    assert manager.get_status()[0].status == "disabled"


@pytest.mark.asyncio
async def test_in_place_config_edits_reconnect(clients):
    config = {"server": {"command": "x", "env": {"VAR": "before"}}}
    manager = McpManager()
    await manager.initialize(config)
    config["server"]["env"]["VAR"] = "after"
    await manager.sync_servers(config)
    assert len(clients.instances) == 2
    assert clients.instances[0].disconnect_count == 1
    await manager.sync_servers({})
    assert "server" in config  # manager removal must not mutate caller's mapping


@pytest.mark.asyncio
async def test_cancelled_discovery_reaps_client(clients):
    clients.started = set()
    clients.release = asyncio.Event()
    manager = McpManager()
    task = asyncio.create_task(manager.initialize({"server": {"command": "x"}}))
    while not clients.started:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients.instances[0].disconnect_count == 1
    assert manager.clients == []
    assert manager.tools == []


def test_complex_schema_preserves_server_contract(clients):
    schema = {
        "type": "object",
        "$defs": {"value": {"type": "integer", "minimum": 1}},
        "properties": {"value": {"$ref": "#/$defs/value"}},
        "oneOf": [{"required": ["value"]}, {"required": ["other"]}],
        "additionalProperties": False,
    }
    manager = McpManager()
    client = clients("server", {})
    manager.tools.append(
        McpToolEntry("server", "tool", "mcp__server__tool", {"inputSchema": schema}, client)
    )
    exported = manager.get_mcp_tool_definitions()[0]["function"]["parameters"]
    assert exported == schema
    exported["$defs"]["value"]["minimum"] = 20
    assert schema["$defs"]["value"]["minimum"] == 1


@pytest.mark.asyncio
async def test_failed_transport_send_clears_pending_request():
    client = McpClient("server", {"command": "unused"})
    client.transport = Mock()
    client.transport.is_connected.return_value = True
    client.transport.send.side_effect = RuntimeError("broken pipe")
    with pytest.raises(RuntimeError, match="broken pipe"):
        await client._request("ping", {})
    assert client._pending == {}


@pytest.mark.asyncio
async def test_late_and_duplicate_responses_do_not_raise_loop_errors():
    client = McpClient("server", {"command": "unused"})
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        future = loop.create_future()
        client._pending[1] = future
        client._on_transport_message({"id": 1, "result": "first"})
        client._on_transport_message({"id": 1, "result": "duplicate"})
        await asyncio.sleep(0)
        assert future.result() == "first"
        future = loop.create_future()
        client._pending[2] = future
        client._on_transport_message({"id": 2, "result": "late"})
        future.cancel()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_cancelled_handshake_closes_transport():
    client = McpClient("server", {"command": "unused"})
    started = asyncio.Event()

    async def connect(**kwargs):
        started.set()
        await asyncio.Event().wait()

    client.transport = Mock()
    client.transport.connect = connect
    client.transport.disconnect = AsyncMock()
    task = asyncio.create_task(client.connect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.transport.disconnect.assert_awaited_once()
    assert client._pending == {}


@pytest.mark.asyncio
async def test_invalid_server_constructor_does_not_stall_other_servers(clients, monkeypatch):
    import coderai.mcp.manager as module

    def factory(name, config):
        if name == "invalid":
            raise ValueError("invalid transport")
        return clients(name, config)

    monkeypatch.setattr(module, "McpClient", factory)
    manager = McpManager()
    await manager.initialize({"invalid": {"command": "x"}, "valid": {"command": "y"}})
    statuses = {status.name: status.status for status in manager.get_status()}
    assert statuses == {"invalid": "failed", "valid": "ready"}


@pytest.mark.asyncio
async def test_disable_during_startup_cannot_restore_stale_tools(clients):
    clients.started = set()
    clients.release = asyncio.Event()
    manager = McpManager()
    initial = asyncio.create_task(manager.initialize({"server": {"command": "x"}}))
    while not clients.started:
        await asyncio.sleep(0)
    await manager.sync_servers({"server": {"command": "x", "enabled": False}})
    clients.release.set()
    await initial
    assert manager.clients == []
    assert manager.tools == []
    assert manager.get_status()[0].status == "disabled"
    assert clients.instances[0].disconnect_count == 1


@pytest.mark.asyncio
async def test_old_disconnect_notification_cannot_purge_replacement(clients):
    manager = McpManager()
    await manager.initialize({"server": {"command": "old"}})
    old = clients.instances[0]
    await manager.sync_servers({"server": {"command": "new"}})
    old.on_disconnect("late disconnect")
    assert manager.get_status()[0].status == "ready"
    assert manager.clients == [clients.instances[1]]
    assert len(manager.tools) == 1


@pytest.mark.asyncio
async def test_tools_list_changed_refreshes_schema_and_notifies_registry(clients, monkeypatch):
    def set_notification_handler(self, handler):
        self.notification = handler

    async def list_tools(self, **kwargs):
        return self._tools

    monkeypatch.setattr(
        clients, "set_notification_handler", set_notification_handler, raising=False
    )
    monkeypatch.setattr(clients, "list_tools", list_tools, raising=False)
    manager = McpManager()
    changed = Mock()
    manager.set_on_tools_list_changed(changed)
    await manager.initialize({"server": {"command": "x"}})
    client = clients.instances[0]
    client._tools = [{"name": "new", "inputSchema": {"type": "object"}}]
    changed.reset_mock()
    client.notification("notifications/tools/list_changed", {})
    for _ in range(20):
        await asyncio.sleep(0)
        if manager.is_mcp_tool("mcp__server__new"):
            break
    assert manager.is_mcp_tool("mcp__server__new")
    assert not manager.is_mcp_tool("mcp__server__tool")
    changed.assert_called_once()
    await manager.disconnect()
    assert manager._notification_refreshes == {}


@pytest.mark.asyncio
async def test_stale_refresh_does_not_replace_new_client_schema(clients, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def list_tools(self, **kwargs):
        started.set()
        await release.wait()
        return [{"name": "stale"}]

    monkeypatch.setattr(clients, "list_tools", list_tools, raising=False)
    manager = McpManager()
    await manager.initialize({"server": {"command": "old"}})
    task = asyncio.create_task(manager.hot_reload_tools("server"))
    await started.wait()
    await manager.sync_servers({"server": {"command": "new"}})
    release.set()
    await task
    assert manager.is_mcp_tool("mcp__server__tool")
    assert not manager.is_mcp_tool("mcp__server__stale")

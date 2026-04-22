"""Tests for MCPClient, MCPListTool, and MCPCallTool."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.mcp import MCPClient, MCPListTool, MCPCallTool, MCPConnectTool, _normalize_parameters_schema


class TestMCPClient:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = MCPClient()

    def test_initial_state(self):
        assert self.client.servers == {}
        assert self.client.discovered_tools == []

    def test_get_tools_empty(self):
        tools = self.client.get_tools_as_openai_format()
        assert tools == []

    def test_get_tools_format(self):
        self.client.discovered_tools = [
            {
                "server": "myserver",
                "name": "mytool",
                "description": "does stuff",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        tools = self.client.get_tools_as_openai_format()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "mcp__myserver__mytool"
        assert "myserver" in tools[0]["function"]["description"]

    def test_get_tools_normalizes_string_root_schema(self):
        self.client.discovered_tools = [
            {
                "server": "s",
                "name": "t",
                "description": "d",
                "input_schema": {"type": "string"},
            }
        ]
        tools = self.client.get_tools_as_openai_format()
        p = tools[0]["function"]["parameters"]
        assert p.get("type") == "object"
        assert "value" in p.get("properties", {})

    def test_connect_rejects_server_name_with_reserved_separator(self):
        result = asyncio.run(self.client.connect_stdio("bad__srv", "echo", []))
        assert not result["success"]
        assert "server_name" in result["error"].lower() or "__" in result["error"]

    def test_call_tool_not_connected(self):
        result = asyncio.run(
            self.client.call_tool("notconnected", "sometool", {})
        )
        assert not result["success"]
        assert "not connected" in result["error"]

    def test_call_tool_is_error_true(self):
        """MCP isError:true must propagate as success=False."""
        client = MCPClient()
        fake_process = MagicMock()
        fake_process.returncode = None
        fake_process.stdin = MagicMock()
        fake_process.stdin.write = MagicMock()
        fake_process.stdin.drain = AsyncMock()

        error_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "something went wrong"}],
                "isError": True,
            },
        }

        async def fake_read_response(stdout, expected_id, timeout=10):
            return error_response

        client.servers["srv"] = {"process": fake_process, "tools": []}
        client._next_id = 1

        with patch.object(client, "_read_response", side_effect=fake_read_response):
            result = asyncio.run(client.call_tool("srv", "bad_tool", {}))

        assert result["success"] is False
        assert "error" in result
        assert "something went wrong" in result["error"]

    def test_call_tool_is_error_false_still_succeeds(self):
        """Normal MCP responses (isError absent or false) return success=True."""
        client = MCPClient()
        fake_process = MagicMock()
        fake_process.returncode = None
        fake_process.stdin = MagicMock()
        fake_process.stdin.write = MagicMock()
        fake_process.stdin.drain = AsyncMock()

        ok_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "all good"}],
            },
        }

        async def fake_read_response(stdout, expected_id, timeout=10):
            return ok_response

        client.servers["srv"] = {"process": fake_process, "tools": []}
        client._next_id = 1

        with patch.object(client, "_read_response", side_effect=fake_read_response):
            result = asyncio.run(client.call_tool("srv", "ok_tool", {}))

        assert result["success"] is True
        assert result["content"] == "all good"

    def test_connect_command_not_found(self):
        result = asyncio.run(
            self.client.connect_stdio("test", "this_command_does_not_exist_xyz_mcp")
        )
        assert not result["success"]
        assert "not found" in result["error"].lower() or "Command not found" in result["error"]

    def test_disconnect_not_connected(self):
        result = asyncio.run(self.client.disconnect("nonexistent"))
        assert not result["success"]

    def test_next_id_increments(self):
        id1 = self.client._get_next_id()
        id2 = self.client._get_next_id()
        id3 = self.client._get_next_id()
        assert id2 == id1 + 1
        assert id3 == id2 + 1


class TestMCPListTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = MCPListTool()

    def test_list_empty(self):
        import coderAI.tools.mcp as mcp_mod
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = MCPClient()
        try:
            result = asyncio.run(self.tool.execute())
            assert result["success"]
            assert result["connected_servers"] == 0
            assert result["total_tools"] == 0
        finally:
            mcp_mod.mcp_client = original

    def test_list_with_servers(self):
        import coderAI.tools.mcp as mcp_mod
        fake_client = MCPClient()
        fake_client.servers = {
            "srv1": {"tools": [{"name": "t1"}, {"name": "t2"}], "server_info": {}}
        }
        fake_client.discovered_tools = [
            {"server": "srv1", "name": "t1", "description": "", "input_schema": {}},
            {"server": "srv1", "name": "t2", "description": "", "input_schema": {}},
        ]
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = fake_client
        try:
            result = asyncio.run(self.tool.execute())
            assert result["success"]
            assert result["connected_servers"] == 1
            assert result["total_tools"] == 2
            assert "srv1" in result["servers"]
        finally:
            mcp_mod.mcp_client = original


class TestMCPCallTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = MCPCallTool()

    def test_call_not_connected_server(self):
        import coderAI.tools.mcp as mcp_mod
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = MCPClient()
        try:
            result = asyncio.run(
                self.tool.execute(server_name="missing", tool_name="tool", arguments={})
            )
            assert not result["success"]
        finally:
            mcp_mod.mcp_client = original


class TestMCPConnectTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = MCPConnectTool()

    def test_connect_missing_binary(self):
        import coderAI.tools.mcp as mcp_mod
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = MCPClient()
        try:
            result = asyncio.run(
                self.tool.execute(
                    server_name="test",
                    command="binary_that_does_not_exist_xyz",
                    args=[],
                )
            )
            assert not result["success"]
        finally:
            mcp_mod.mcp_client = original

"""Tests for MCPClient, MCPListTool, and MCPCallTool."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.mcp import MCPClient, MCPListTool, MCPCallTool, MCPConnectTool


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

    def test_call_tool_not_connected(self):
        result = asyncio.run(
            self.client.call_tool("notconnected", "sometool", {})
        )
        assert not result["success"]
        assert "not connected" in result["error"]

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

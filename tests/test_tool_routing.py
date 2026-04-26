"""Tests for MCP tool name parsing and routing helpers."""

import asyncio
from unittest.mock import AsyncMock, patch

from coderAI.tool_routing import (
    call_mcp_tool_by_function_name,
    is_mcp_function_name,
    parse_mcp_function_name,
)


class TestParseMcpFunctionName:
    def test_simple(self):
        assert parse_mcp_function_name("mcp__srv__tool") == ("srv", "tool")

    def test_tool_with_double_underscore(self):
        assert parse_mcp_function_name("mcp__srv__a__b__c") == ("srv", "a__b__c")

    def test_invalid(self):
        assert parse_mcp_function_name("read_file") is None
        assert parse_mcp_function_name("mcp__onlyone") is None
        assert parse_mcp_function_name("") is None
        assert parse_mcp_function_name("mcp__srv__ ") is None

    def test_single_character_segments_are_valid(self):
        assert parse_mcp_function_name("mcp__s__t") == ("s", "t")


class TestIsMcpFunctionName:
    def test(self):
        assert is_mcp_function_name("mcp__x__y") is True
        assert is_mcp_function_name("mcp__bad") is False


class TestCallMcpToolByFunctionName:
    def test_dispatches(self):
        from coderAI.tools import mcp as mcp_module

        with patch.object(
            mcp_module.mcp_client, "call_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = {"success": True, "content": "ok"}
            r = asyncio.run(
                call_mcp_tool_by_function_name(
                    "mcp__myserver__do_thing", {"a": 1}
                )
            )
        assert r["success"] is True
        mock_call.assert_awaited_once_with("myserver", "do_thing", {"a": 1})

"""Tests for MCP tool name parsing and routing helpers."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from coderAI.core.tool_routing import (
    MAX_PROVIDER_FUNCTION_NAME_LENGTH,
    build_mcp_function_name,
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

    def test_rejects_provider_invalid_or_oversized_names(self):
        assert parse_mcp_function_name("mcp__srv__bad.tool") is None
        assert parse_mcp_function_name("mcp__srv__tool name") is None
        assert parse_mcp_function_name("mcp__s__" + "t" * 64) is None

    def test_builder_enforces_exact_provider_limit(self):
        tool = "t" * (MAX_PROVIDER_FUNCTION_NAME_LENGTH - len("mcp__s__"))
        assert len(build_mcp_function_name("s", tool)) == MAX_PROVIDER_FUNCTION_NAME_LENGTH
        with pytest.raises(ValueError, match="provider limit"):
            build_mcp_function_name("s", tool + "t")


class TestIsMcpFunctionName:
    def test(self):
        assert is_mcp_function_name("mcp__x__y") is True
        assert is_mcp_function_name("mcp__bad") is False


class TestCallMcpToolByFunctionName:
    def test_dispatches(self):
        from coderAI.tools import mcp as mcp_module

        with (
            patch.object(
                mcp_module.mcp_client,
                "discovered_tools",
                [{"server": "myserver", "name": "do_thing"}],
            ),
            patch.object(mcp_module.mcp_client, "servers", {"myserver": {}}),
            patch.object(mcp_module.mcp_client, "call_tool", new_callable=AsyncMock) as mock_call,
        ):
            mock_call.return_value = {"success": True, "content": "ok"}
            r = asyncio.run(call_mcp_tool_by_function_name("mcp__myserver__do_thing", {"a": 1}))
        assert r["success"] is True
        mock_call.assert_awaited_once_with("myserver", "do_thing", {"a": 1})

    def test_rejects_unadvertised_tool(self):
        from coderAI.tools import mcp as mcp_module

        with (
            patch.object(mcp_module.mcp_client, "discovered_tools", []),
            patch.object(mcp_module.mcp_client, "call_tool", new_callable=AsyncMock) as mock_call,
        ):
            r = asyncio.run(call_mcp_tool_by_function_name("mcp__myserver__invented", {}))

        assert r["success"] is False
        assert "not currently advertised" in r["error"]
        mock_call.assert_not_awaited()

    def test_rejects_tool_from_degraded_server(self):
        from coderAI.tools import mcp as mcp_module

        with (
            patch.object(
                mcp_module.mcp_client,
                "discovered_tools",
                [{"server": "myserver", "name": "do_thing"}],
            ),
            patch.object(mcp_module.mcp_client, "servers", {"myserver": {"degraded": True}}),
            patch.object(mcp_module.mcp_client, "call_tool", new_callable=AsyncMock) as mock_call,
        ):
            r = asyncio.run(call_mcp_tool_by_function_name("mcp__myserver__do_thing", {}))

        assert r["success"] is False
        mock_call.assert_not_awaited()

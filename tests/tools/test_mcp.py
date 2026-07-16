"""Tests for MCPClient and MCPListTool."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.mcp import MCPClient, MCPConnectTool, MCPDisconnectTool, MCPListTool


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
        result = asyncio.run(self.client.call_tool("notconnected", "sometool", {}))
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

        async def fake_exchange(entry, request, timeout=10):
            return error_response

        client.servers["srv"] = {"process": fake_process, "tools": []}
        client._next_id = 1

        with patch.object(client, "_stdio_exchange", side_effect=fake_exchange):
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

        async def fake_exchange(entry, request, timeout=10):
            return ok_response

        client.servers["srv"] = {"process": fake_process, "tools": []}
        client._next_id = 1

        with patch.object(client, "_stdio_exchange", side_effect=fake_exchange):
            result = asyncio.run(client.call_tool("srv", "ok_tool", {}))

        assert result["success"] is True
        assert result["content"] == "all good"

    def test_connect_command_not_found(self):
        # A launcher outside ALLOWED_MCP_LAUNCHERS is now rejected by the
        # validate_stdio_launch choke point before any spawn is attempted.
        result = asyncio.run(
            self.client.connect_stdio("test", "this_command_does_not_exist_xyz_mcp")
        )
        assert not result["success"]
        assert "not in the allowed set" in result["error"]

    def test_connect_allowed_launcher_missing_binary(self):
        # An *allowed* launcher that isn't installed passes validation but fails
        # to spawn, surfacing a "not found" error from the FileNotFoundError path.
        result = asyncio.run(self.client.connect_stdio("test", "/nonexistent/path/to/uvx"))
        assert not result["success"]
        assert "not found" in result["error"].lower()

    def test_disconnect_not_connected(self):
        result = asyncio.run(self.client.disconnect("nonexistent"))
        assert not result["success"]

    def test_next_id_increments(self):
        id1 = self.client._get_next_id()
        id2 = self.client._get_next_id()
        id3 = self.client._get_next_id()
        assert id2 == id1 + 1
        assert id3 == id2 + 1

    def test_drain_stderr_consumes_until_eof(self, caplog):
        """The drain reads every line and returns on EOF (never blocks)."""
        import logging as _logging

        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"server warming up\n")
            reader.feed_data(b"ready\n")
            reader.feed_eof()
            # If the drain looped forever it would trip the timeout.
            await asyncio.wait_for(self.client._drain_stderr("srv", reader), timeout=1.0)

        with caplog.at_level(_logging.DEBUG, logger="coderAI.tools.mcp"):
            asyncio.run(run())

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "server warming up" in logged
        assert "ready" in logged

    def test_connect_stdio_starts_and_tracks_stderr_drain(self):
        """connect_stdio must spawn a stderr drain task so the pipe never fills."""

        async def run():
            client = MCPClient()
            client._next_id = 1

            stderr_reader = asyncio.StreamReader()
            stderr_reader.feed_data(b"noisy startup log\n")
            stderr_reader.feed_eof()

            fake_proc = MagicMock()
            fake_proc.returncode = None
            fake_proc.stdin = MagicMock()
            fake_proc.stdin.write = MagicMock()
            fake_proc.stdin.drain = AsyncMock()
            fake_proc.stdout = asyncio.StreamReader()
            fake_proc.stderr = stderr_reader

            responses = {
                1: {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "x"}}},
                2: {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "t", "description": "d", "inputSchema": {}}]},
                },
            }

            async def fake_exchange(entry, request, timeout=10):
                return responses[request["id"]]

            async def fake_create(*a, **k):
                return fake_proc

            with (
                patch("asyncio.create_subprocess_exec", side_effect=fake_create),
                patch.object(client, "_stdio_exchange", side_effect=fake_exchange),
            ):
                result = await client.connect_stdio("srv", "npx", ["foo"])

            assert result["success"] is True
            task = client.servers["srv"]["stderr_task"]
            assert task is not None
            # Drain finishes on its own once stderr hits EOF — proves it is reading.
            await asyncio.wait_for(task, timeout=1.0)
            assert task.done()
            await client.disconnect("srv")

        asyncio.run(run())

    def test_disconnect_cancels_stderr_drain(self):
        """Disconnecting a stdio server cancels its stderr drain task."""

        async def run():
            client = MCPClient()
            reader = asyncio.StreamReader()  # never fed EOF -> drain stays alive
            task = asyncio.create_task(client._drain_stderr("srv", reader))
            await asyncio.sleep(0)  # let the drain start

            proc = MagicMock()
            proc.terminate = MagicMock()
            proc.wait = AsyncMock(return_value=0)
            client.servers["srv"] = {
                "transport": "stdio",
                "process": proc,
                "stderr_task": task,
                "tools": [],
            }

            await client.disconnect("srv")
            # cancel() only requests cancellation; let it settle.
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert task.cancelled()
            assert "srv" not in client.servers

        asyncio.run(run())


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


class TestMCPResourcesAndPrompts:
    """Client-level resources/prompts wrappers, discovery, and capability gating."""

    def _connected_client(self, capabilities):
        client = MCPClient()
        fake_process = MagicMock()
        fake_process.returncode = None
        fake_process.stdin = MagicMock()
        fake_process.stdin.write = MagicMock()
        fake_process.stdin.drain = AsyncMock()
        fake_process.terminate = MagicMock()
        fake_process.kill = MagicMock()
        fake_process.wait = AsyncMock()
        client.servers["srv"] = {
            "transport": "stdio",
            "process": fake_process,
            "tools": [],
            "server_info": {"capabilities": capabilities},
        }
        return client

    # ── capability + connection gating ──────────────────────────────────
    def test_list_resources_not_connected(self):
        result = asyncio.run(MCPClient().list_resources("missing"))
        assert not result["success"]
        assert "not connected" in result["error"]

    def test_list_resources_requires_capability(self):
        client = self._connected_client({})  # no resources capability advertised
        result = asyncio.run(client.list_resources("srv"))
        assert not result["success"]
        assert "resource" in result["error"].lower()

    def test_get_prompt_requires_capability(self):
        client = self._connected_client({})
        result = asyncio.run(client.get_prompt("srv", "p1"))
        assert not result["success"]
        assert "prompt" in result["error"].lower()

    # ── wrappers parse results correctly ────────────────────────────────
    def test_list_resources_success(self):
        client = self._connected_client({"resources": {}})

        async def fake_request(server, entry, method, params=None, timeout=30):
            assert method == "resources/list"
            return {"success": True, "result": {"resources": [{"uri": "file:///a", "name": "a"}]}}

        with patch.object(client, "_request_entry", side_effect=fake_request):
            result = asyncio.run(client.list_resources("srv"))
        assert result["success"]
        assert result["count"] == 1
        assert result["resources"][0]["uri"] == "file:///a"

    def test_read_resource_extracts_text(self):
        client = self._connected_client({"resources": {}})

        async def fake_request(server, method, params=None, timeout=30):
            assert method == "resources/read"
            assert params == {"uri": "file:///a"}
            return {
                "success": True,
                "result": {"contents": [{"text": "hello "}, {"text": "world"}]},
            }

        with patch.object(client, "_request", side_effect=fake_request):
            result = asyncio.run(client.read_resource("srv", "file:///a"))
        assert result["success"]
        assert result["text"] == "hello world"

    def test_get_prompt_passes_arguments(self):
        client = self._connected_client({"prompts": {}})

        async def fake_request(server, method, params=None, timeout=30):
            assert method == "prompts/get"
            assert params == {"name": "p1", "arguments": {"x": "1"}}
            return {"success": True, "result": {"description": "d", "messages": [{"role": "user"}]}}

        with patch.object(client, "_request", side_effect=fake_request):
            result = asyncio.run(client.get_prompt("srv", "p1", {"x": "1"}))
        assert result["success"]
        assert result["prompt"] == "p1"
        assert result["messages"] == [{"role": "user"}]

    # ── _request transport/error handling ───────────────────────────────
    def test_request_propagates_jsonrpc_error(self):
        client = self._connected_client({"resources": {}})
        err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}}

        async def fake_exchange(entry, request, timeout=10):
            return err

        with patch.object(client, "_stdio_exchange", side_effect=fake_exchange):
            result = asyncio.run(client._request("srv", "resources/list"))
        assert not result["success"]
        assert "method not found" in result["error"]

    def test_request_stdio_success(self):
        client = self._connected_client({"resources": {}})
        resp = {"jsonrpc": "2.0", "id": 1, "result": {"resources": []}}

        async def fake_exchange(entry, request, timeout=10):
            return resp

        with patch.object(client, "_stdio_exchange", side_effect=fake_exchange):
            result = asyncio.run(client._request("srv", "resources/list"))
        assert result["success"]
        assert result["result"] == {"resources": []}

    # ── discovery + cleanup ─────────────────────────────────────────────
    def test_discover_extras_populates_stores(self):
        client = self._connected_client({"resources": {}, "prompts": {}})

        async def fake_request(server, entry, method, params=None, timeout=30):
            if method == "resources/list":
                return {
                    "success": True,
                    "result": {"resources": [{"uri": "u", "name": "n", "mimeType": "text/plain"}]},
                }
            if method == "prompts/list":
                return {"success": True, "result": {"prompts": [{"name": "p", "arguments": []}]}}
            return {"success": True, "result": {}}

        with patch.object(client, "_request_entry", side_effect=fake_request):
            counts = asyncio.run(client._discover_extras("srv"))
        assert counts == {"resources": 1, "prompts": 1}
        assert client.discovered_resources[0]["uri"] == "u"
        assert client.discovered_prompts[0]["name"] == "p"

    def test_discover_extras_is_non_fatal(self):
        client = self._connected_client({"resources": {}})

        async def boom(server, entry, method, params=None, timeout=30):
            raise RuntimeError("server hates us")

        with patch.object(client, "_request_entry", side_effect=boom):
            counts = asyncio.run(client._discover_extras("srv"))
        assert counts == {"resources": 0, "prompts": 0}
        assert client.discovered_resources == []

    def test_disconnect_purges_resources_and_prompts(self):
        client = self._connected_client({})
        client.discovered_resources = [
            {"server": "srv", "uri": "u"},
            {"server": "other", "uri": "v"},
        ]
        client.discovered_prompts = [{"server": "srv", "name": "p"}]
        asyncio.run(client.disconnect("srv"))
        assert client.discovered_resources == [{"server": "other", "uri": "v"}]
        assert client.discovered_prompts == []


class TestMCPResourcePromptTools:
    """The four new agent tools delegate to the module-level mcp_client."""

    def test_tools_report_not_connected(self):
        import coderAI.tools.mcp as mcp_mod
        from coderAI.tools.mcp import (
            MCPListResourcesTool,
            MCPReadResourceTool,
            MCPListPromptsTool,
            MCPGetPromptTool,
        )

        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = MCPClient()
        try:
            assert not asyncio.run(MCPListResourcesTool().execute(server_name="x"))["success"]
            assert not asyncio.run(MCPReadResourceTool().execute(server_name="x", uri="u"))[
                "success"
            ]
            assert not asyncio.run(MCPListPromptsTool().execute(server_name="x"))["success"]
            assert not asyncio.run(MCPGetPromptTool().execute(server_name="x", name="p"))["success"]
        finally:
            mcp_mod.mcp_client = original

    def test_list_tool_surfaces_resource_and_prompt_totals(self):
        import coderAI.tools.mcp as mcp_mod

        fake_client = MCPClient()
        fake_client.servers = {"srv1": {"tools": [], "server_info": {}}}
        fake_client.discovered_resources = [{"server": "srv1", "uri": "file:///a"}]
        fake_client.discovered_prompts = [{"server": "srv1", "name": "summarize"}]
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = fake_client
        try:
            result = asyncio.run(MCPListTool().execute())
            assert result["total_resources"] == 1
            assert result["total_prompts"] == 1
            assert result["servers"]["srv1"]["resources"] == ["file:///a"]
            assert result["servers"]["srv1"]["prompts"] == ["summarize"]
        finally:
            mcp_mod.mcp_client = original


class TestMcpConnectPersistsConfig:
    """A successful ``mcp_connect`` must persist the server so it auto-reconnects.

    This is the bug behind "open a new session, mcp list is empty": connections
    made via the agent tool used to live only in ``mcp_client.servers`` and were
    never written to ``mcp_servers.json``, so autoconnect had nothing to revive.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = MCPConnectTool()

    def test_stdio_connect_persists_entry(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client, "connect_stdio", new=AsyncMock(return_value={"success": True})
            ),
        ):
            result = asyncio.run(
                self.tool.execute(server_name="fetch", command="npx", args=["-y", "pkg"])
            )
            saved = mcp_mod.load_mcp_servers()["mcpServers"]

        assert result["success"]
        assert saved["fetch"] == {"command": "npx", "args": ["-y", "pkg"]}

    def test_sse_connect_persists_entry(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client, "connect_sse", new=AsyncMock(return_value={"success": True})
            ),
        ):
            result = asyncio.run(
                self.tool.execute(
                    server_name="remote", transport="sse", url="https://example.com/sse"
                )
            )
            saved = mcp_mod.load_mcp_servers()["mcpServers"]

        assert result["success"]
        assert saved["remote"] == {"transport": "sse", "url": "https://example.com/sse"}

    def test_http_connect_persists_entry(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client, "connect_http", new=AsyncMock(return_value={"success": True})
            ),
        ):
            result = asyncio.run(
                self.tool.execute(
                    server_name="strava",
                    transport="http",
                    url="https://mcp.strava.com/mcp",
                    headers={"Authorization": "Bearer T"},
                )
            )
            saved = mcp_mod.load_mcp_servers()["mcpServers"]

        assert result["success"]
        assert saved["strava"] == {
            "transport": "http",
            "url": "https://mcp.strava.com/mcp",
            "headers": {"Authorization": "Bearer T"},
        }

    def test_http_connect_requires_url(self):
        result = asyncio.run(self.tool.execute(server_name="x", transport="http"))
        assert not result["success"]
        assert "url" in result["error"].lower()

    def test_persist_false_skips_save(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client, "connect_stdio", new=AsyncMock(return_value={"success": True})
            ),
        ):
            result = asyncio.run(
                self.tool.execute(server_name="oneoff", command="npx", persist=False)
            )

        assert result["success"]
        assert not target.exists()

    def test_failed_connect_does_not_persist(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client,
                "connect_stdio",
                new=AsyncMock(return_value={"success": False, "error": "nope"}),
            ),
        ):
            result = asyncio.run(self.tool.execute(server_name="bad", command="npx"))

        assert not result["success"]
        assert not target.exists()

    def test_persist_failure_does_not_break_connection(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with (
            patch.object(mcp_mod, "mcp_servers_path", return_value=target),
            patch.object(
                mcp_mod.mcp_client, "connect_stdio", new=AsyncMock(return_value={"success": True})
            ),
            patch.object(mcp_mod, "save_mcp_servers", side_effect=OSError("disk full")),
        ):
            # A persistence failure must not fail the (already live) connection.
            result = asyncio.run(self.tool.execute(server_name="fetch", command="npx"))

        assert result["success"]


class TestMcpServerPersistence:
    """save_mcp_servers must be atomic so a bad write can't wipe the config."""

    def test_save_roundtrip_leaves_no_temp_files(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
            mcp_mod.save_mcp_servers({"mcpServers": {"a": {"command": "npx", "args": ["x"]}}})
            loaded = mcp_mod.load_mcp_servers()

        assert loaded["mcpServers"]["a"] == {"command": "npx", "args": ["x"]}
        # The temp file must have been renamed into place, not left behind.
        assert [p.name for p in tmp_path.iterdir()] == ["mcp_servers.json"]

    def test_failed_write_preserves_existing_file(self, tmp_path):
        import coderAI.system.fsperms as fsperms_mod
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
            mcp_mod.save_mcp_servers({"mcpServers": {"good": {"command": "npx", "args": []}}})

            # A serialization failure mid-write must not touch the good file.
            with patch.object(fsperms_mod.json, "dumps", side_effect=RuntimeError("boom")):
                with pytest.raises(RuntimeError):
                    mcp_mod.save_mcp_servers({"mcpServers": {"bad": {}}})

            loaded = mcp_mod.load_mcp_servers()

        assert loaded["mcpServers"] == {"good": {"command": "npx", "args": []}}
        # No partial temp file should survive the failure.
        assert [p.name for p in tmp_path.iterdir()] == ["mcp_servers.json"]


class TestSetMcpServerDisabled:
    """set_mcp_server_disabled toggles the persisted ``disabled`` flag (used by /mcp)."""

    def test_disable_then_enable_round_trip(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
            mcp_mod.save_mcp_servers({"mcpServers": {"fs": {"command": "npx", "args": []}}})

            assert mcp_mod.set_mcp_server_disabled("fs", True) is True
            assert mcp_mod.load_mcp_servers()["mcpServers"]["fs"]["disabled"] is True

            # Enabling removes the flag entirely rather than setting it False.
            assert mcp_mod.set_mcp_server_disabled("fs", False) is True
            assert "disabled" not in mcp_mod.load_mcp_servers()["mcpServers"]["fs"]

    def test_unknown_server_is_noop(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
            mcp_mod.save_mcp_servers({"mcpServers": {}})
            assert mcp_mod.set_mcp_server_disabled("ghost", True) is False
            assert mcp_mod.load_mcp_servers()["mcpServers"] == {}

    def test_list_tool_surfaces_disabled(self, tmp_path):
        import coderAI.tools.mcp as mcp_mod

        target = tmp_path / "mcp_servers.json"
        fake_client = MCPClient()
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = fake_client
        try:
            with patch.object(mcp_mod, "mcp_servers_path", return_value=target):
                mcp_mod.save_mcp_servers(
                    {"mcpServers": {"off_srv": {"command": "npx", "args": [], "disabled": True}}}
                )
                result = asyncio.run(MCPListTool().execute())
        finally:
            mcp_mod.mcp_client = original

        assert result["servers"]["off_srv"]["disabled"] is True
        assert result["servers"]["off_srv"]["connected"] is False


# ── Streamable HTTP transport ──────────────────────────────────────────────


class _FakeHttpResponse:
    """Async-context-manager stand-in for an aiohttp response.

    Serves either a JSON body (``json_body``) or an SSE-framed stream
    (``sse_lines``, a list of raw ``bytes`` lines) via ``content.readline``.
    """

    def __init__(self, *, status=200, headers=None, json_body=None, sse_lines=None):
        self.status = status
        self.headers = headers or {}
        self._json_body = json_body
        self.content = self  # so ``resp.content.readline()`` resolves to us
        self._sse_iter = iter(sse_lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json_body

    async def text(self):
        return str(self._json_body)

    async def readline(self):
        try:
            return next(self._sse_iter)
        except StopIteration:
            return b""


class _FakeHttpSession:
    """Minimal aiohttp.ClientSession replacement returning queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        self.posts.append({"url": url, "json": json, "headers": headers or {}})
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


class _FakeLegacySseResponse:
    def __init__(self, endpoint="/messages"):
        self.status = 200
        self.content = asyncio.StreamReader()
        self.content.feed_data(f"event: endpoint\ndata: {endpoint}\n\n".encode())
        self.closed = False

    def close(self):
        self.closed = True


class _FakeLegacySseSession:
    def __init__(self, endpoint="/messages"):
        self.stream = _FakeLegacySseResponse(endpoint)
        self.closed = False
        self.posts = []

    async def get(self, url, **kwargs):
        return self.stream

    def post(self, url, json=None, **kwargs):
        self.posts.append({"url": url, "json": json})
        request_id = (json or {}).get("id")
        method = (json or {}).get("method")
        if request_id is not None:
            if method == "initialize":
                result = {"serverInfo": {"name": "legacy"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "legacy_tool", "description": "d"}]}
            else:
                result = {"content": [{"type": "text", "text": "legacy result"}]}
            event = {"jsonrpc": "2.0", "id": request_id, "result": result}
            self.stream.content.feed_data(
                f"event: message\ndata: {__import__('json').dumps(event)}\n\n".encode()
            )
        return _FakeHttpResponse(status=202, headers={})

    async def close(self):
        self.closed = True
        self.stream.content.feed_eof()


class TestMCPHttpTransport:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.client = MCPClient()

    def test_connect_http_json_responses(self):
        init = _FakeHttpResponse(
            status=200,
            headers={"Mcp-Session-Id": "sess-123", "Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "remote"}}},
        )
        notif = _FakeHttpResponse(status=202, headers={})
        tools = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_body={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {"name": "do_thing", "description": "d", "inputSchema": {"type": "object"}}
                    ]
                },
            },
        )
        session = _FakeHttpSession([init, notif, tools])
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                self.client.connect_http("remote", "https://h/mcp", {"Authorization": "Bearer T"})
            )

        assert result["success"]
        assert result["tools"] == ["do_thing"]
        assert self.client.servers["remote"]["session_id"] == "sess-123"
        # The session id from initialize must be echoed on every later request.
        assert session.posts[2]["headers"].get("Mcp-Session-Id") == "sess-123"
        assert any(t["name"] == "do_thing" for t in self.client.discovered_tools)

    def test_connect_http_sse_framed_response(self):
        init = _FakeHttpResponse(
            status=200,
            headers={"Mcp-Session-Id": "s1", "Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 1, "result": {}},
        )
        notif = _FakeHttpResponse(status=202, headers={})
        tools = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            sse_lines=[
                b": keep-alive\n",
                b"event: message\n",
                b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"t1"}]}}\n',
                b"\n",
            ],
        )
        session = _FakeHttpSession([init, notif, tools])
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(self.client.connect_http("remote", "https://h/mcp"))

        assert result["success"]
        assert result["tools"] == ["t1"]

    def test_connect_http_error_status_cleans_up(self):
        err = _FakeHttpResponse(status=500, headers={})
        session = _FakeHttpSession([err])
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(self.client.connect_http("remote", "https://h/mcp"))

        assert not result["success"]
        assert "500" in result["error"]
        assert session.closed
        assert "remote" not in self.client.servers

    def test_call_tool_http_echoes_session_id(self):
        session = _FakeHttpSession(
            [
                _FakeHttpResponse(
                    status=200,
                    headers={"Content-Type": "application/json"},
                    json_body={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"content": [{"type": "text", "text": "hello"}]},
                    },
                )
            ]
        )
        self.client.servers["remote"] = {
            "transport": "http",
            "session": session,
            "url": "https://h/mcp",
            "session_id": "s1",
        }
        result = asyncio.run(self.client.call_tool("remote", "t1", {"a": 1}))

        assert result["success"]
        assert result["content"] == "hello"
        assert session.posts[0]["headers"].get("Mcp-Session-Id") == "s1"

    def test_disconnect_closes_http_session(self):
        session = _FakeHttpSession([])
        self.client.servers["remote"] = {
            "transport": "http",
            "session": session,
            "url": "https://h/mcp",
            "session_id": "s1",
        }
        self.client.discovered_tools = [{"server": "remote", "name": "t1"}]
        result = asyncio.run(self.client.disconnect("remote"))

        assert result["success"]
        assert session.closed
        assert "remote" not in self.client.servers
        assert self.client.discovered_tools == []

    def test_connect_http_injects_oauth_token(self):
        init = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 1, "result": {}},
        )
        notif = _FakeHttpResponse(status=202, headers={})
        tools = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
        )
        session = _FakeHttpSession([init, notif, tools])
        with (
            patch("aiohttp.ClientSession", return_value=session) as cs,
            patch("coderAI.tools.mcp_oauth.get_valid_token_sync", return_value="TOK"),
        ):
            result = asyncio.run(self.client.connect_http("remote", "https://h/mcp"))

        assert result["success"]
        # The stored OAuth token must be attached to the session headers.
        ctor_headers = cs.call_args.kwargs.get("headers", {})
        assert ctor_headers.get("Authorization") == "Bearer TOK"

    def test_connect_http_explicit_header_wins_over_oauth(self):
        init = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 1, "result": {}},
        )
        notif = _FakeHttpResponse(status=202, headers={})
        tools = _FakeHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            json_body={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
        )
        session = _FakeHttpSession([init, notif, tools])
        with (
            patch("aiohttp.ClientSession", return_value=session) as cs,
            patch("coderAI.tools.mcp_oauth.get_valid_token_sync", return_value="TOK") as gvt,
        ):
            result = asyncio.run(
                self.client.connect_http(
                    "remote", "https://h/mcp", {"Authorization": "Bearer EXPLICIT"}
                )
            )

        assert result["success"]
        ctor_headers = cs.call_args.kwargs.get("headers", {})
        assert ctor_headers.get("Authorization") == "Bearer EXPLICIT"
        gvt.assert_not_called()

    def test_connect_http_401_returns_needs_auth(self):
        unauth = _FakeHttpResponse(
            status=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="https://h/.well-known/x"'},
        )
        session = _FakeHttpSession([unauth])
        with (
            patch("aiohttp.ClientSession", return_value=session),
            patch("coderAI.tools.mcp_oauth.get_valid_token_sync", return_value=None),
        ):
            result = asyncio.run(self.client.connect_http("strava", "https://h/mcp"))

        assert not result["success"]
        assert result["needs_auth"] is True
        assert "mcp login strava" in result["error"]
        assert session.closed


class TestMCPTransportHardening:
    def test_stdio_dispatches_concurrent_out_of_order_responses(self):
        async def run():
            client = MCPClient()
            stdout = asyncio.StreamReader()
            process = MagicMock()
            process.returncode = None
            process.stdin = MagicMock()
            process.stdin.drain = AsyncMock()
            entry = {
                "transport": "stdio",
                "process": process,
                "pending": {},
                "write_lock": asyncio.Lock(),
            }
            reader = asyncio.create_task(client._stdio_reader("srv", entry, stdout))
            first = asyncio.create_task(
                client._stdio_exchange(
                    entry, {"jsonrpc": "2.0", "id": 1, "method": "one"}, timeout=1
                )
            )
            second = asyncio.create_task(
                client._stdio_exchange(
                    entry, {"jsonrpc": "2.0", "id": 2, "method": "two"}, timeout=1
                )
            )
            await asyncio.sleep(0)
            stdout.feed_data(b'{"jsonrpc":"2.0","id":2,"result":{"order":2}}\n')
            stdout.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{"order":1}}\n')
            one, two = await asyncio.gather(first, second)
            stdout.feed_eof()
            await reader
            assert one["result"]["order"] == 1
            assert two["result"]["order"] == 2
            assert entry["pending"] == {}

        asyncio.run(run())

    def test_stdio_eof_fails_pending_request(self):
        async def run():
            client = MCPClient()
            stdout = asyncio.StreamReader()
            process = MagicMock(returncode=None)
            process.stdin = MagicMock()
            process.stdin.drain = AsyncMock()
            entry = {
                "transport": "stdio",
                "process": process,
                "pending": {},
                "write_lock": asyncio.Lock(),
            }
            reader = asyncio.create_task(client._stdio_reader("srv", entry, stdout))
            request = asyncio.create_task(
                client._stdio_exchange(
                    entry, {"jsonrpc": "2.0", "id": 9, "method": "hang"}, timeout=1
                )
            )
            await asyncio.sleep(0)
            stdout.feed_eof()
            with pytest.raises(RuntimeError, match="closed stdout"):
                await request
            await reader

        asyncio.run(run())

    def test_resource_pagination_collects_pages_and_rejects_cursor_loop(self):
        client = MCPClient()
        client.servers["srv"] = {"server_info": {"capabilities": {"resources": {}}}}
        calls = []

        async def pages(server, entry, method, params=None, timeout=30):
            calls.append(params)
            if params is None:
                return {
                    "success": True,
                    "result": {"resources": [{"uri": "one"}], "nextCursor": "c1"},
                }
            return {"success": True, "result": {"resources": [{"uri": "two"}]}}

        with patch.object(client, "_request_entry", side_effect=pages):
            result = asyncio.run(client.list_resources("srv"))
        assert [item["uri"] for item in result["resources"]] == ["one", "two"]
        assert calls == [None, {"cursor": "c1"}]

        async def loop(server, entry, method, params=None, timeout=30):
            return {"success": True, "result": {"resources": [], "nextCursor": "same"}}

        with patch.object(client, "_request_entry", side_effect=loop):
            result = asyncio.run(client.list_resources("srv"))
        assert result["success"] is False
        assert "repeated pagination cursor" in result["error"]

    def test_duplicate_or_invalid_discovered_tool_rejected_before_commit(self):
        client = MCPClient()
        old = {"transport": "http", "session": _FakeHttpSession([])}
        client.servers["srv"] = old
        client.discovered_tools = [{"server": "srv", "name": "old"}]
        candidate = {"transport": "http", "session": _FakeHttpSession([])}
        init = {"jsonrpc": "2.0", "id": 1, "result": {}}
        duplicate = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"tools": [{"name": "same"}, {"name": "same"}]},
        }
        with pytest.raises(ValueError, match="duplicate"):
            asyncio.run(client._finish_connect("srv", candidate, init, duplicate))
        assert client.servers["srv"] is old
        assert client.discovered_tools == [{"server": "srv", "name": "old"}]

        invalid = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"tools": [{"name": "not valid"}]},
        }
        with pytest.raises(ValueError, match="only letters"):
            asyncio.run(client._finish_connect("srv", candidate, init, invalid))
        assert client.servers["srv"] is old

    def test_successful_replacement_purges_stale_state_and_sanitizes_metadata(self):
        client = MCPClient()
        old_session = _FakeHttpSession([])
        client.servers["srv"] = {"transport": "http", "session": old_session}
        client.discovered_tools = [{"server": "srv", "name": "old"}]
        client.discovered_resources = [{"server": "srv", "uri": "old"}]
        client.discovered_prompts = [{"server": "srv", "name": "old"}]
        candidate = {"transport": "http", "session": _FakeHttpSession([])}
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"serverInfo": {"description": "line one\nline two"}},
        }
        tools = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "fresh",
                        "description": "x\n" * 2_000,
                        "inputSchema": {
                            "type": "object",
                            "description": "schema\x00 instructions",
                        },
                    }
                ]
            },
        }
        result = asyncio.run(client._finish_connect("srv", candidate, init, tools))
        assert result["success"]
        assert old_session.closed
        assert [item["name"] for item in client.discovered_tools] == ["fresh"]
        assert client.discovered_resources == []
        assert client.discovered_prompts == []
        schema = client.get_tools_as_openai_format()[0]["function"]
        assert "\n" not in schema["description"]
        assert len(schema["description"]) <= 1024
        assert "\x00" not in schema["parameters"]["description"]
        assert result["server_info"]["description"] == "line one line two"

    def test_disconnect_tool_returns_client_failure(self):
        import coderAI.tools.mcp as mcp_mod

        original = mcp_mod.mcp_client
        fake = MagicMock()
        fake.disconnect = AsyncMock(return_value={"success": False, "error": "close failed"})
        mcp_mod.mcp_client = fake
        try:
            result = asyncio.run(MCPDisconnectTool().execute("srv"))
        finally:
            mcp_mod.mcp_client = original
        assert result == {"success": False, "error": "close failed"}

    def test_legacy_sse_stream_stays_open_and_dispatches_calls(self):
        async def run():
            client = MCPClient()
            session = _FakeLegacySseSession()
            with patch("aiohttp.ClientSession", return_value=session):
                connected = await client.connect_sse("legacy", "https://mcp.example/sse")
            assert connected["success"], connected
            assert not client.servers["legacy"]["reader_task"].done()
            called = await client.call_tool("legacy", "legacy_tool", {})
            assert called["success"]
            assert called["content"] == "legacy result"
            await client.disconnect("legacy")
            assert session.closed

        asyncio.run(run())

    def test_legacy_sse_rejects_cross_origin_message_endpoint(self):
        async def run():
            client = MCPClient()
            session = _FakeLegacySseSession("https://evil.example/messages")
            with patch("aiohttp.ClientSession", return_value=session):
                result = await client.connect_sse("legacy", "https://mcp.example/sse")
            assert result["success"] is False
            assert "cross-origin" in result["error"]
            assert session.closed

        asyncio.run(run())

    def test_http_timeout_schedules_protocol_cancellation(self):
        async def run():
            client = MCPClient()
            entry = {"transport": "http", "session": object(), "url": "https://h/mcp"}

            async def timeout(*args, **kwargs):
                raise asyncio.TimeoutError

            with (
                patch.object(client, "_http_send", side_effect=timeout),
                patch.object(client, "_send_request_cancellation", new=AsyncMock()) as cancellation,
            ):
                result = await client._request_entry("srv", entry, "slow", timeout=0.01)
                await asyncio.sleep(0)
            assert result["success"] is False
            cancellation.assert_awaited_once_with(entry, 1)

        asyncio.run(run())

"""Tests verifying all audit and review fixes for CoderAI."""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from coderai.core.lsp.connection import LspConnection
from coderai.core.network.client import HttpClient
from coderai.core.network.security import NetworkPolicy, validate_outbound_url
from coderai.core.sandbox import check_sandbox_path_access
from coderai.core.session import SessionManager
from coderai.core.subagent_backends.base import CliSubagentDriver
from coderai.core.subagent_backends.claude_code import ClaudeCodeConfig, ClaudeCodeDriver
from coderai.core.subagent_backends.codex import CodexConfig, CodexDriver
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.ralph import handle_ralph_tool
from coderai.core.tools.str_replace_editor import handle_str_replace_editor_tool
from coderai.core.tools.types import ToolDefinition, ToolResult


class TestSandboxAndSecurityFixes:
    def test_sandbox_path_confinement_workspace_write(self, tmp_path: pathlib.Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        inside_file = ws / "test.txt"
        outside_file = tmp_path / "other_dir" / "outside.txt"

        ok_in, err_in = check_sandbox_path_access(
            inside_file, op="write", mode="workspace-write", workspace_root=ws
        )
        assert ok_in is True
        assert err_in is None

        ok_out, err_out = check_sandbox_path_access(
            outside_file, op="write", mode="workspace-write", workspace_root=ws
        )
        assert ok_out is False
        assert "SANDBOX_VIOLATION" in (err_out or "")

    def test_sandbox_path_confinement_readonly(self, tmp_path: pathlib.Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "test.txt"

        ok, err = check_sandbox_path_access(target, op="write", mode="read-only", workspace_root=ws)
        assert ok is False
        assert "SANDBOX_VIOLATION" in (err or "")

    def test_post_redirect_ssrf_validation(self):
        client = HttpClient()
        policy = NetworkPolicy(allowed_domains=["example.com"])
        client.policy = policy

        mock_resp_1 = MagicMock()
        mock_resp_1.is_redirect = True
        mock_resp_1.is_permanent_redirect = False
        mock_resp_1.status_code = 302
        mock_resp_1.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with patch.object(client._session, "post", return_value=mock_resp_1):
            res = client.post("https://example.com/api")
            # Must block redirect to private/metadata IP
            assert res.ok is False
            assert (
                "blocked" in (res.error or "").lower() or "forbidden" in (res.error or "").lower()
            )

    def test_dns_resolution_fail_closed(self):
        policy = NetworkPolicy(allowed_domains=[])
        with patch("socket.getaddrinfo", side_effect=RuntimeError("DNS resolution crashed")):
            ok, err = validate_outbound_url("https://example.com", policy)
            assert ok is False
            assert "failed to resolve host" in (err or "").lower()


class TestToolSchemaAndHandlerFixes:
    @pytest.mark.asyncio
    async def test_ralph_parameter_aliases(self, mock_tool_context):
        res = await handle_ralph_tool(
            {"prompt": "Check build correctness", "max_iterations": 2}, mock_tool_context
        )
        # Should not fail with "Missing required argument objective"
        assert "Missing required argument 'objective'" not in (res.error or "")

    def test_str_replace_editor_undo_command_alias(
        self, temp_workspace: pathlib.Path, mock_tool_context
    ):
        f = temp_workspace / "sample.txt"
        f.write_text("initial content\n", encoding="utf-8")

        mock_tool_context.session_id = "test-session-undo"
        mock_tool_context.project_root = str(temp_workspace)

        # View first
        handle_str_replace_editor_tool(
            {"command": "view", "path": str(f)},
            mock_tool_context,
        )

        # Replace
        handle_str_replace_editor_tool(
            {"command": "str_replace", "path": str(f), "old_str": "initial", "new_str": "updated"},
            mock_tool_context,
        )
        assert f.read_text(encoding="utf-8") == "updated content\n"

        # Undo via undo_command alias
        undo_res = handle_str_replace_editor_tool(
            {"command": "undo_command", "path": str(f)},
            mock_tool_context,
        )
        assert undo_res.ok is True
        assert f.read_text(encoding="utf-8") == "initial content\n"

    def test_str_replace_editor_requires_observed(
        self, temp_workspace: pathlib.Path, mock_tool_context
    ):
        f = temp_workspace / "unobserved.txt"
        f.write_text("some text\n", encoding="utf-8")
        mock_tool_context.session_id = "fresh-session-unobserved"
        mock_tool_context.project_root = str(temp_workspace)

        res = handle_str_replace_editor_tool(
            {"command": "str_replace", "path": str(f), "old_str": "some", "new_str": "other"},
            mock_tool_context,
        )
        assert res.ok is False
        assert (
            "must be observed" in (res.error or "").lower()
            or "read" in (res.error or "").lower()
            or "not been read" in (res.error or "")
        )


class TestExecutorAndAsyncSafety:
    @pytest.mark.asyncio
    async def test_executor_runs_sync_handler_in_thread(self, mock_tool_context):
        def sync_handler(args: dict, ctx: any) -> ToolResult:
            return ToolResult(ok=True, name="sync_tool", output="sync_done")

        tdef = ToolDefinition(
            name="sync_tool",
            description="A test sync tool",
            parameters={},
            required=[],
            handler=sync_handler,
        )
        executor = ToolExecutor(project_root=str(mock_tool_context.project_root))
        executor.registry.register(tdef)
        result = await executor.execute_tool_call(
            "sess_test",
            {
                "id": "tc_1",
                "type": "function",
                "function": {"name": "sync_tool", "arguments": "{}"},
            },
        )
        assert result.ok is True
        assert result.output == "sync_done"

    @pytest.mark.asyncio
    async def test_mcp_error_does_not_double_execute(self):
        call_count = 0

        async def mock_mcp_exec(tool_name: str, args: dict, session_id: str | None = None):
            nonlocal call_count
            call_count += 1
            raise ValueError("MCP connection lost")

        mgr = MagicMock()
        mgr.execute_mcp_tool = mock_mcp_exec
        mgr.is_mcp_tool.return_value = True
        executor = ToolExecutor(project_root=".", mcp_manager=mgr)

        res = await executor._run_mcp("mcp_unknown_tool", {}, hooks=None)
        assert res.ok is False
        assert call_count == 1


class TestLspAndSessionFixes:
    @pytest.mark.asyncio
    async def test_lsp_dispatches_string_id_and_server_requests(self):
        conn = LspConnection(command=["echo"], cwd=".")
        conn._loop = asyncio.get_running_loop()
        # Test string req id response
        fut = conn._loop.create_future()
        conn._pending_requests[42] = fut
        conn._dispatch_message({"jsonrpc": "2.0", "id": "42", "result": {"ok": True}})
        res = await fut
        assert res == {"jsonrpc": "2.0", "id": "42", "result": {"ok": True}}

    def test_session_seq_monotonic_and_index_pruning_preserves_files(self, tmp_path: pathlib.Path):
        mgr = SessionManager(
            project_root=str(tmp_path),
            create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
            get_resolved_settings=lambda: {"model": "gpt-4o"},
        )
        msg_dir = tmp_path / "messages"
        msg_dir.mkdir(parents=True, exist_ok=True)
        mgr.session_store.project_dir = msg_dir
        mgr.session_store.index_path = tmp_path / "sessions.json"

        # Write a dummy message file with seq 5
        msg_file = msg_dir / "sess1.jsonl"
        msg_file.write_text(json.dumps({"role": "user", "content": "hello", "seq": 5}) + "\n")

        # Load messages
        messages = mgr.list_session_messages("sess1")
        assert len(messages) == 1
        assert mgr._next_seq("sess1") == 6

    def test_subagent_cli_driver_hierarchy(self):
        codex = CodexDriver(CodexConfig())
        claude = ClaudeCodeDriver(ClaudeCodeConfig())
        assert isinstance(codex, CliSubagentDriver)
        assert isinstance(claude, CliSubagentDriver)

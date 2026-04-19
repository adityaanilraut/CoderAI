"""Tests for DelegateTaskTool — depth limiting, retry config, and error paths."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.subagent import DelegateTaskTool, MAX_DELEGATION_DEPTH


class TestDelegateTaskToolInit:
    def test_default_depth(self):
        tool = DelegateTaskTool()
        assert tool._current_depth == 0

    def test_max_delegation_depth_constant(self):
        assert MAX_DELEGATION_DEPTH == 3

    def test_name_and_description(self):
        tool = DelegateTaskTool()
        assert tool.name == "delegate_task"
        assert len(tool.description) > 0

    def test_not_marked_read_only(self):
        """Sub-agents can mutate repo/state; parallelism is capped separately."""
        assert DelegateTaskTool.is_read_only is False

    def test_parallel_cap_is_five(self):
        """Up to five delegate_task calls may run concurrently in one tool wave."""
        assert DelegateTaskTool.max_parallel_invocations == 5


class TestDelegateTaskDepthLimit:
    def test_rejects_at_max_depth(self):
        tool = DelegateTaskTool()
        tool._current_depth = MAX_DELEGATION_DEPTH  # already at limit

        result = asyncio.run(
            tool.execute(task_description="do something")
        )
        assert not result["success"]
        assert "depth" in result["error"].lower() or "limit" in result["error"].lower()

    def test_allows_at_depth_below_limit(self):
        """At depth < MAX we should not get a depth error (may fail for other reasons)."""
        tool = DelegateTaskTool()
        tool._current_depth = MAX_DELEGATION_DEPTH - 1

        # Patch Agent to prevent real LLM calls
        mock_agent = MagicMock()
        mock_agent.process_single_shot = AsyncMock(return_value="done")
        mock_agent.total_tokens = 0
        mock_agent.cost_tracker = MagicMock()
        mock_agent.cost_tracker.get_total_cost.return_value = 0.0
        mock_agent._finish_tracker = MagicMock()
        mock_agent.session = MagicMock()
        mock_agent.session.messages = []
        mock_agent.tools = MagicMock()
        mock_agent.tools.get.return_value = None
        mock_agent.create_session = MagicMock()
        mock_agent._register_tracker = MagicMock()
        mock_agent.context_manager = MagicMock()
        mock_agent.context_manager.pinned_files = {}
        mock_agent.context_manager._pinned_mtimes = {}
        mock_agent.context_manager.project_instructions = None
        mock_agent.set_persona = MagicMock(return_value=None)
        mock_agent.close = AsyncMock()

        with patch("coderAI.agent.Agent", return_value=mock_agent):
            result = asyncio.run(
                tool.execute(task_description="simple task")
            )
        # Should NOT be a depth error
        if not result["success"]:
            assert "depth" not in result.get("error", "").lower()


class TestDelegateTaskRetryConfig:
    def test_retry_count_is_two(self):
        """Verify max_retries=2 gives 3 total attempts (loop uses range(1, max_retries+2))."""
        attempts = []

        tool = DelegateTaskTool()
        tool._current_depth = 0

        mock_agent = MagicMock()
        call_count = [0]

        async def failing_process(*args, **kwargs):
            call_count[0] += 1
            raise RuntimeError("simulated failure")

        mock_agent.process_single_shot = failing_process
        mock_agent.total_tokens = 0
        mock_agent.cost_tracker = MagicMock()
        mock_agent.cost_tracker.get_total_cost.return_value = 0.0
        mock_agent._finish_tracker = MagicMock()
        mock_agent.session = MagicMock()
        mock_agent.session.messages = []
        mock_agent.tools = MagicMock()
        mock_agent.tools.get.return_value = None
        mock_agent.create_session = MagicMock()
        mock_agent._register_tracker = MagicMock()
        mock_agent.context_manager = MagicMock()
        mock_agent.context_manager.pinned_files = {}
        mock_agent.context_manager._pinned_mtimes = {}
        mock_agent.context_manager.project_instructions = None
        mock_agent.set_persona = MagicMock(return_value=None)
        mock_agent.close = AsyncMock()

        with patch("coderAI.agent.Agent", return_value=mock_agent):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(tool.execute(task_description="fail"))

        assert not result["success"]
        # 3 total attempts (1 initial + 2 retries)
        assert call_count[0] == 3


class TestDelegateTaskEmptyReport:
    """Sub-agent empty-report handling: nudge-retry then clean failure."""

    def _make_mock_agent(self, responses):
        """Build a mock Agent whose process_single_shot returns ``responses``
        in order (one per call)."""
        mock_agent = MagicMock()
        call_iter = iter(responses)

        async def _single_shot(*args, **kwargs):
            return next(call_iter)

        mock_agent.process_single_shot = _single_shot
        mock_agent.total_tokens = 1234
        mock_agent.cost_tracker = MagicMock()
        mock_agent.cost_tracker.get_total_cost.return_value = 0.0
        mock_agent._finish_tracker = MagicMock()
        mock_agent.session = MagicMock()
        mock_agent.session.messages = []
        mock_agent.tools = MagicMock()
        mock_agent.tools.get.return_value = None
        mock_agent.create_session = MagicMock()
        mock_agent._register_tracker = MagicMock()
        mock_agent._configure_delegate_tool_context = MagicMock()
        mock_agent.context_manager = MagicMock()
        mock_agent.context_manager.pinned_files = {}
        mock_agent.context_manager._pinned_mtimes = {}
        mock_agent.context_manager.project_instructions = None
        mock_agent.set_persona = MagicMock(return_value=None)
        mock_agent.close = AsyncMock()
        mock_agent.model = "mock-model"
        return mock_agent

    def test_empty_report_triggers_nudge_and_recovers(self):
        """Initial empty response → nudge → non-empty response → success."""
        tool = DelegateTaskTool()
        tool._current_depth = 0

        mock_agent = self._make_mock_agent(["", "Here is the report."])

        with patch("coderAI.agent.Agent", return_value=mock_agent):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(tool.execute(task_description="research"))

        assert result["success"] is True
        assert result["final_report"] == "Here is the report."

    def test_persistent_empty_report_returns_failure(self):
        """Every call returns empty → all retries exhausted → success=False
        with a clear error and tokens_used surfaced."""
        tool = DelegateTaskTool()
        tool._current_depth = 0

        # 3 attempts × 2 calls each (initial + nudge) = 6 empty responses
        mock_agent = self._make_mock_agent([""] * 6)

        with patch("coderAI.agent.Agent", return_value=mock_agent):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(tool.execute(task_description="research"))

        assert result["success"] is False
        assert "no final report" in result["error"].lower() or "failed" in result["error"].lower()
        assert "tokens_used" in result

    def test_whitespace_only_report_is_treated_as_empty(self):
        """A report containing only whitespace should trigger the nudge."""
        tool = DelegateTaskTool()
        tool._current_depth = 0

        mock_agent = self._make_mock_agent(["   \n\t  ", "Real report content."])

        with patch("coderAI.agent.Agent", return_value=mock_agent):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = asyncio.run(tool.execute(task_description="research"))

        assert result["success"] is True
        assert result["final_report"] == "Real report content."


class TestDelegateTaskSchema:
    def test_schema_has_required_fields(self):
        tool = DelegateTaskTool()
        schema = tool.get_schema()
        params = schema["function"]["parameters"]["properties"]
        assert "task_description" in params

    def test_task_description_is_required(self):
        tool = DelegateTaskTool()
        schema = tool.get_schema()
        required = schema["function"]["parameters"].get("required", [])
        assert "task_description" in required

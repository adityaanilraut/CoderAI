"""Tests for DelegateTaskTool — depth limiting, retry config, and error paths."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.tools.subagent import (
    DelegateTaskTool,
    MAX_DELEGATION_DEPTH,
    SubagentContext,
)


class TestDelegateTaskToolInit:
    def test_default_depth(self):
        tool = DelegateTaskTool()
        # Depth lives on the structured context now; the legacy
        # ``_current_depth`` property forwards there for backwards compat.
        assert tool.context.delegation_depth == 0
        assert tool._current_depth == 0

    def test_depth_setter_updates_context(self):
        tool = DelegateTaskTool()
        tool._current_depth = 2
        assert tool.context.delegation_depth == 2

    def test_max_delegation_depth_constant(self):
        assert MAX_DELEGATION_DEPTH == 3

    def test_name_and_description(self):
        tool = DelegateTaskTool()
        assert tool.name == "delegate_task"
        assert len(tool.description) > 0

    def test_not_marked_read_only(self):
        """Sub-agents can mutate repo/state; parallelism is capped separately."""
        assert DelegateTaskTool.is_read_only is False

    def test_parallel_cap_serialises_workspace_access(self):
        """Sub-agents run one at a time to serialise workspace access and prevent deadlocks."""
        assert DelegateTaskTool.max_parallel_invocations == 1


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


class TestDelegateTaskParentState:
    def test_subagent_inherits_auto_approve_and_ipc_server(self):
        tool = DelegateTaskTool()
        ipc_server = object()
        tool.context = SubagentContext(
            parent_auto_approve=True,
            parent_ipc_server=ipc_server,
        )

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
        mock_agent._configure_delegate_tool_context = MagicMock()
        mock_agent.close = AsyncMock()

        with patch("coderAI.agent.Agent", return_value=mock_agent) as agent_cls:
            result = asyncio.run(tool.execute(task_description="simple task"))

        assert result["success"] is True
        agent_cls.assert_called_once()
        assert agent_cls.call_args.kwargs["auto_approve"] is True
        assert mock_agent.ipc_server is ipc_server


class TestDelegateTaskDepthPropagation:
    """H-2 / M-10: depth must be passed via the Agent constructor and
    threaded through SubagentContext, not patched onto a child after the
    fact."""

    def test_child_agent_constructed_with_incremented_depth(self):
        tool = DelegateTaskTool()
        tool.context = SubagentContext(delegation_depth=1)

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
        mock_agent._configure_delegate_tool_context = MagicMock()
        mock_agent.close = AsyncMock()

        with patch("coderAI.agent.Agent", return_value=mock_agent) as agent_cls:
            asyncio.run(tool.execute(task_description="task"))

        assert agent_cls.call_args.kwargs["delegation_depth"] == 2

    def test_run_delegation_rejects_at_construction_when_above_cap(self):
        """If ``_run_delegation`` is invoked at a depth that would put the
        child past ``MAX_DELEGATION_DEPTH``, it must refuse rather than
        construct the Agent. ``execute`` blocks the same case earlier; this
        guards the direct-call entry point."""
        tool = DelegateTaskTool()
        tool.context = SubagentContext(delegation_depth=MAX_DELEGATION_DEPTH)

        with patch("coderAI.agent.Agent") as agent_cls:
            result = asyncio.run(
                tool._run_delegation(
                    task_description="x",
                    agent_role=None,
                    context_hints=None,
                    model=None,
                    inherit_project_context=False,
                )
            )

        assert result["success"] is False
        assert "depth" in result["error"].lower()
        agent_cls.assert_not_called()

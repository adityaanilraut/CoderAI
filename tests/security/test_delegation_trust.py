"""Phase 5 — delegation trust propagation: a child agent is always a subset of
its parent.

Threat model: the model (possibly steered by untrusted repo/web/MCP content)
drives ``delegate_task``. A spawned child must never gain a capability, escape a
confirmation policy, escape a cost budget, or assume a launch role the parent
does not have. These are the regression tests for that invariant:

* 5.1 capability ⊆ parent — disabling web tools is transitive, and a child's
      tool set is intersected with the parent's registered tools.
* 5.2 confirmation policy propagates — the parent's (e.g. headless
      deny-on-mutate) override reaches the child and shares its audit list.
* 5.3 persona mode/hidden is enforced — a subagent/hidden persona can't be
      primary and a primary persona can't be a sub-agent.
* 5.4 one delegation-tree-wide cost budget — a child inherits the parent's cap
      and the shared cost tracker trips for the whole tree.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.core.tool_executor import ToolExecutor
from coderAI.core.agents import AgentPersona, persona_allowed_in_context
from coderAI.system.history import Session
from coderAI.tools.subagent import (
    BROWSER_NATIVE_CAPABILITIES,
    DESKTOP_NATIVE_CAPABILITIES,
    READ_ONLY_NATIVE_CAPABILITIES,
    WORKSPACE_NATIVE_CAPABILITIES,
    DelegateTaskTool,
    SubagentContext,
)

# ═══════════════════════════════════════════════════════════════════════════
# Harnesses
# ═══════════════════════════════════════════════════════════════════════════


def _mock_child(tool_names, *, read_only_map=None):
    """A MagicMock sub-agent whose ``.tools.tools`` is a **real** dict.

    Using a real dict lets the read-only strip and the Phase-5.1 capability
    intersection in ``_build_sub_agent`` actually mutate it, so a test can
    assert on the surviving tool set. Everything else is stubbed enough for
    ``_run_delegation`` to reach a successful report.
    """
    read_only_map = read_only_map or {}
    tools = {}
    for name in tool_names:
        t = MagicMock()
        t.is_read_only = read_only_map.get(name, True)
        tools[name] = t

    child = MagicMock()
    child.tools.tools = tools
    child.process_single_shot = AsyncMock(return_value="report")
    child.total_tokens = 0
    child.total_prompt_tokens = 0
    child.total_completion_tokens = 0
    child.provider = MagicMock()
    child.provider.actual_model = "claude"
    child.cost_tracker = MagicMock()
    child.cost_tracker.get_total_cost.return_value = 0.0
    child.tracker_info = None
    child.session = Session(session_id="session_1000_deadbeef", model="claude")
    child.session.add_message("system", "base-system")
    child.create_session = MagicMock()
    child._register_tracker = MagicMock()
    child._configure_delegate_tool_context = MagicMock()
    child.context_controller.pinned_files = {}
    child.context_controller._pinned_mtimes = {}
    child.context_controller.project_instructions = None
    child.set_persona = MagicMock(return_value=None)
    child.close = AsyncMock()
    # Real objects the Phase-5 code assigns onto, so a test can read them back.
    child.config = SimpleNamespace(budget_limit=0.0)
    child.confirmation_override = None
    return child


def _run(tool: DelegateTaskTool, child, **execute_kwargs):
    """Run one delegation with ``child`` as the constructed sub-agent."""
    with patch("coderAI.core.agent.Agent", return_value=child):
        return asyncio.run(tool.execute(task_description="do a thing", **execute_kwargs))


def _build_real_agent(*, is_subagent: bool = False, **config_kwargs):
    """Construct a real ``Agent`` with a mocked provider and injected config.

    Skills/network are disabled so construction is fast and offline. The
    provider patch is only needed during ``__init__``; the returned agent's
    config and tool registry are fully built.
    """
    from coderAI.core.agent import Agent
    from coderAI.system.config import Config

    cfg_kwargs = dict(auto_detect_skills=False)
    cfg_kwargs.update(config_kwargs)
    cfg = Config(**cfg_kwargs)
    with patch("coderAI.core.agent.config_manager") as cm:
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        with patch.object(Agent, "_create_provider", return_value=MagicMock()):
            return Agent(model="gpt-5.4-mini", streaming=False, is_subagent=is_subagent)


def _persona(mode: str = "all", hidden: bool = False) -> AgentPersona:
    return AgentPersona(
        name="p",
        description="d",
        tools=[],
        model="gpt-5.4-mini",
        instructions="i",
        mode=mode,
        hidden=hidden,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5.1 — child capability ⊆ parent
# ═══════════════════════════════════════════════════════════════════════════


class TestCapabilitySubsetOfParent:
    def test_subagent_drops_web_tools_when_disabled_transitively(self):
        """web_tools_in_main=False must reach sub-agents (carve-out removed)."""
        agent = _build_real_agent(is_subagent=True, web_tools_in_main=False)
        assert "web_search" not in agent.tools.tools
        assert "read_url" not in agent.tools.tools
        assert "download_file" not in agent.tools.tools

    def test_subagent_keeps_web_tools_when_enabled(self):
        """Default posture (web tools on) is unchanged for sub-agents."""
        agent = _build_real_agent(is_subagent=True, web_tools_in_main=True)
        assert "web_search" in agent.tools.tools

    def test_configure_delegate_context_snapshots_parent_tool_ceiling(self):
        agent = _build_real_agent(web_tools_in_main=True)
        # Parent gives up a capability after construction.
        agent.tools.tools.pop("write_file", None)
        agent._configure_delegate_tool_context()

        delegate = agent.tools.get("delegate_task")
        assert delegate is not None
        ceiling = delegate.context.parent_tool_names
        assert ceiling is not None
        assert "write_file" not in ceiling
        assert "read_file" in ceiling  # sanity: the ceiling is populated

    def test_delegation_drops_tools_absent_from_parent(self):
        """A child never keeps a tool the parent lacks — even without a role."""
        tool = DelegateTaskTool()
        tool.context = SubagentContext(parent_tool_names=frozenset({"read_file", "delegate_task"}))
        child = _mock_child(
            ["read_file", "write_file", "web_search", "delegate_task"],
            read_only_map={"write_file": False, "delegate_task": False},
        )

        result = _run(tool, child)

        assert result["success"] is True
        assert set(child.tools.tools.keys()) == {"read_file", "delegate_task"}

    def test_read_only_and_intersection_compose(self):
        """read_only_task strips mutators, then the ceiling strips extras."""
        tool = DelegateTaskTool()
        tool.context = SubagentContext(
            parent_tool_names=frozenset({"read_file", "write_file", "delegate_task"})
        )
        child = _mock_child(
            ["read_file", "write_file", "web_search", "delegate_task"],
            read_only_map={
                "read_file": True,
                "write_file": False,
                "web_search": True,
                "delegate_task": False,
            },
        )

        result = _run(tool, child, read_only_task=True)

        assert result["success"] is True
        # read_only removes write_file + delegate_task (mutating); the ceiling
        # then removes web_search (not a parent capability). read_file survives.
        assert set(child.tools.tools.keys()) == {"read_file"}

    def test_missing_ceiling_leaves_tools_untouched(self):
        """A missing parent ceiling still applies the workspace domain boundary."""
        tool = DelegateTaskTool()
        tool.context = SubagentContext(parent_tool_names=None)
        child = _mock_child(["read_file", "write_file"])

        result = _run(tool, child)

        assert result["success"] is True
        assert set(child.tools.tools.keys()) == {"read_file", "write_file"}

    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("read_only", READ_ONLY_NATIVE_CAPABILITIES),
            ("browser", BROWSER_NATIVE_CAPABILITIES),
            ("desktop", DESKTOP_NATIVE_CAPABILITIES),
            ("workspace", WORKSPACE_NATIVE_CAPABILITIES),
        ],
    )
    def test_isolation_domain_applies_exact_native_capability_set(self, domain, expected):
        tool = DelegateTaskTool()
        candidates = {
            "read_file",
            "write_file",
            "browser_navigate",
            "run_applescript",
            "delegate_task",
            "mcp_list",
        }
        child = _mock_child(
            candidates,
            read_only_map={
                "read_file": True,
                "write_file": False,
                "browser_navigate": False,
                "run_applescript": False,
                "delegate_task": False,
                "mcp_list": True,
            },
        )

        result = _run(tool, child, isolation_domain=domain)

        assert result["success"] is True
        assert set(child.tools.tools) == candidates & expected
        assert child._capability_domain == domain
        assert child._allowed_native_tool_names == frozenset(candidates & expected)
        assert child._allow_dynamic_mcp is False

    def test_read_only_flag_overrides_requested_workspace_domain(self):
        tool = DelegateTaskTool()
        child = _mock_child(
            ["read_file", "write_file"],
            read_only_map={"read_file": True, "write_file": False},
        )

        _run(tool, child, read_only_task=True, isolation_domain="workspace")

        assert set(child.tools.tools) == {"read_file"}
        assert child._capability_domain == "read_only"


class TestCapabilityRuntimeEnforcement:
    @staticmethod
    def _executor() -> tuple[ToolExecutor, MagicMock]:
        read_tool = MagicMock(is_read_only=True)
        hidden_tool = MagicMock(is_read_only=False, requires_confirmation=True)
        registry = MagicMock()
        registry.get.side_effect = lambda name: {
            "read_file": read_tool,
            "write_file": hidden_tool,
        }.get(name)
        registry.execute = AsyncMock(return_value={"success": True})
        agent = SimpleNamespace(
            auto_approve=True,
            tools=registry,
            tracker_info=None,
            _allowed_native_tool_names=frozenset({"read_file"}),
            _capability_domain="read_only",
        )
        return ToolExecutor(agent), registry

    @pytest.mark.asyncio
    async def test_hidden_native_call_is_rejected_before_dispatch(self):
        executor, registry = self._executor()
        result = await executor.execute_single_tool(
            {
                "tool_id": "hidden",
                "tool_name": "write_file",
                "arguments": {"path": "x", "content": "bad"},
                "parse_error": None,
            },
            None,
            SimpleNamespace(run_hooks=AsyncMock(return_value=[])),
        )

        assert result["success"] is False
        assert result["error_code"] == ToolErrorCode.PERMISSION_DENIED
        registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hidden_mcp_call_is_rejected_before_routing(self, monkeypatch):
        import coderAI.core.tool_executor as executor_module

        executor, _registry = self._executor()
        routed = AsyncMock(return_value={"success": True})
        monkeypatch.setattr(executor_module, "call_mcp_tool_by_function_name", routed)

        result = await executor.execute_single_tool(
            {
                "tool_id": "hidden-mcp",
                "tool_name": "mcp__server__tool",
                "arguments": {},
                "parse_error": None,
            },
            None,
            SimpleNamespace(run_hooks=AsyncMock(return_value=[])),
        )

        assert result["success"] is False
        assert result["error_code"] == ToolErrorCode.PERMISSION_DENIED
        routed.assert_not_awaited()

    def test_restricted_agent_schemas_exclude_dynamic_mcp(self):
        native_schema = {
            "type": "function",
            "function": {"name": "read_file", "parameters": {}},
        }
        mcp_schema = {
            "type": "function",
            "function": {"name": "mcp__server__tool", "parameters": {}},
        }
        agent = SimpleNamespace(
            hooks_manager=None,
            provider=SimpleNamespace(supports_tools=lambda: True),
            tools=SimpleNamespace(get_schemas=lambda: [native_schema]),
            _allow_dynamic_mcp=False,
        )
        services = SimpleNamespace(
            mcp_client=SimpleNamespace(
                get_tools_as_openai_format=lambda: [mcp_schema],
                servers={},
            )
        )

        with patch("coderAI.core.agent_loop.get_services", return_value=services):
            schemas = ExecutionLoop(agent)._get_tool_schemas()

        assert schemas == [native_schema]


# ═══════════════════════════════════════════════════════════════════════════
# 5.2 — confirmation policy propagates into the child
# ═══════════════════════════════════════════════════════════════════════════


class TestConfirmationPolicyPropagation:
    def test_child_inherits_parent_confirmation_override(self):
        """The child gets the *same* callable, so denials audit to one list."""

        async def deny(_name, _args):
            return False

        tool = DelegateTaskTool()
        tool.context = SubagentContext(parent_confirmation_override=deny)
        child = _mock_child(["read_file"])

        result = _run(tool, child)

        assert result["success"] is True
        assert child.confirmation_override is deny

    def test_no_override_propagates_none(self):
        tool = DelegateTaskTool()
        tool.context = SubagentContext(parent_confirmation_override=None)
        child = _mock_child(["read_file"])

        _run(tool, child)

        assert child.confirmation_override is None

    def test_configure_delegate_context_snapshots_confirmation_override(self):
        """Mirrors the headless run path: override installed post-build, then
        the delegate context is re-snapshotted so children inherit it."""
        agent = _build_real_agent()

        async def deny(_name, _args):
            return False

        agent.confirmation_override = deny
        agent._configure_delegate_tool_context()

        delegate = agent.tools.get("delegate_task")
        assert delegate is not None
        assert delegate.context.parent_confirmation_override is deny

    def test_headless_run_refreshes_delegate_context_after_deny_guard(self):
        """`coderAI run` installs deny-on-mutate then must re-wire the delegate
        tool so the guard is transitive; assert the audit list is shared."""
        from coderAI.cli import run_cmd

        blocked: list = []
        agent = _build_real_agent()
        agent.auto_approve = False
        agent.process_message = AsyncMock(return_value={"content": "ok"})
        agent.close = AsyncMock()

        asyncio.run(run_cmd._run_agent(agent, "hi", blocked))

        delegate = agent.tools.get("delegate_task")
        override = delegate.context.parent_confirmation_override
        assert override is not None
        # Driving the shared override appends to the run's blocked_tools list.
        assert asyncio.run(override("write_file", {})) is False
        assert "write_file" in blocked


# ═══════════════════════════════════════════════════════════════════════════
# 5.3 — persona mode / hidden enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestPersonaModeEnforcement:
    def test_allowed_matrix(self):
        # Primary launch context.
        assert persona_allowed_in_context(_persona("all"), is_subagent=False)
        assert persona_allowed_in_context(_persona("primary"), is_subagent=False)
        assert not persona_allowed_in_context(_persona("subagent"), is_subagent=False)
        assert not persona_allowed_in_context(_persona("hidden"), is_subagent=False)
        assert not persona_allowed_in_context(_persona("all", hidden=True), is_subagent=False)
        # Sub-agent launch context.
        assert persona_allowed_in_context(_persona("all"), is_subagent=True)
        assert persona_allowed_in_context(_persona("subagent"), is_subagent=True)
        assert persona_allowed_in_context(_persona("hidden"), is_subagent=True)
        assert not persona_allowed_in_context(_persona("primary"), is_subagent=True)

    def test_primary_agent_refuses_hidden_persona(self):
        agent = _build_real_agent(is_subagent=False)
        assert agent.apply_persona(_persona(hidden=True)) is None
        assert agent.persona is None

    def test_primary_agent_refuses_subagent_mode_persona(self):
        agent = _build_real_agent(is_subagent=False)
        assert agent.apply_persona(_persona("subagent")) is None
        assert agent.persona is None

    def test_subagent_refuses_primary_only_persona(self):
        agent = _build_real_agent(is_subagent=True)
        assert agent.apply_persona(_persona("primary")) is None
        assert agent.persona is None

    def test_set_persona_refuses_hidden_file_as_primary(self, tmp_path):
        """Loading a subagent/hidden persona *file* as the primary agent (the
        ``--persona`` / ``/persona`` path) is refused end-to-end."""
        agents_dir = tmp_path / ".coderAI" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "sneaky.md").write_text(
            "---\nname: sneaky\nmodel: gpt-5.4-mini\nmode: subagent\nhidden: true\n---\n"
            "Do sneaky things.\n",
            encoding="utf-8",
        )
        agent = _build_real_agent(is_subagent=False, project_root=str(tmp_path))
        loaded = agent.set_persona("sneaky")
        assert loaded is None
        assert agent.persona is None


# ═══════════════════════════════════════════════════════════════════════════
# 5.4 — one delegation-tree-wide cost budget
# ═══════════════════════════════════════════════════════════════════════════


class TestDelegationTreeBudget:
    def test_child_inherits_parent_budget_cap(self):
        tool = DelegateTaskTool()
        tool.context = SubagentContext(
            parent_config=SimpleNamespace(budget_limit=5.0, subagent_timeout_seconds=600.0)
        )
        child = _mock_child(["read_file"])
        assert child.config.budget_limit == 0.0  # starts unset

        _run(tool, child)

        assert child.config.budget_limit == 5.0

    def test_child_inherits_unlimited_when_parent_unlimited(self):
        tool = DelegateTaskTool()
        tool.context = SubagentContext(
            parent_config=SimpleNamespace(budget_limit=0.0, subagent_timeout_seconds=600.0)
        )
        child = _mock_child(["read_file"])
        child.config.budget_limit = 3.0  # child would otherwise diverge

        _run(tool, child)

        # Tree budget is single-sourced from the parent (0 == unlimited).
        assert child.config.budget_limit == 0.0

"""Tests for the Agent orchestrator."""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.context.context_controller import ContextController


class TestTransientErrorDetection:
    """Tests for the transient-error classifier in error_policy."""

    def test_timeout_is_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(Exception("Request timed out")) is True

    def test_rate_limit_is_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(Exception("Rate limit exceeded (429)")) is True

    def test_server_error_is_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(Exception("502 Bad Gateway")) is True

    def test_auth_error_is_not_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(Exception("Invalid API key")) is False

    def test_generic_error_is_not_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(ValueError("bad value")) is False

    def test_connection_reset_is_transient(self):
        from coderAI.system.error_policy import is_transient_error

        assert is_transient_error(Exception("Connection reset by peer")) is True


class TestSummarizeToolResult:
    """Tests for ContextController.summarize_tool_result."""

    def _make_controller(self):
        from coderAI.system.config import Config

        cfg = Config(max_tool_output=200)
        return ContextController(cfg, MagicMock())

    def test_small_result_unchanged(self):
        ctrl = self._make_controller()
        result = {"success": True, "data": "short"}
        assert ctrl.summarize_tool_result(result) == result

    def test_large_string_truncated(self):
        ctrl = self._make_controller()
        result = {"success": True, "content": "x" * 5000}
        summarized = ctrl.summarize_tool_result(result)
        assert "truncated" in summarized["content"]
        assert len(summarized["content"]) < 5000

    def test_large_list_truncated(self):
        ctrl = self._make_controller()
        result = {"success": True, "items": list(range(100))}
        summarized = ctrl.summarize_tool_result(result)
        assert len(summarized["items"]) == 51
        assert "_note" in summarized["items"][-1]


class TestTruncateMessages:
    """Tests for ContextController.manage_context_window."""

    def _make_controller(self):
        from coderAI.system.config import Config

        cfg = Config(context_window=500)  # small window
        provider = MagicMock()
        provider.count_tokens = lambda text: len(text) // 4
        provider.chat = AsyncMock(return_value={"choices": [{"message": {"content": "summary"}}]})
        return ContextController(cfg, provider)

    def test_preserves_system_messages(self):
        ctrl = self._make_controller()
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "x" * 2000},
            {"role": "assistant", "content": "y" * 2000},
            {"role": "user", "content": "recent question"},
        ]
        result = asyncio.run(ctrl.manage_context_window(messages))
        # System message must always be present
        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert system_msgs[0]["content"] == "You are a bot."

    def test_summary_cost_uses_incremental_provider_delta(self):
        from coderAI.system.cost import CostTracker

        ctrl = self._make_controller()
        ctrl.cost_tracker = CostTracker()
        ctrl._last_summary_time = -10_000
        seen = iter(
            [
                {"total_input_tokens": 1000, "total_output_tokens": 500},
                {"total_input_tokens": 1050, "total_output_tokens": 520},
            ]
        )
        ctrl.provider.get_model_info = lambda: next(seen)
        ctrl.config.context_window = 300
        ctrl._on_summary_tokens = MagicMock()
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "first " + "x" * 1000},
            {"role": "assistant", "content": "second " + "y" * 1000},
            {"role": "user", "content": "third " + "z" * 1000},
            {"role": "assistant", "content": "fourth " + "q" * 1000},
            {"role": "user", "content": "recent"},
        ]

        asyncio.run(ctrl.manage_context_window(messages))

        ctrl._on_summary_tokens.assert_called_once_with(50, 20)

    def test_summary_prompt_includes_tool_calls_without_content(self):
        ctrl = self._make_controller()
        ctrl._last_summary_time = -10_000
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "initial task"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": '{"success": true, "output": "' + ("x" * 900) + '"}',
            },
            {"role": "user", "content": "middle " + ("y" * 900)},
            {"role": "assistant", "content": "middle response " + ("z" * 900)},
            {"role": "user", "content": "recent question"},
        ]

        asyncio.run(ctrl.manage_context_window(messages))

        prompt = ctrl.provider.chat.await_args.args[0][0]["content"]
        assert "ASSISTANT TOOL_CALLS" in prompt
        assert "read_file" in prompt
        assert "README.md" in prompt

    def test_strip_internal_markers_removes_truncation_notice(self):
        cleaned = ContextController.strip_internal_markers(
            [
                {
                    "role": "system",
                    "content": "[Note: earlier messages removed]",
                    ContextController._TRUNCATION_MARKER_KEY: True,
                }
            ]
        )
        assert ContextController._TRUNCATION_MARKER_KEY not in cleaned[0]
        assert cleaned[0]["content"].startswith("[Note:")

    def test_manage_context_window_drops_stale_truncation_notices(self):
        ctrl = self._make_controller()
        ctrl.config.context_window = 200
        stale = {
            "role": "system",
            "content": "[Prior Conversation Summary]: old",
            ContextController._TRUNCATION_MARKER_KEY: True,
        }
        messages = [
            {"role": "system", "content": "You are a bot."},
            stale,
            {"role": "user", "content": "recent"},
        ]
        ctrl.estimate_tokens = MagicMock(return_value=50)
        result = asyncio.run(ctrl.manage_context_window(messages))
        assert not any(m.get(ContextController._TRUNCATION_MARKER_KEY) for m in result)
        assert stale not in result

    def test_budget_blocks_summarization_llm_call(self):
        import pytest
        from coderAI.system.cost import CostTracker
        from coderAI.system.error_policy import BudgetExceededError

        ctrl = self._make_controller()
        ctrl.cost_tracker = CostTracker()
        ctrl.config.budget_limit = 0.01
        ctrl.cost_tracker.total_cost_usd = 0.02
        ctrl._last_summary_time = -10_000
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "first " + "x" * 1000},
            {"role": "assistant", "content": "second " + "y" * 1000},
            {"role": "user", "content": "third " + "z" * 1000},
            {"role": "assistant", "content": "fourth " + "q" * 1000},
            {"role": "user", "content": "recent"},
        ]

        with pytest.raises(BudgetExceededError):
            asyncio.run(ctrl.manage_context_window(messages))

        ctrl.provider.chat.assert_not_called()

    def test_aggressive_truncation_emits_warning(self):
        from types import SimpleNamespace

        ctrl = self._make_controller()
        ctrl.config.context_window = 120
        ctrl.estimate_tokens = MagicMock(return_value=10_000)
        messages = [{"role": "system", "content": "sys"}]
        for i in range(4):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{i}",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": f'{{"path":"f{i}.py"}}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{i}",
                        "name": "read_file",
                        "content": "x" * 400,
                    },
                ]
            )
        emitter = SimpleNamespace(emit=MagicMock())
        with patch("coderAI.context.context_controller.event_emitter", emitter):
            asyncio.run(ctrl.manage_context_window(messages))
        warning_msgs = [
            c.kwargs.get("message", c.args[1] if len(c.args) > 1 else "")
            for c in emitter.emit.call_args_list
            if c.args and c.args[0] == "agent_warning"
        ]
        assert any("Context truncation aggressive" in msg for msg in warning_msgs)


class TestCostTrackerConcurrency:
    def test_concurrent_add_cost(self):
        import pytest
        from coderAI.system.cost import CostTracker

        async def run() -> None:
            tracker = CostTracker()
            per_call = CostTracker.calculate_cost_for_tokens("gpt-5.4-mini", 1000, 100)

            async def bump() -> float:
                return await tracker.add_cost("gpt-5.4-mini", 1000, 100)

            costs = await asyncio.gather(*(bump() for _ in range(50)))
            assert tracker.get_total_cost() == pytest.approx(per_call * 50)
            assert sum(costs) == pytest.approx(per_call * 50)

        asyncio.run(run())


class TestRecoverableErrorMarker:
    """Recoverable errors must persist tagged system feedback in the session."""

    def test_handle_recoverable_error_adds_marker_to_session(self):
        from types import SimpleNamespace

        from coderAI.core.agent_loop import RECOVERABLE_ERROR_MARKER, ExecutionLoop
        from coderAI.system.history import Session

        session = Session(session_id="session_1234567890_marker01")
        context_controller = SimpleNamespace(
            inject_context=lambda messages, _cm, query=None: messages,
            manage_context_window=AsyncMock(side_effect=lambda messages: messages),
        )
        agent = SimpleNamespace(
            session=session,
            context_controller=context_controller,
            context_manager=SimpleNamespace(),
            hooks_manager=None,
        )
        loop = ExecutionLoop(agent)

        asyncio.run(loop._handle_recoverable_error(RuntimeError("disk full"), 1, "fix it"))

        system_contents = [m.content for m in session.messages if m.role == "system"]
        assert any(RECOVERABLE_ERROR_MARKER in c for c in system_contents)

    def test_project_config_is_loaded(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            base = Config(temperature=0.7)
            project = Config(temperature=0.1)
            cm.load.return_value = base
            cm.load_project_config.return_value = project

            from coderAI.core.agent import Agent

            # Patch provider creation to avoid needing a real API key
            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                Agent(model="gpt-5.4-mini", streaming=False)
                # load_project_config should have been called
                cm.load_project_config.assert_called_once_with(".")


class TestAgentsPersonas:
    """Tests for the Agents module personas."""

    def test_load_agent_persona_no_dir(self):
        from coderAI.core.agents import load_agent_persona

        with patch("pathlib.Path.exists", return_value=False):
            persona = load_agent_persona("planner")
            assert persona is None

    def test_load_agent_persona_success(self):
        from coderAI.core.agents import load_agent_persona
        from coderAI.system.config import Config

        mock_md = """---
name: Planner Agent
description: Plans tasks
tools: [manage_tasks]
model: claude-3-5-sonnet-20241022
---
You are a planner."""

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=mock_md):
                with patch("coderAI.system.config.config_manager.load", return_value=Config()):
                    persona = load_agent_persona("planner")
                    assert persona is not None
                    assert persona.name == "Planner Agent"
                    assert persona.model == "claude-3-5-sonnet-20241022"
                    assert "You are a planner." in persona.instructions

    def test_resolve_persona_name_alias(self):
        from coderAI.core.agents import resolve_persona_name

        with patch("coderAI.core.agents._find_agents_dir", return_value=Path("/tmp")):
            with patch(
                "coderAI.core.agents.get_available_personas",
                return_value=["code-reviewer", "planner"],
            ):
                assert resolve_persona_name("Code Reviewer") == "code-reviewer"

    def test_expand_persona_tools_aliases(self):
        from coderAI.core.agents import expand_persona_tools

        expanded = expand_persona_tools(["Read", "Edit", "Bash"])

        assert "read_file" in expanded
        assert "search_replace" in expanded
        assert "apply_diff" in expanded
        assert "run_command" in expanded
        assert "run_background" in expanded


class TestAgentPersonaSwitching:
    """Tests for live persona switching on an existing agent session."""

    def _make_agent(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                return Agent(model="gpt-5.4-mini", streaming=False)

    def test_set_persona_updates_live_prompt_and_tools(self):
        from coderAI.core.agents import AgentPersona

        agent = self._make_agent()
        agent.create_session()

        persona = AgentPersona(
            name="Reviewer",
            description="Reviews code",
            tools=["Read"],
            model=None,
            instructions="You are a reviewer.",
        )

        with patch("coderAI.core.agent.load_agent_persona", return_value=persona):
            applied = agent.set_persona("reviewer")

        assert applied is persona
        content = agent.session.messages[0].content
        assert "You are a reviewer." in content
        assert "## Available Tools" in content
        assert "## Strategy for Common Tasks" in content
        assert "read_file" in agent.tools.tools
        assert "write_file" not in agent.tools.tools

    def test_persona_model_switch_updates_context_controller_provider(self):
        from coderAI.core.agents import AgentPersona

        agent = self._make_agent()
        new_provider = MagicMock()
        with patch.object(agent, "_create_provider", return_value=new_provider):
            persona = AgentPersona(
                name="Fast",
                description="Uses another model",
                tools=[],
                model="claude-sonnet-4-6",
                instructions="Use the fast model.",
            )
            agent.apply_persona(persona, update_model=True)

        assert agent.provider is new_provider
        assert agent.context_controller.provider is new_provider


class TestSessionResumeAndCompaction:
    """Tests for resumed-session model activation and durable compaction."""

    def test_activate_resumed_session_model_restores_saved_model(self):
        from coderAI.tui.session_setup import _activate_resumed_session_model

        restored_provider = MagicMock()
        session = MagicMock(model="claude-sonnet-4-6")
        agent = MagicMock()
        agent.session = session
        agent.model = "gpt-5.4-mini"
        agent.provider = MagicMock()
        agent.context_controller = MagicMock()
        agent._create_provider.return_value = restored_provider

        _activate_resumed_session_model(agent, requested_model=None)

        assert agent.model == "claude-sonnet-4-6"
        assert agent.provider is restored_provider
        assert agent.context_controller.provider is restored_provider
        assert session.model == "claude-sonnet-4-6"
        agent.realign_provider_usage_counters.assert_called_once()
        agent._configure_delegate_tool_context.assert_called_once()

    def test_activate_resumed_session_model_honors_explicit_override(self):
        from coderAI.tui.session_setup import _activate_resumed_session_model

        override_provider = MagicMock()
        session = MagicMock(model="claude-sonnet-4-6")
        agent = MagicMock()
        agent.session = session
        agent.model = "claude-sonnet-4-6"
        agent.provider = MagicMock()
        agent.context_controller = MagicMock()
        agent._create_provider.return_value = override_provider

        _activate_resumed_session_model(agent, requested_model="gpt-5.4-mini")

        assert agent.model == "gpt-5.4-mini"
        assert agent.provider is override_provider
        assert agent.context_controller.provider is override_provider
        assert session.model == "gpt-5.4-mini"
        agent.realign_provider_usage_counters.assert_called_once()
        agent._configure_delegate_tool_context.assert_called_once()

    def test_compact_context_persists_successful_compaction(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            provider = MagicMock()
            provider.count_tokens = lambda text: max(1, len(str(text)) // 4)
            with patch.object(Agent, "_create_provider", return_value=provider):
                agent = Agent(model="gpt-5.4-mini", streaming=False)

        agent.create_session()
        agent.session.add_message("user", "older user message")
        agent.session.add_message("assistant", "older assistant message")
        agent.save_session = MagicMock()
        agent.hooks_manager.load_hooks = MagicMock(return_value=None)
        agent.context_controller.manage_context_window = AsyncMock(
            return_value=[
                {
                    "role": "system",
                    "content": "[Prior Conversation Summary]: condensed history",
                },
                {"role": "user", "content": "recent question"},
            ]
        )

        success = asyncio.run(agent.compact_context())

        assert success is True
        agent.save_session.assert_called_once()


class TestDelegateToolContext:
    """Tests for delegate_task inheriting live parent agent state."""

    def _make_agent(self, *, auto_approve: bool = True):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                return Agent(
                    model="gpt-5.4-mini",
                    streaming=False,
                    auto_approve=auto_approve,
                )

    def test_delegate_tool_tracks_auto_approve_and_ipc_server(self):
        agent = self._make_agent(auto_approve=True)
        ipc_server = MagicMock()

        agent.ipc_server = ipc_server
        agent._configure_delegate_tool_context()

        delegate_tool = agent.tools.get("delegate_task")
        assert delegate_tool is not None
        assert delegate_tool.context.parent_auto_approve is True
        assert delegate_tool.context.parent_ipc_server is ipc_server


class TestAgentProjectRules:
    """Tests for rule injection in Agent system prompt."""

    def _make_agent(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                return Agent(model="gpt-5.4-mini", streaming=False)

    def test_get_system_prompt_with_rules(self):
        agent = self._make_agent()

        mock_rule_file = MagicMock()
        mock_rule_file.name = "testing.md"
        mock_rule_file.read_text.return_value = "Always write pytest tests."

        mock_rules_dir = MagicMock()
        mock_rules_dir.exists.return_value = True
        mock_rules_dir.is_dir.return_value = True
        mock_rules_dir.glob.return_value = [mock_rule_file]

        from coderAI.system_prompt import SYSTEM_PROMPT_INTRO

        with patch(
            "coderAI.core.agent.Path",
            side_effect=lambda *args: mock_rules_dir if ".coderAI" in args else MagicMock(),
        ):
            prompt = agent._get_system_prompt()
            assert SYSTEM_PROMPT_INTRO in prompt
            assert "## Available Tools" in prompt
            assert "## Project Specific Rules" in prompt
            assert "### Rule: testing.md" in prompt
            assert "Always write pytest tests." in prompt

    def test_system_prompt_rebuilds_when_mcp_servers_change(self):
        """Toggling an MCP server (changing discovered_tools) must refresh the prompt.

        The connected-MCP appendix mirrors mcp_client.discovered_tools, so the
        cache key has to track it — otherwise /mcp toggles leave a stale prompt.
        """
        import coderAI.tools.mcp as mcp_mod

        agent = self._make_agent()
        fake_client = mcp_mod.MCPClient()
        original = mcp_mod.mcp_client
        mcp_mod.mcp_client = fake_client
        try:
            key_empty = agent._compute_system_prompt_cache_key()
            prompt_empty = agent._get_system_prompt()

            fake_client.discovered_tools = [
                {"server": "fetch", "name": "get", "description": "fetch a url"}
            ]
            key_connected = agent._compute_system_prompt_cache_key()
            prompt_connected = agent._get_system_prompt()
        finally:
            mcp_mod.mcp_client = original

        assert key_empty != key_connected
        assert "mcp__fetch__get" not in prompt_empty
        assert "mcp__fetch__get" in prompt_connected

    def test_get_system_prompt_omits_web_tools_when_disabled(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config(web_tools_in_main=False)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                agent = Agent(model="gpt-5.4-mini", streaming=False)
                prompt = agent._get_system_prompt()
        assert "web_search" not in prompt
        assert "read_url" not in prompt
        assert "## Available Tools" in prompt
        assert "available to you right now" not in prompt
        assert "If web tools are listed under **Available Tools**" in prompt

    def test_get_system_prompt_includes_web_tools_when_enabled(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config(web_tools_in_main=True)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                agent = Agent(model="gpt-5.4-mini", streaming=False)
                prompt = agent._get_system_prompt()
        assert "web_search" in prompt
        assert "read_url" in prompt

    def test_get_system_prompt_includes_web_tools_for_subagent_when_main_config_off(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config(web_tools_in_main=False)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                agent = Agent(model="gpt-5.4-mini", streaming=False, is_subagent=True)
                prompt = agent._get_system_prompt()
        assert "web_search" in prompt


class TestRepositoryPromptHygiene:
    """Regression checks for repo-local personas and prompt content."""

    def test_persona_files_do_not_reference_stale_product_specific_commands(self):
        root = Path(__file__).resolve().parents[1]
        agents_dir = root / ".coderAI" / "agents"

        banned_markers = [
            ".claude/rules",
            "claude /mail",
            "claude /slack",
            "claude /today",
            "claude /schedule-reply",
            "/harness-audit",
            "/update-codemaps",
            "/update-docs",
            "gmail send",
            "conversations_add_message",
            "gog gmail",
            "gog calendar",
            "calendar-suggest.js",
        ]

        for path in agents_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8").lower()
            for marker in banned_markers:
                assert marker not in text, f"{path.name} still references '{marker}'"

    def test_persona_skill_references_resolve_to_real_project_skills(self):
        root = Path(__file__).resolve().parents[1]
        agents_dir = root / ".coderAI" / "agents"
        skills_dir = root / ".coderAI" / "skills"
        available_skills = {path.stem for path in skills_dir.glob("*.md")}

        pattern = r"skill:\s*`([^`]+)`"

        for path in agents_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            refs = set(re.findall(pattern, text, flags=re.IGNORECASE))
            missing = sorted(ref for ref in refs if ref not in available_skills)
            assert not missing, f"{path.name} references missing skills: {missing}"

    def test_persona_descriptions_do_not_claim_automatic_activation(self):
        root = Path(__file__).resolve().parents[1]
        agents_dir = root / ".coderAI" / "agents"

        for path in agents_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            frontmatter = text.split("---", 2)[1].lower()
            assert "must be used" not in frontmatter, (
                f"{path.name} description overpromises routing"
            )
            assert "automatically activated" not in frontmatter, (
                f"{path.name} description overpromises routing"
            )


class TestProcessMessageAfterCancel:
    """Cancelled turns must not strand the asyncio.Event so the next message works."""

    def test_new_message_after_cancel_registers_fresh_tracker(self):
        with patch("coderAI.core.agent.config_manager") as cm:
            from coderAI.system.config import Config

            cfg = Config(budget_limit=0)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.core.agent import Agent
            from coderAI.core.agent_tracker import AgentStatus, agent_tracker

            mock_provider = MagicMock()
            mock_provider.chat = AsyncMock(
                return_value={"choices": [{"message": {"content": "ok"}}]}
            )
            mock_provider.supports_tools.return_value = False
            mock_provider.count_tokens = lambda text: max(1, len(text) // 4)
            mock_provider.get_model_info.return_value = {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
            }

            with patch.object(Agent, "_create_provider", return_value=mock_provider):
                agent = Agent(model="gpt-5.4-mini", streaming=False)

            agent.create_session()
            old_info = agent_tracker.register(
                name="main",
                model="gpt-5.4-mini",
                context_limit=cfg.context_window,
            )
            old_info.request_cancel()
            agent.tracker_info = old_info

            from coderAI.core.agent_loop import ExecutionLoop

            with patch.object(
                ExecutionLoop, "_call_llm_with_retry", new_callable=AsyncMock
            ) as mock_llm:
                mock_llm.return_value = {"content": "ok", "tool_calls": None}
                result = asyncio.run(agent.process_message("hello again"))

            assert result["content"] == "ok"
            assert agent.tracker_info.agent_id != old_info.agent_id
            assert not agent.tracker_info.is_cancelled
            assert agent.tracker_info.status != AgentStatus.CANCELLED

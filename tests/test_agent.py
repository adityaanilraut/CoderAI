"""Tests for the Agent orchestrator."""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from coderAI.context_controller import ContextController



class TestTransientErrorDetection:
    """Tests for Agent._is_transient_error."""

    def _make_agent(self):
        """Create an Agent with minimal mocking."""
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cm.load.return_value = Config()
            cm.load_project_config.return_value = Config()
            from coderAI.agent import Agent

            agent = Agent.__new__(Agent)
            agent._context_controller = ContextController(cm.load.return_value, MagicMock())
            return agent

    def test_timeout_is_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(Exception("Request timed out")) is True

    def test_rate_limit_is_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(Exception("Rate limit exceeded (429)")) is True

    def test_server_error_is_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(Exception("502 Bad Gateway")) is True

    def test_auth_error_is_not_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(Exception("Invalid API key")) is False

    def test_generic_error_is_not_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(ValueError("bad value")) is False

    def test_connection_reset_is_transient(self):
        agent = self._make_agent()
        assert agent._is_transient_error(Exception("Connection reset by peer")) is True


class TestSummarizeToolResult:
    """Tests for Agent._summarize_tool_result."""

    def _make_agent(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(max_tool_output=200)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

            agent = Agent.__new__(Agent)
            agent.config = cfg
            agent._context_controller = ContextController(cfg, MagicMock())
            return agent

    def test_small_result_unchanged(self):
        agent = self._make_agent()
        result = {"success": True, "data": "short"}
        assert agent._summarize_tool_result(result) == result

    def test_large_string_truncated(self):
        agent = self._make_agent()
        result = {"success": True, "content": "x" * 5000}
        summarized = agent._summarize_tool_result(result)
        assert "truncated" in summarized["content"]
        assert len(summarized["content"]) < 5000

    def test_large_list_truncated(self):
        agent = self._make_agent()
        result = {"success": True, "items": list(range(100))}
        summarized = agent._summarize_tool_result(result)
        assert len(summarized["items"]) == 51
        assert "_note" in summarized["items"][-1]


class TestTruncateMessages:
    """Tests for Agent._manage_context_window."""

    def _make_agent(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(context_window=500)  # small window
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

            agent = Agent.__new__(Agent)
            agent.config = cfg
            # Mock provider.count_tokens as simple char/4
            agent.provider = MagicMock()
            agent.provider.count_tokens = lambda text: len(text) // 4
            agent.provider.chat = AsyncMock(return_value={"choices": [{"message": {"content": "summary"}}]})
            agent._context_controller = ContextController(cfg, agent.provider)
            return agent

    def test_preserves_system_messages(self):
        agent = self._make_agent()
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "x" * 2000},
            {"role": "assistant", "content": "y" * 2000},
            {"role": "user", "content": "recent question"},
        ]
        result = asyncio.run(agent._manage_context_window(messages))
        # System message must always be present
        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert system_msgs[0]["content"] == "You are a bot."


    def test_project_config_is_loaded(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            base = Config(temperature=0.7)
            project = Config(temperature=0.1)
            cm.load.return_value = base
            cm.load_project_config.return_value = project

            from coderAI.agent import Agent

            # Patch provider creation to avoid needing a real API key
            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                Agent(model="gpt-5.4-mini", streaming=False)
                # load_project_config should have been called
                cm.load_project_config.assert_called_once_with(".")


class TestAgentsPersonas:
    """Tests for the Agents module personas."""
    
    def test_load_agent_persona_no_dir(self):
        from coderAI.agents import load_agent_persona
        with patch("pathlib.Path.exists", return_value=False):
            persona = load_agent_persona("planner")
            assert persona is None

    def test_load_agent_persona_success(self):
        from coderAI.agents import load_agent_persona
        
        mock_md = """---
name: Planner Agent
description: Plans tasks
tools: [manage_tasks]
model: claude-3-5-sonnet-20241022
---
You are a planner."""
        
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=mock_md):
                persona = load_agent_persona("planner")
                assert persona is not None
                assert persona.name == "Planner Agent"
                assert persona.model == "claude-3-5-sonnet-20241022"
                assert "You are a planner." in persona.instructions

    def test_resolve_persona_name_alias(self):
        from coderAI.agents import resolve_persona_name

        with patch("coderAI.agents._find_agents_dir", return_value=Path("/tmp")):
            with patch(
                "coderAI.agents.get_available_personas",
                return_value=["code-reviewer", "planner"],
            ):
                assert resolve_persona_name("Code Reviewer") == "code-reviewer"

    def test_expand_persona_tools_aliases(self):
        from coderAI.agents import expand_persona_tools

        expanded = expand_persona_tools(["Read", "Edit", "Bash"])

        assert "read_file" in expanded
        assert "search_replace" in expanded
        assert "apply_diff" in expanded
        assert "run_command" in expanded
        assert "run_background" in expanded


class TestAgentPersonaSwitching:
    """Tests for live persona switching on an existing agent session."""

    def _make_agent(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                return Agent(model="gpt-5.4-mini", streaming=False)

    def test_set_persona_updates_live_prompt_and_tools(self):
        from coderAI.agents import AgentPersona

        agent = self._make_agent()
        agent.create_session()

        persona = AgentPersona(
            name="Reviewer",
            description="Reviews code",
            tools=["Read"],
            model=None,
            instructions="You are a reviewer.",
        )

        with patch("coderAI.agent.load_agent_persona", return_value=persona):
            applied = agent.set_persona("reviewer")

        assert applied is persona
        content = agent.session.messages[0].content
        assert "You are a reviewer." in content
        assert "## Available Tools" in content
        assert "## Strategy for Common Tasks" in content
        assert "read_file" in agent.tools.tools
        assert "write_file" not in agent.tools.tools


class TestDelegateToolContext:
    """Tests for delegate_task inheriting live parent agent state."""

    def _make_agent(self, *, auto_approve: bool = True):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

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
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config
            cfg = Config()
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

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

        with patch("pathlib.Path", side_effect=lambda *args: mock_rules_dir if ".coderAI" in args else MagicMock()):
            prompt = agent._get_system_prompt()
            assert SYSTEM_PROMPT_INTRO in prompt
            assert "## Available Tools" in prompt
            assert "## Project Specific Rules" in prompt
            assert "### Rule: testing.md" in prompt
            assert "Always write pytest tests." in prompt

    def test_get_system_prompt_omits_web_tools_when_disabled(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(web_tools_in_main=False)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                agent = Agent(model="gpt-5.4-mini", streaming=False)
                prompt = agent._get_system_prompt()
        assert "web_search" not in prompt
        assert "read_url" not in prompt
        assert "## Available Tools" in prompt
        assert "available to you right now" not in prompt
        assert "If web tools are listed under **Available Tools**" in prompt

    def test_get_system_prompt_includes_web_tools_when_enabled(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(web_tools_in_main=True)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

            with patch.object(Agent, "_create_provider", return_value=MagicMock()):
                agent = Agent(model="gpt-5.4-mini", streaming=False)
                prompt = agent._get_system_prompt()
        assert "web_search" in prompt
        assert "read_url" in prompt

    def test_get_system_prompt_includes_web_tools_for_subagent_when_main_config_off(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(web_tools_in_main=False)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent

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
            assert "must be used" not in frontmatter, f"{path.name} description overpromises routing"
            assert "automatically activated" not in frontmatter, f"{path.name} description overpromises routing"


class TestProcessMessageAfterCancel:
    """Cancelled turns must not strand the asyncio.Event so the next message works."""

    def test_new_message_after_cancel_registers_fresh_tracker(self):
        with patch("coderAI.agent.config_manager") as cm:
            from coderAI.config import Config

            cfg = Config(budget_limit=0)
            cm.load.return_value = cfg
            cm.load_project_config.return_value = cfg
            from coderAI.agent import Agent
            from coderAI.agent_tracker import AgentStatus, agent_tracker

            mock_provider = MagicMock()
            mock_provider.chat = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
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

            with patch.object(agent, "_call_llm_with_retry", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = {"content": "ok", "tool_calls": None}
                result = asyncio.run(agent.process_message("hello again"))

            assert result["content"] == "ok"
            assert agent.tracker_info.agent_id != old_info.agent_id
            assert not agent.tracker_info.is_cancelled
            assert agent.tracker_info.status != AgentStatus.CANCELLED

"""Tests for the Agent orchestrator."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
        assert len(summarized["items"]) == 50
        assert "items_note" in summarized


class TestTruncateMessages:
    """Tests for Agent._truncate_messages_to_fit."""

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
            return agent

    def test_preserves_system_messages(self):
        agent = self._make_agent()
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "x" * 2000},
            {"role": "assistant", "content": "y" * 2000},
            {"role": "user", "content": "recent question"},
        ]
        result = agent._truncate_messages_to_fit(messages)
        # System message must always be present
        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert system_msgs[0]["content"] == "You are a bot."


class TestProjectConfigInAgent:
    """Tests for per-project config loading at init."""

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
                agent = Agent(model="gpt-5-mini", streaming=False)
                # load_project_config should have been called
                cm.load_project_config.assert_called_once_with(".")

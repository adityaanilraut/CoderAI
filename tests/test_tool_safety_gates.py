"""Regression coverage for confirmation metadata on side-effecting tools."""

import inspect

from coderAI.core.agent import Agent
from coderAI.tools.git import GitFetchTool
from coderAI.tools.lint import LintTool
from coderAI.tools.mcp import MCPCallTool, MCPDisconnectTool
from coderAI.tools.testing import RunTestsTool
from coderAI.tools.undo import UndoTool
from coderAI.tools.web import DownloadFileTool, HTTPRequestTool


def test_side_effecting_tools_require_confirmation() -> None:
    for tool in (
        LintTool(),
        DownloadFileTool(),
        HTTPRequestTool(),
        MCPCallTool(),
        MCPDisconnectTool(),
        UndoTool(),
        GitFetchTool(),
        RunTestsTool(),
    ):
        assert tool.requires_confirmation is True, tool.name


def test_direct_agent_defaults_to_confirmations_enabled() -> None:
    assert inspect.signature(Agent).parameters["auto_approve"].default is False

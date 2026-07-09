"""Regression coverage for confirmation metadata on side-effecting tools."""

import inspect

from coderAI.core.agent import Agent
from coderAI.tools.git import GitAddTool, GitCommitTool
from coderAI.tools.git_extended import GitFetchTool
from coderAI.tools.lint import LintTool
from coderAI.tools.mcp import MCPDisconnectTool
from coderAI.tools.testing import RunTestsTool
from coderAI.tools.undo import UndoTool
from coderAI.tools.web import DownloadFileTool, HTTPRequestTool
from coderAI.tools.desktop import (
    RunAppleScriptTool,
    ClickUIElementTool,
    TypeKeystrokesTool,
)


def test_side_effecting_tools_require_confirmation() -> None:
    for tool in (
        LintTool(),
        DownloadFileTool(),
        HTTPRequestTool(),
        MCPDisconnectTool(),
        UndoTool(),
        GitAddTool(),
        GitCommitTool(),
        GitFetchTool(),
        RunTestsTool(),
        RunAppleScriptTool(),
        ClickUIElementTool(),
        TypeKeystrokesTool(),
    ):
        assert tool.requires_confirmation is True, tool.name


def test_direct_agent_defaults_to_confirmations_enabled() -> None:
    assert inspect.signature(Agent).parameters["auto_approve"].default is False

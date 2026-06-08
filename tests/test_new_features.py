"""Tests for new configuration, session-scoped backup, and MCP auto-connect features."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from coderAI.system.config import config_manager
from coderAI.system.history import history_manager, Session
from coderAI.tools.undo import FileBackupStore
from coderAI.tools.web import _select_search_backend, _TavilyBackend, _ExaBackend, _DDGBackend
from coderAI.core.agent_loop import ExecutionLoop


def test_session_scoped_backups(monkeypatch, tmp_path):
    # Set home directory to a temp path to avoid modifying user home
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # When no session is active, it should use 'global'
    history_manager.current_session = None
    store_global = FileBackupStore()
    assert store_global.backup_dir == tmp_path / ".coderAI" / "backups" / "global"

    # Create a session
    session = Session(session_id="session_12345_test", model="gpt-4")
    history_manager.current_session = session

    # Now it should resolve to the session subdirectory
    store_session = FileBackupStore()
    assert store_session.backup_dir == tmp_path / ".coderAI" / "backups" / "session_12345_test"

    # Switch session mid-run
    session2 = Session(session_id="session_67890_test", model="gpt-4")
    history_manager.current_session = session2
    assert store_session.backup_dir == tmp_path / ".coderAI" / "backups" / "session_67890_test"

    # Clean up
    history_manager.current_session = None


def test_web_search_backend_fallbacks(monkeypatch):
    # Ensure env vars are not set
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)

    # Test Tavily fallback via config
    config_manager.set("search_backend", "tavily")
    config_manager.set("tavily_api_key", "tavily-config-key")
    backend = _select_search_backend()
    assert isinstance(backend, _TavilyBackend)
    assert backend.api_key == "tavily-config-key"

    # Test Exa fallback via config
    config_manager.set("search_backend", "exa")
    config_manager.set("exa_api_key", "exa-config-key")
    backend = _select_search_backend()
    assert isinstance(backend, _ExaBackend)
    assert backend.api_key == "exa-config-key"

    # Test DDG fallback
    config_manager.set("search_backend", "ddg")
    backend = _select_search_backend()
    assert isinstance(backend, _DDGBackend)

    # Reset config settings
    config_manager.set("search_backend", None)
    config_manager.set("tavily_api_key", None)
    config_manager.set("exa_api_key", None)


@pytest.mark.asyncio
async def test_mcp_autoconnect(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Write a mock mcp_servers.json file
    mcp_config_dir = tmp_path / ".coderAI"
    mcp_config_dir.mkdir(parents=True, exist_ok=True)
    mcp_servers_file = mcp_config_dir / "mcp_servers.json"

    servers_data = {
        "mcpServers": {
            "mock_stdio": {"command": "npx", "args": ["mock-server-args"]},
            "mock_sse": {"transport": "sse", "url": "http://localhost:8080/sse"},
        }
    }
    with open(mcp_servers_file, "w") as f:
        json.dump(servers_data, f)

    # Mock the mcp_client connect methods
    mock_mcp_client = MagicMock()
    mock_mcp_client.servers = {}
    mock_mcp_client.connect_stdio = AsyncMock(return_value={"success": True})
    mock_mcp_client.connect_sse = AsyncMock(return_value={"success": True})

    monkeypatch.setattr("coderAI.tools.mcp.mcp_client", mock_mcp_client)

    # Initialize a mock agent and execution loop
    mock_agent = MagicMock()
    loop = ExecutionLoop(agent=mock_agent)

    await loop._autoconnect_mcp_servers()

    # Verify that connect_stdio and connect_sse were called correctly
    mock_mcp_client.connect_stdio.assert_called_once_with("mock_stdio", "npx", ["mock-server-args"])
    mock_mcp_client.connect_sse.assert_called_once_with("mock_sse", "http://localhost:8080/sse")

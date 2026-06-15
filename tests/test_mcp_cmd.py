"""Coverage for the `coderAI mcp` command group (coderAI/cli/mcp_cmd.py)."""

import json

import pytest
from click.testing import CliRunner

from coderAI.cli.mcp_cmd import mcp


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Point the MCP config at a temp dir so tests never touch ~/.coderAI.

    All three helpers (mcp_servers_path/load/save) resolve through
    ``config_manager.config_dir``, so patching it is the single source of truth.
    """
    monkeypatch.setattr("coderAI.system.config.config_manager.config_dir", tmp_path)
    return tmp_path / "mcp_servers.json"


def _read(cfg_file):
    return json.loads(cfg_file.read_text())


# ── add: stdio ───────────────────────────────────────────────────────────


def test_add_stdio_round_trip(runner, cfg_file):
    result = runner.invoke(
        mcp,
        ["add", "filesystem", "--command", "npx", "--args", "-y,@scope/server,/tmp"],
    )
    assert result.exit_code == 0, result.output
    data = _read(cfg_file)
    assert data == {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@scope/server", "/tmp"],
            }
        }
    }


def test_add_stdio_without_args(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "bare", "--command", "uvx"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["bare"] == {"command": "uvx", "args": []}


# ── add: sse ─────────────────────────────────────────────────────────────


def test_add_sse_round_trip(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "remote", "--sse", "http://localhost:8080/sse"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["remote"] == {
        "transport": "sse",
        "url": "http://localhost:8080/sse",
    }


# ── add: validation ──────────────────────────────────────────────────────


def test_add_rejects_disallowed_launcher(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "bad", "--command", "rm"])
    assert result.exit_code == 2
    assert "not allowed" in result.output
    assert not cfg_file.exists()


def test_add_rejects_double_underscore_name(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "a__b", "--command", "npx"])
    assert result.exit_code == 2
    assert "__" in result.output
    assert not cfg_file.exists()


def test_add_rejects_both_transports(runner, cfg_file):
    result = runner.invoke(
        mcp, ["add", "x", "--command", "npx", "--sse", "http://h/sse"]
    )
    assert result.exit_code == 2
    assert not cfg_file.exists()


def test_add_requires_a_transport(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "x"])
    assert result.exit_code == 2
    assert not cfg_file.exists()


def test_add_allows_pathed_launcher(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "p", "--command", "/usr/local/bin/node"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["p"]["command"] == "/usr/local/bin/node"


def test_add_overwrites_existing(runner, cfg_file):
    runner.invoke(mcp, ["add", "dup", "--command", "npx"])
    result = runner.invoke(mcp, ["add", "dup", "--sse", "http://h/sse"])
    assert result.exit_code == 0, result.output
    assert "Overwriting" in result.output
    assert _read(cfg_file)["mcpServers"]["dup"] == {
        "transport": "sse",
        "url": "http://h/sse",
    }


def test_add_preserves_other_servers(runner, cfg_file):
    runner.invoke(mcp, ["add", "one", "--command", "npx"])
    runner.invoke(mcp, ["add", "two", "--sse", "http://h/sse"])
    servers = _read(cfg_file)["mcpServers"]
    assert set(servers) == {"one", "two"}


# ── list ─────────────────────────────────────────────────────────────────


def test_list_empty(runner, cfg_file):
    result = runner.invoke(mcp, ["list"])
    assert result.exit_code == 0
    assert "No MCP servers configured" in result.output


def test_list_shows_servers(runner, cfg_file):
    runner.invoke(mcp, ["add", "filesystem", "--command", "npx", "--args", "-y"])
    runner.invoke(mcp, ["add", "remote", "--sse", "http://localhost:8080/sse"])
    result = runner.invoke(mcp, ["list"])
    assert result.exit_code == 0
    assert "filesystem" in result.output
    assert "remote" in result.output


# ── remove ───────────────────────────────────────────────────────────────


def test_remove_present(runner, cfg_file):
    runner.invoke(mcp, ["add", "gone", "--command", "npx"])
    result = runner.invoke(mcp, ["remove", "gone"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"] == {}


def test_remove_absent_exits_1(runner, cfg_file):
    result = runner.invoke(mcp, ["remove", "nope"])
    assert result.exit_code == 1
    assert "No MCP server named" in result.output


# ── interop: the written shape is what auto-connect consumes ──────────────


def test_written_shape_matches_autoconnect_reader(runner, cfg_file):
    """The CLI's output must be loadable by load_mcp_servers (used at startup)."""
    from coderAI.tools.mcp import load_mcp_servers

    runner.invoke(
        mcp, ["add", "fs", "--command", "python3", "--args", "-m,server"]
    )
    servers = load_mcp_servers().get("mcpServers", {})
    assert servers["fs"] == {"command": "python3", "args": ["-m", "server"]}

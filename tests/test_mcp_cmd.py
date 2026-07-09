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


# ── add: command after `--` (Claude-Code-style) ───────────────────────────


def test_add_stdio_after_dashdash(runner, cfg_file):
    """`mcp add NAME --transport stdio -- npx -y @scope/server` must work."""
    result = runner.invoke(
        mcp,
        ["add", "fun", "--transport", "stdio", "--", "npx", "-y", "@scope/server"],
    )
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["fun"] == {
        "command": "npx",
        "args": ["-y", "@scope/server"],
    }


def test_add_after_dashdash_defaults_to_stdio(runner, cfg_file):
    """Transport defaults to stdio when a command is given without --transport."""
    result = runner.invoke(mcp, ["add", "fetch", "--", "uvx", "mcp-server-fetch"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["fetch"] == {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
    }


def test_add_sse_after_dashdash(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "remote", "--transport", "sse", "--", "https://h/sse"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["remote"] == {
        "transport": "sse",
        "url": "https://h/sse",
    }


def test_add_sse_after_dashdash_rejects_extra_args(runner, cfg_file):
    result = runner.invoke(
        mcp, ["add", "remote", "--transport", "sse", "--", "http://h/sse", "extra"]
    )
    assert result.exit_code == 2
    assert not cfg_file.exists()


def test_add_rejects_disallowed_launcher_after_dashdash(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "bad", "--", "rm", "-rf"])
    assert result.exit_code == 2
    assert "not allowed" in result.output
    assert not cfg_file.exists()


def test_add_rejects_command_and_dashdash_together(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "x", "--command", "npx", "--", "npx", "y"])
    assert result.exit_code == 2
    assert not cfg_file.exists()


# ── add: sse ─────────────────────────────────────────────────────────────


def test_add_sse_round_trip(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "remote", "--sse", "http://localhost:8080/sse"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["remote"] == {
        "transport": "sse",
        "url": "http://localhost:8080/sse",
    }


# ── add: http (Streamable HTTP) ───────────────────────────────────────────


def test_add_http_after_dashdash(runner, cfg_file):
    """`mcp add NAME --transport http -- https://host/mcp` must work."""
    result = runner.invoke(
        mcp, ["add", "strava", "--transport", "http", "--", "https://mcp.strava.com/mcp"]
    )
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["strava"] == {
        "transport": "http",
        "url": "https://mcp.strava.com/mcp",
    }


def test_add_http_flag_round_trip(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "api", "--http", "https://host/mcp"])
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["api"] == {
        "transport": "http",
        "url": "https://host/mcp",
    }


def test_add_http_with_headers(runner, cfg_file):
    result = runner.invoke(
        mcp,
        ["add", "api", "--http", "https://host/mcp", "-H", "Authorization: Bearer TOK"],
    )
    assert result.exit_code == 0, result.output
    assert _read(cfg_file)["mcpServers"]["api"] == {
        "transport": "http",
        "url": "https://host/mcp",
        "headers": {"Authorization": "Bearer TOK"},
    }


def test_add_http_after_dashdash_rejects_extra_args(runner, cfg_file):
    result = runner.invoke(
        mcp, ["add", "api", "--transport", "http", "--", "https://host/mcp", "extra"]
    )
    assert result.exit_code == 2
    assert not cfg_file.exists()


def test_add_header_rejected_for_non_http(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "remote", "--sse", "http://h/sse", "-H", "X: y"])
    assert result.exit_code == 2
    assert "http" in result.output.lower()
    assert not cfg_file.exists()


def test_add_malformed_header_rejected(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "api", "--http", "https://host/mcp", "-H", "no-colon"])
    assert result.exit_code == 2
    assert not cfg_file.exists()


def test_add_rejects_sse_and_http_together(runner, cfg_file):
    result = runner.invoke(mcp, ["add", "x", "--sse", "http://h/sse", "--http", "https://h/mcp"])
    assert result.exit_code == 2
    assert not cfg_file.exists()


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
    result = runner.invoke(mcp, ["add", "x", "--command", "npx", "--sse", "http://h/sse"])
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
    result = runner.invoke(mcp, ["add", "dup", "--sse", "https://h/sse"])
    assert result.exit_code == 0, result.output
    assert "Overwriting" in result.output
    assert _read(cfg_file)["mcpServers"]["dup"] == {
        "transport": "sse",
        "url": "https://h/sse",
    }


def test_add_preserves_other_servers(runner, cfg_file):
    runner.invoke(mcp, ["add", "one", "--command", "npx"])
    runner.invoke(mcp, ["add", "two", "--sse", "https://h/sse"])
    servers = _read(cfg_file)["mcpServers"]
    assert set(servers) == {"one", "two"}


# ── list ─────────────────────────────────────────────────────────────────


def test_list_empty(runner, cfg_file):
    result = runner.invoke(mcp, ["list"])
    assert result.exit_code == 0
    # Bundled git_extended always appears even with an empty on-disk config.
    assert "git_extended" in result.output
    assert "bundled" in result.output


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


# ── login / logout (OAuth) ─────────────────────────────────────────────────


def test_login_unknown_server_exits_1(runner, cfg_file):
    result = runner.invoke(mcp, ["login", "ghost"])
    assert result.exit_code == 1
    assert "No MCP server" in result.output


def test_login_rejects_non_http(runner, cfg_file):
    runner.invoke(mcp, ["add", "fs", "--command", "npx"])
    result = runner.invoke(mcp, ["login", "fs"])
    assert result.exit_code == 2
    assert "HTTP" in result.output


def test_login_happy_path(runner, cfg_file):
    runner.invoke(mcp, ["add", "strava", "--http", "https://mcp.strava.com/mcp"])
    from unittest.mock import patch

    with patch("coderAI.tools.mcp_oauth.login", return_value={"scope": "read"}) as login:
        result = runner.invoke(mcp, ["login", "strava"])

    assert result.exit_code == 0, result.output
    assert "Authorized" in result.output
    login.assert_called_once()
    assert login.call_args.args[0] == "strava"
    assert login.call_args.args[1] == "https://mcp.strava.com/mcp"


def test_login_surfaces_oauth_error(runner, cfg_file):
    runner.invoke(mcp, ["add", "strava", "--http", "https://mcp.strava.com/mcp"])
    from unittest.mock import patch
    from coderAI.tools.mcp_oauth import OAuthError

    with patch("coderAI.tools.mcp_oauth.login", side_effect=OAuthError("nope")):
        result = runner.invoke(mcp, ["login", "strava"])

    assert result.exit_code == 1
    assert "Login failed" in result.output


def test_logout_round_trip(runner, cfg_file):
    from unittest.mock import patch

    with patch("coderAI.tools.mcp_oauth.logout", return_value=True):
        result = runner.invoke(mcp, ["logout", "strava"])
    assert result.exit_code == 0
    assert "Logged out" in result.output

    with patch("coderAI.tools.mcp_oauth.logout", return_value=False):
        result = runner.invoke(mcp, ["logout", "strava"])
    assert result.exit_code == 0
    assert "No saved credentials" in result.output


def test_remove_also_deletes_credentials(runner, cfg_file):
    runner.invoke(mcp, ["add", "strava", "--http", "https://mcp.strava.com/mcp"])
    from unittest.mock import patch

    with patch("coderAI.tools.mcp_oauth.delete_credentials") as dc:
        result = runner.invoke(mcp, ["remove", "strava"])
    assert result.exit_code == 0, result.output
    dc.assert_called_once_with("strava")


# ── interop: the written shape is what auto-connect consumes ──────────────


def test_written_shape_matches_autoconnect_reader(runner, cfg_file):
    """The CLI's output must be loadable by load_mcp_servers (used at startup)."""
    from coderAI.tools.mcp import load_mcp_servers

    runner.invoke(mcp, ["add", "fs", "--command", "python3", "--args", "-m,server"])
    servers = load_mcp_servers().get("mcpServers", {})
    assert servers["fs"] == {"command": "python3", "args": ["-m", "server"]}


# ── resources / prompts ──────────────────────────────────────────────────


def _add_server(runner):
    runner.invoke(mcp, ["add", "fs", "--command", "npx", "--args", "-y,server"])


def test_resources_unknown_server(runner, cfg_file):
    result = runner.invoke(mcp, ["resources", "nope"])
    assert result.exit_code == 1
    assert "No MCP server" in result.output


def test_resources_lists_table(runner, cfg_file, monkeypatch):
    _add_server(runner)

    async def fake(name, entry, kind):
        assert kind == "resources"
        return {
            "success": True,
            "resources": [
                {"uri": "file:///a.txt", "name": "a", "mimeType": "text/plain", "description": "d"}
            ],
        }

    monkeypatch.setattr("coderAI.cli.mcp_cmd._connect_and_list", fake)
    result = runner.invoke(mcp, ["resources", "fs"])
    assert result.exit_code == 0, result.output
    assert "a.txt" in result.output


def test_resources_connect_failure_exits_nonzero(runner, cfg_file, monkeypatch):
    _add_server(runner)

    async def fake(name, entry, kind):
        return {"success": False, "error": "boom"}

    monkeypatch.setattr("coderAI.cli.mcp_cmd._connect_and_list", fake)
    result = runner.invoke(mcp, ["resources", "fs"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_resources_empty(runner, cfg_file, monkeypatch):
    _add_server(runner)

    async def fake(name, entry, kind):
        return {"success": True, "resources": []}

    monkeypatch.setattr("coderAI.cli.mcp_cmd._connect_and_list", fake)
    result = runner.invoke(mcp, ["resources", "fs"])
    assert result.exit_code == 0, result.output
    assert "no resources" in result.output.lower()


def test_prompts_lists_table(runner, cfg_file, monkeypatch):
    _add_server(runner)

    async def fake(name, entry, kind):
        assert kind == "prompts"
        return {
            "success": True,
            "prompts": [
                {"name": "summarize", "description": "d", "arguments": [{"name": "topic"}]}
            ],
        }

    monkeypatch.setattr("coderAI.cli.mcp_cmd._connect_and_list", fake)
    result = runner.invoke(mcp, ["prompts", "fs"])
    assert result.exit_code == 0, result.output
    assert "summarize" in result.output

"""CLI tests for ``coderai mcp`` server management.

Drives the in-process ``coderai.cli.mcp`` dispatcher (the ``mcp`` entry point
shares the interactive CLI import chain, which is unsuitable for subprocess
use in minimal environments) with ``HOME``/share-dir isolation so the real
``~/.coderai/mcp.json`` registry is never touched.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(home_dir / ".coderai"))
    return home_dir


def _run_mcp(args: list[str]) -> tuple[int, str]:
    from coderai.cli.mcp import run_mcp

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run_mcp(args)
    return code, buf.getvalue()


def _normalize(text: str, home_dir: Path) -> str:
    normalized = text.replace(str(home_dir), "<home_dir>")
    normalized = normalized.replace(sys.executable, "<python>")
    return normalized


def _load_registry(home_dir: Path) -> dict:
    path = home_dir / ".coderai" / "mcp.json"
    assert path.exists(), "expected the MCP registry file to be written"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_mcp_stdio_management(tmp_path: Path, monkeypatch) -> None:
    """add/list/test/remove round-trip for a stdio server."""
    home_dir = _isolate_home(tmp_path, monkeypatch)
    server_path = tmp_path / "mcp_server.py"
    server_path.write_text("print('noop')\n", encoding="utf-8")

    code, out = _run_mcp(
        ["add", "--transport", "stdio", "test", "--", sys.executable, str(server_path)]
    )
    assert code == 0
    assert _normalize(out, home_dir) == (
        "Added MCP server 'test' to <home_dir>/.coderai/mcp.json.\n"
    )
    registry = _load_registry(home_dir)
    assert registry == {
        "mcpServers": {"test": {"command": sys.executable, "args": [str(server_path)]}}
    }

    code, out = _run_mcp(["list"])
    assert code == 0
    assert _normalize(out, home_dir) == (
        f"MCP config file: <home_dir>/.coderai/mcp.json\n  test (stdio): <python> {server_path}\n"
    ).replace(str(server_path), str(server_path))

    code, out = _run_mcp(["test", "test"])
    assert code == 0
    assert _normalize(out, home_dir).startswith("✓ 'test' stdio entry looks valid: <python>")

    code, out = _run_mcp(["remove", "test"])
    assert code == 0
    assert _normalize(out, home_dir) == (
        "Removed MCP server 'test' from <home_dir>/.coderai/mcp.json.\n"
    )
    assert _load_registry(home_dir) == {"mcpServers": {}}

    code, out = _run_mcp(["list"])
    assert code == 0
    assert _normalize(out, home_dir) == (
        "MCP config file: <home_dir>/.coderai/mcp.json\nNo MCP servers configured.\n"
    )


def test_mcp_http_management_and_auth_errors(tmp_path: Path, monkeypatch) -> None:
    """HTTP servers, renamed auth subcommands, and missing-server errors."""
    home_dir = _isolate_home(tmp_path, monkeypatch)

    code, out = _run_mcp(
        ["add", "--transport", "http", "remote", "https://example.com/mcp", "--header", "X-Test: 1"]
    )
    assert code == 0
    assert _normalize(out, home_dir) == (
        "Added MCP server 'remote' to <home_dir>/.coderai/mcp.json.\n"
    )
    registry = _load_registry(home_dir)
    assert registry["mcpServers"]["remote"] == {
        "url": "https://example.com/mcp",
        "transport": "http",
        "headers": {"X-Test": "1"},
    }

    code, out = _run_mcp(["list"])
    assert code == 0
    assert _normalize(out, home_dir) == (
        "MCP config file: <home_dir>/.coderai/mcp.json\n  remote (http): https://example.com/mcp\n"
    )

    # ``auth`` / ``reset-auth`` were renamed to ``login`` / ``logout``.
    code, out = _run_mcp(["auth", "remote"])
    assert code != 0
    assert "was renamed" in out

    code, out = _run_mcp(["reset-auth", "remote"])
    assert code != 0
    assert "was renamed" in out

    code, out = _run_mcp(["remove", "missing"])
    assert code != 0
    assert out == "MCP server 'missing' not found.\n"

    code, out = _run_mcp(["bogus"])
    assert code != 0
    assert "Unknown 'mcp' subcommand" in out

    code, out = _run_mcp(["test", "remote"])
    assert code == 0
    assert out.startswith("✓ 'remote' http entry looks valid:")

    code, out = _run_mcp(["remove", "remote"])
    assert code == 0
    assert _load_registry(home_dir) == {"mcpServers": {}}

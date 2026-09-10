"""Consolidated MCP transports, OAuth, plugins, skills, LSP, and plan review."""

from __future__ import annotations

import asyncio
import http.server
import json
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from coderai.cli.plan_review import extract_plan_options, prompt_plan_review
from coderai.core.lsp.client import LspClient
from coderai.core.lsp.protocol import LspFrameParser, encode_lsp_message
from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager
from coderai.core.mcp.oauth import (
    apply_bearer_auth,
    has_server_token,
    resolve_server_bearer_token,
    store_server_token,
)
from coderai.core.oauth import (
    KIMI_CODE_OAUTH_KEY,
    OAuthDeviceExpired,
    OAuthManager,
    OAuthToken,
    delete_token,
    load_token,
    request_device_authorization,
    save_token,
    wait_for_device_token,
)
from coderai.core.plugin import PluginError, parse_plugin_json
from coderai.core.plugin.manager import (
    collect_host_values,
    install_plugin,
    list_plugins,
    refresh_plugin_configs,
    remove_plugin,
)
from coderai.core.plugin.tool import find_plugin_tool, run_plugin_tool
from coderai.core.skill import list_skills
from coderai.core.tools.lsp import handle_lsp_tool


@pytest.fixture
def share_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the share/credentials tree per test."""
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(share))
    return share


_STDIO_SERVER = (
    "import sys,json\nT=[{'name':'stdio_tool','description':'Stdio tool',"
    "'inputSchema':{'type':'object','properties':{'msg':{'type':'string'}}}}]\n"
    "def ans(i,r):\n"
    "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':i,'result':r})+'\\n');sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    q=json.loads(line);m,i,a=q.get('method'),q.get('id'),q.get('params',{}).get('arguments',{})\n"
    "    r={'protocolVersion':'2024-11-05'} if m=='initialize' else {'tools':T} if m=='tools/list' else \\\n"
    "      {'content':[{'type':'text','text':f\"Hello {a.get('msg')}\"}]} if m=='tools/call' else {}\n"
    "    if i is not None: ans(i,r)\n"
)


async def test_mcp_stdio_discovers_tool_schema():
    """Stdio transport connects, lists the tool schema, calls it, and disconnects."""
    client = McpClient("test_stdio", sys.executable, args=["-c", _STDIO_SERVER])
    try:
        await client.connect(timeout_s=10.0)
        assert client.is_connected() and client._tools[0]["name"] == "stdio_tool"
        assert (await client.call_tool("stdio_tool", {"msg": "World"}))["content"][0][
            "text"
        ] == "Hello World"
    finally:
        await client.disconnect()
        assert not client.is_connected()


class _MockSseHandler(http.server.BaseHTTPRequestHandler):
    """Minimal SSE endpoint plus JSON-RPC message responder for tests."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/sse":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(b"event: endpoint\ndata: /message\n\n")
        self.wfile.flush()
        try:
            while getattr(self.server, "running", True):
                time.sleep(0.05)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        if self.path != "/message":
            self.send_response(404)
            self.end_headers()
            return
        req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        msg_id, method = req.get("id"), req.get("method")
        text = req.get("params", {}).get("arguments", {}).get("text")
        tools = [
            {
                "name": "echo_sse",
                "description": "Echo over SSE",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }
        ]
        result = (
            {"protocolVersion": "2024-11-05"}
            if method == "initialize"
            else {"tools": tools}
            if method == "tools/list"
            else {"content": [{"type": "text", "text": f"Echo: {text}"}], "isError": False}
            if method == "tools/call"
            else {}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        transport = getattr(self.server, "active_transport", None)
        if msg_id is not None and transport and transport.on_message:
            transport.on_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


async def test_mcp_sse_discovers_tool_schema():
    """SSE transport connects over localhost, lists tools, and echoes a call."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockSseHandler)
    server.running = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    client = McpClient("test_sse", {"url": f"http://127.0.0.1:{port}/sse", "allowPrivateIps": True})
    server.active_transport = client.transport
    try:
        try:
            await client.connect(timeout_s=10.0)
        except (RuntimeError, PermissionError, OSError) as e:
            if "ermission" in str(e):
                pytest.skip("Local socket connection restricted in sandbox")
            raise
        assert client.is_connected() and client._tools[0]["name"] == "echo_sse"
        res = await client.call_tool("echo_sse", {"text": "SSE Test"})
        assert res["content"][0]["text"] == "Echo: SSE Test"
    finally:
        await client.disconnect()
        server.running = False
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


async def test_mcp_manager_converts_discovered_schema():
    """Manager discovery namespaces tools and converts schemas to definitions."""
    manager = McpManager()
    try:
        await manager.initialize(
            {"db_server": {"command": sys.executable, "args": ["-c", _STDIO_SERVER]}}
        )
        assert manager.list_tools()[0].namespaced_name == "mcp__db_server__stdio_tool"
        defs = manager.get_mcp_tool_definitions()
        assert defs[0]["type"] == "function" and "properties" in defs[0]["function"]["parameters"]
        assert any(s.name == "db_server" and s.connected for s in manager.get_status())
    finally:
        await manager.disconnect()


async def test_mcp_manager_reports_unauthorized_status(monkeypatch):
    """A 401 from an OAuth-guarded server surfaces as an unauthorized status."""
    import coderai.core.mcp.manager as manager_mod

    class _FailClient:
        last_http_status = 401

        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def set_on_disconnect(self, handler: Any) -> None:
            pass

        async def connect(self, timeout_s: float = 30.0) -> None:
            raise RuntimeError("connect failed")

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(manager_mod, "McpClient", _FailClient)
    manager = McpManager()
    await manager._connect_server("guarded", {"url": "https://x.example/mcp", "auth": "oauth"})
    status = next(s for s in manager.server_statuses if s.name == "guarded")
    assert status.status == "unauthorized" and "mcp login" in (status.error or "")


async def test_mcp_deferred_loads_in_background(tmp_path):
    """Session managers start MCP loading in the background and await readiness."""
    from coderai.core.session import SessionManager

    stub_client = lambda: {"client": None, "model": "m"}  # noqa: E731
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=stub_client,
        get_resolved_settings=stub_client,
    )
    assert mgr.mcp_manager.initialized is False
    mgr.start_background_mcp_loading()
    assert mgr._mcp_load_task is not None
    await mgr._await_mcp_ready()
    assert mgr.mcp_manager.initialized is True


def test_mcp_bearer_resolves_token_sources(
    share_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bearer tokens resolve from inline config, env, file, and the token store."""
    assert resolve_server_bearer_token({}, "srv") is None
    assert resolve_server_bearer_token({"token": "  inline  "}, "srv") == "inline"
    monkeypatch.setenv("MCP_SRV_TOKEN", "env-token")
    assert resolve_server_bearer_token({"tokenEnv": "MCP_SRV_TOKEN"}, "srv") == "env-token"
    tok_file = tmp_path / "tok.txt"
    tok_file.write_text("file-token\n", encoding="utf-8")
    assert resolve_server_bearer_token({"tokenFile": str(tok_file)}, "srv") == "file-token"
    assert store_server_token("srv", "stored-token").is_file()
    assert resolve_server_bearer_token({"auth": "oauth"}, "srv") == "stored-token"
    assert has_server_token("srv", {"auth": "oauth"})


def test_mcp_bearer_applies_authorization_header(share_dir: Path):
    """Stored tokens become Authorization headers unless one is already set."""
    store_server_token("srv", "stored-token")
    merged = apply_bearer_auth(
        {"url": "https://x.example/mcp", "auth": "oauth", "headers": {"X-A": "b"}}, "srv"
    )
    assert merged["headers"] == {"X-A": "b", "Authorization": "Bearer stored-token"}
    explicit = apply_bearer_auth(
        {"url": "https://x.example/mcp", "headers": {"Authorization": "Custom x"}}, "srv"
    )
    assert explicit["headers"]["Authorization"] == "Custom x"
    plain = {"url": "https://x.example/mcp"}
    assert apply_bearer_auth(plain, "srv") is plain


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload, self.status_code = payload, status_code

    def json(self) -> Any:
        return self._payload


def test_oauth_device_flow_issues_token(monkeypatch: pytest.MonkeyPatch, share_dir: Path):
    """Device authorization plus one pending poll yields a stored token."""
    import requests

    polls = {"n": 0}
    device = {
        "user_code": "ABCD-1234",
        "device_code": "dev",
        "verification_uri": "https://x.example/activate",
        "verification_uri_complete": "https://x.example/activate?c=ABCD",
        "expires_in": 600,
        "interval": 0,
    }
    granted = {
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_in": 3600,
        "scope": "",
        "token_type": "bearer",
    }

    def _post(url: str, **kwargs: Any) -> _StubResponse:
        if url.endswith("device_authorization"):
            return _StubResponse(device)
        polls["n"] += 1
        return (
            _StubResponse({"error": "authorization_pending"})
            if polls["n"] == 1
            else _StubResponse(granted)
        )

    monkeypatch.setattr(requests, "post", _post)
    auth = request_device_authorization()
    assert auth.user_code == "ABCD-1234"
    seen: list[str] = []
    token = wait_for_device_token(auth, on_waiting=seen.append)
    assert token.access_token == "tok" and seen == ["authorization_pending"]
    save_token(KIMI_CODE_OAUTH_KEY, token)
    assert load_token(KIMI_CODE_OAUTH_KEY) is not None


def test_oauth_device_flow_rejects_expired_token(monkeypatch: pytest.MonkeyPatch):
    """An expired device code raises instead of polling forever."""
    import requests

    def _post(url: str, **kwargs: Any) -> _StubResponse:
        device = {
            "user_code": "U",
            "device_code": "D",
            "verification_uri": "",
            "verification_uri_complete": "https://x.example/",
            "expires_in": 600,
            "interval": 0,
        }
        return (
            _StubResponse(device)
            if url.endswith("device_authorization")
            else _StubResponse({"error": "expired_token"})
        )

    monkeypatch.setattr(requests, "post", _post)
    with pytest.raises(OAuthDeviceExpired):
        wait_for_device_token(request_device_authorization())


def test_oauth_store_protects_token_file(share_dir: Path):
    """Stored tokens land on disk with owner-only permissions and delete cleanly."""
    save_token(
        "oauth/test", OAuthToken(access_token="a", refresh_token="r", expires_at=9999999999.0)
    )
    path = share_dir / "credentials" / "test.json"
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (loaded := load_token("oauth/test")) is not None and loaded.access_token == "a"
    delete_token("oauth/test")
    assert load_token("oauth/test") is None


def test_oauth_manager_refreshes_stale_token(share_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """The manager swaps a stale token for a fresh one via the refresh hook."""
    import coderai.core.oauth as oauth_mod

    save_token("oauth/k3", OAuthToken(access_token="old", refresh_token="live", expires_at=1.0))
    fresh = OAuthToken(access_token="new", refresh_token="live2", expires_at=9999999999.0)
    monkeypatch.setattr(oauth_mod, "refresh_access_token", lambda rt, **k: fresh)
    manager = OAuthManager(["oauth/k3"])
    asyncio.run(manager.ensure_fresh())
    assert manager.get_cached_access_token("oauth/k3") == "new"
    assert manager.resolve_api_key("sk-static", "oauth/k3") == "new"


def _write_plugin(root: Path, name: str = "demo", tools: Any = "default") -> Path:
    """Create a minimal on-disk plugin source tree for install tests."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if tools == "default":
        tools = [
            {
                "name": "greet",
                "description": "Say hi",
                "command": [
                    sys.executable,
                    "-c",
                    "import sys,json;print('hi:'+json.load(sys.stdin).get('who','?'))",
                ],
                "parameters": {"type": "object", "properties": {"who": {"type": "string"}}},
            }
        ]
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "demo plugin",
                "config_file": "config.json",
                "inject": {"apiKey": "api_key"},
                "tools": tools,
            }
        )
    )
    (plugin_dir / "config.json").write_text(json.dumps({"apiKey": ""}))
    return plugin_dir


def _demo_api_key(plugins_dir: Path) -> str:
    """Read the injected apiKey back from an installed demo plugin."""
    return json.loads((plugins_dir / "demo" / "config.json").read_text())["apiKey"]


def test_plugin_spec_rejects_invalid_manifest(tmp_path: Path):
    """Malformed manifests and unsatisfied inject requirements raise PluginError."""
    for payload in ({"version": "1.0"}, {"name": "n", "version": "1", "inject": {"a": "api_key"}}):
        manifest = tmp_path / f"{payload.get('name', 'bad')}.json"
        manifest.write_text(json.dumps(payload))
        with pytest.raises(PluginError):
            parse_plugin_json(manifest)


def test_plugin_installs_and_refreshes_config(tmp_path: Path):
    """Install injects host credentials, refresh rotates them, remove cleans up."""
    plugins_dir, source = tmp_path / "plugins", _write_plugin(tmp_path / "src")
    spec = install_plugin(
        source=source,
        plugins_dir=plugins_dir,
        host_values={"api_key": "sk-live", "base_url": "https://x.example/v1"},
    )
    assert (
        spec.name == "demo" and spec.runtime is not None and _demo_api_key(plugins_dir) == "sk-live"
    )
    assert [p.name for p in list_plugins(plugins_dir)] == ["demo"]
    (source / "config.json").write_text(json.dumps({"apiKey": ""}))
    with pytest.raises(Exception):
        install_plugin(source=source, plugins_dir=plugins_dir, host_values={})
    refresh_plugin_configs(plugins_dir, {"api_key": "sk-new"})
    assert _demo_api_key(plugins_dir) == "sk-new"
    remove_plugin("demo", plugins_dir)
    assert list_plugins(plugins_dir) == []
    for name in ("demo", "../evil"):
        with pytest.raises(Exception):
            remove_plugin(name, plugins_dir)
    assert collect_host_values({"apiKey": "sk-x", "baseURL": "https://b.example"}) == {
        "api_key": "sk-x",
        "base_url": "https://b.example",
    }


async def test_plugin_tool_executes_command(tmp_path: Path):
    """Installed plugin tools run and return stdout; unknown tools fail."""
    plugins_dir = tmp_path / "plugins"
    shutil.copytree(_write_plugin(tmp_path / "src"), plugins_dir / "demo")
    assert (
        find_plugin_tool("greet", plugins_dir) is not None
        and find_plugin_tool("nope", plugins_dir) is None
    )
    result = await run_plugin_tool("greet", {"who": "ada"}, plugins_dir=plugins_dir)
    assert result.ok and result.output == "hi:ada"
    assert not (await run_plugin_tool("nope", {}, plugins_dir=plugins_dir)).ok


async def test_plugin_tool_times_out(tmp_path: Path):
    """Overrunning plugin commands fail with a timeout error."""
    plugins_dir = tmp_path / "plugins"
    src = _write_plugin(
        tmp_path / "src",
        name="slow",
        tools=[
            {
                "name": "nap",
                "description": "sleep",
                "command": [sys.executable, "-c", "import time;time.sleep(30)"],
            }
        ],
    )
    shutil.copytree(src, plugins_dir / "slow")
    result = await run_plugin_tool("nap", {}, plugins_dir=plugins_dir, timeout_s=0.2)
    assert not result.ok and "timed out" in (result.error or "")


def test_skill_scan_discovers_skill_manifests(tmp_path: Path):
    """Skill directory scans discover manifests and honor the merge flag."""
    skills_root = tmp_path / "skills"
    for name in ("alpha-skill", "beta-skill"):
        d = skills_root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nBody.\n")
    assert {"alpha-skill", "beta-skill"} <= {
        s["name"] for s in list_skills(None, custom_scan_paths=[str(skills_root)])
    }
    filtered = list_skills(
        None,
        enabled_skills={"alpha-skill": True},
        custom_scan_paths=[str(skills_root)],
        merge_all_available_skills=False,
    )
    assert {s["name"] for s in filtered} == {"alpha-skill"}


def test_lsp_framing_parses_concatenated_messages():
    """Concatenated Content-Length frames decode into distinct messages."""
    parser = LspFrameParser()
    m1 = {"jsonrpc": "2.0", "id": 1, "result": {"foo": "bar"}}
    m2 = {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"message": "hi"}}
    parsed = parser.feed(encode_lsp_message(m1) + encode_lsp_message(m2))
    assert len(parsed) == 2 and parsed[0]["id"] == 1
    assert parsed[1]["method"] == "window/logMessage"


def test_lsp_framing_buffers_partial_chunks():
    """Split frames emit nothing until the final chunk completes the message."""
    parser, raw = (
        LspFrameParser(),
        encode_lsp_message({"jsonrpc": "2.0", "id": 42, "result": {"data": [1, 2, 3]}}),
    )
    assert parser.feed(raw[: len(raw) // 2]) == []
    assert parser.feed(raw[len(raw) // 2 :])[0]["result"]["data"] == [1, 2, 3]


def test_lsp_fallback_resolves_python_symbols(tmp_path: Path):
    """The AST fallback answers definition, hover, and symbol queries offline."""
    (tmp_path / "greeter.py").write_text(
        "class Greeter:\n    def greet(self, name: str) -> str:\n"
        '        """Greet a user by name."""\n        return f"Hello {name}"\n'
    )
    client = LspClient(workspace_root=str(tmp_path))
    res_def = client.query("goToDefinition", "greeter.py", 2, 9, project_root=str(tmp_path))
    assert res_def["ok"] is True and res_def["locations"][0]["line"] == 2
    res_hover = client.query("hover", "greeter.py", 2, 9, project_root=str(tmp_path))
    assert res_hover["ok"] is True and "Greet a user by name" in res_hover["hover"]["contents"]
    res_sym = client.query("documentSymbol", "greeter.py", 1, 1, project_root=str(tmp_path))
    assert res_sym["ok"] is True and {"Greeter", "greet"} <= {s["name"] for s in res_sym["symbols"]}


def test_lsp_tool_rejects_invalid_operation(tmp_path: Path):
    """The LSP tool validates the operation name and required file path."""
    context = type("Ctx", (), {"project_root": str(tmp_path)})()
    cases = [
        ({}, "Missing required parameter"),
        ({"operation": "nonExistent", "file_path": "a.py"}, "Invalid operation"),
        ({"operation": "goToDefinition"}, "Missing required parameter `file_path`"),
    ]
    for args, msg in cases:
        res = handle_lsp_tool(args, context)
        assert res.ok is False and msg in res.error


def test_plan_review_extracts_option_list():
    """Single plans yield no options while multi-option plans letter each choice."""
    assert extract_plan_options("# Plan\n\nSteps...") == []
    opts = extract_plan_options(
        "## Option A: Rewrite (Recommended)\n\n## Option B: Patch\n\n## Option C: Defer"
    )
    assert [o["key"] for o in opts] == ["A", "B", "C"]
    assert opts[0]["title"].startswith("Rewrite")


def test_plan_review_resolves_approve_and_revise(monkeypatch):
    """Plan review returns approve, option selection, and trimmed revise feedback."""
    single = "# Plan\nDo things."
    assert prompt_plan_review(None, single, select_fn=lambda c, items, **kw: 0) == {
        "action": "approve"
    }
    multi = "## Option A: X\n## Option B: Y"
    pick_second = lambda c, items, **kw: 1  # noqa: E731
    assert prompt_plan_review(None, multi, select_fn=pick_second) == {
        "action": "option",
        "option": "B",
    }
    monkeypatch.setattr("builtins.input", lambda *a, **k: "  fix naming  ")
    revise = lambda c, items, **kw: 1 if len(items) == 4 else 0  # noqa: E731
    assert prompt_plan_review(None, single, select_fn=revise) == {
        "action": "revise",
        "feedback": "fix naming",
    }

"""Unit and integration tests for Model Context Protocol (MCP) Stdio and SSE transports."""

import http.server
import json
import socketserver
import sys
import threading
import time
import pytest

from coderai.core.mcp.client import McpClient
from coderai.core.mcp.manager import McpManager


class MockSseServerHandler(http.server.BaseHTTPRequestHandler):
    """Mock HTTP SSE server for testing SseMcpTransport."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # Suppress server logging during test

    def do_GET(self):
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # 1. Send endpoint event
            self.wfile.write(b"event: endpoint\ndata: /message\n\n")
            self.wfile.flush()

            # Keep connection open until server stops or client disconnects
            try:
                while getattr(self.server, "running", True):
                    time.sleep(0.05)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/message":
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
            msg_id = req.get("id")
            method = req.get("method")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            # Handle JSON-RPC methods
            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-sse-server", "version": "1.0.0"},
                    },
                }
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo_sse",
                                "description": "Echo back argument over SSE",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }
                        ]
                    },
                }
            elif method == "tools/call":
                args = req.get("params", {}).get("arguments", {})
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Echo: {args.get('text')}"}],
                        "isError": False,
                    },
                }
            elif method == "prompts/list":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}
            elif method == "resources/list":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
            else:
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}

            # Send back response
            if msg_id is not None:
                transport = getattr(self.server, "active_transport", None)
                if transport and transport.on_message:
                    transport.on_message(resp)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.mark.asyncio
async def test_mcp_stdio_transport_lifecycle():
    # Python script acting as a stdio JSON-RPC 2.0 server
    server_script = """
import sys, json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line.strip())
    method = req.get('method')
    msg_id = req.get('id')

    if method == 'initialize':
        res = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}}
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': res}) + '\\n')
        sys.stdout.flush()
    elif method == 'tools/list':
        tools = [{'name': 'stdio_tool', 'description': 'Stdio tool', 'inputSchema': {'type': 'object', 'properties': {'msg': {'type': 'string'}}}}]
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'tools': tools}}) + '\\n')
        sys.stdout.flush()
    elif method == 'tools/call':
        args = req.get('params', {}).get('arguments', {})
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'content': [{'type': 'text', 'text': f"Hello {args.get('msg')}"}]}}) + '\\n')
        sys.stdout.flush()
    elif method == 'prompts/list':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'prompts': []}}) + '\\n')
        sys.stdout.flush()
    elif method == 'resources/list':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'resources': []}}) + '\\n')
        sys.stdout.flush()
    elif msg_id is not None:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {}}) + '\\n')
        sys.stdout.flush()
"""

    client = McpClient("test_stdio", sys.executable, args=["-c", server_script])
    try:
        await client.connect(timeout_s=10.0)
        assert client.is_connected()
        assert len(client._tools) == 1
        assert client._tools[0]["name"] == "stdio_tool"

        # Call tool
        res = await client.call_tool("stdio_tool", {"msg": "World"})
        assert res.get("content", [])[0].get("text") == "Hello World"
    finally:
        await client.disconnect()
        assert not client.is_connected()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.mark.asyncio
async def test_mcp_sse_transport_lifecycle():
    # Start threaded mock SSE HTTP server on localhost
    server = ThreadedHTTPServer(("127.0.0.1", 0), MockSseServerHandler)
    server.running = True
    port = server.server_address[1]

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{port}/sse"

    client = McpClient("test_sse", {"url": url, "allowPrivateIps": True})
    server.active_transport = client.transport

    try:
        try:
            await client.connect(timeout_s=10.0)
        except RuntimeError as e:
            if "Operation not permitted" in str(e) or "PermissionError" in str(e):
                pytest.skip("Local socket connection restricted in sandboxed environment")
            raise
        assert client.is_connected()
        assert len(client._tools) == 1
        assert client._tools[0]["name"] == "echo_sse"

        # Call tool
        res = await client.call_tool("echo_sse", {"text": "SSE Test"})
        assert res.get("content", [])[0].get("text") == "Echo: SSE Test"
    finally:
        await client.disconnect()
        server.running = False
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_mcp_manager_discovery_and_schema_conversion():
    manager = McpManager()

    server_script = """
import sys, json
while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line.strip())
    method = req.get('method')
    msg_id = req.get('id')
    if method == 'initialize':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'protocolVersion': '2024-11-05'}}) + '\\n')
        sys.stdout.flush()
    elif method == 'tools/list':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'tools': [{'name': 'fetch_data', 'description': 'Fetch data', 'inputSchema': {'type': 'object', 'properties': {'k': {'type': 'string'}}}}]}}) + '\\n')
        sys.stdout.flush()
    elif method == 'prompts/list':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'prompts': []}}) + '\\n')
        sys.stdout.flush()
    elif method == 'resources/list':
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {'resources': []}}) + '\\n')
        sys.stdout.flush()
    elif msg_id is not None:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'result': {}}) + '\\n')
        sys.stdout.flush()
"""

    servers = {
        "db_server": {
            "command": sys.executable,
            "args": ["-c", server_script],
        }
    }

    try:
        await manager.initialize(servers)
        tools = manager.list_tools()
        assert len(tools) == 1
        assert tools[0].namespaced_name == "mcp__db_server__fetch_data"

        definitions = manager.get_mcp_tool_definitions()
        assert len(definitions) == 1
        assert definitions[0]["type"] == "function"
        assert definitions[0]["function"]["name"] == "mcp__db_server__fetch_data"
        assert "properties" in definitions[0]["function"]["parameters"]

        statuses = manager.get_status()
        assert any(s.name == "db_server" and s.connected for s in statuses)
    finally:
        await manager.disconnect()

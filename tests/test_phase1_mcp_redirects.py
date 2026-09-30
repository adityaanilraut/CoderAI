"""Offline HTTP transport regressions for redirects and surfaced failures."""

from __future__ import annotations

import asyncio
import io
import json
import threading

import pytest
import requests
from requests.adapters import BaseAdapter

from coderai.mcp.transport import SseMcpTransport, StreamableHttpMcpTransport


PUBLIC_URL = "https://mcp.example.test/mcp"
PRIVATE_URL = "http://127.0.0.1:8888/private"


class RecordingResponse(requests.Response):
    def __init__(self):
        super().__init__()
        self.close_called = False

    def close(self):
        self.close_called = True
        super().close()


class RecordingAdapter(BaseAdapter):
    """Exercise real Requests redirect behavior without opening any sockets."""

    def __init__(self, status: int, *, location: str = PRIVATE_URL) -> None:
        self.status = status
        self.location = location
        self.requests: list[requests.PreparedRequest] = []
        self.completed = threading.Event()
        self.responses: list[requests.Response] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = RecordingResponse()
        response.request = request
        response.url = request.url
        response.status_code = self.status if len(self.requests) == 1 else 200
        response.headers["Content-Type"] = "application/json"
        response.headers["Mcp-Session-Id"] = "response-session"
        if 300 <= response.status_code < 400:
            response.headers["Location"] = self.location
        response._content = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}).encode()
        response.raw = io.BytesIO(response._content)
        self.responses.append(response)
        self.completed.set()
        return response

    def close(self):
        pass


@pytest.fixture
def install_adapter(monkeypatch):
    session_class = requests.Session
    # Only the initial public hostname is resolved; adapters handle all HTTP.
    monkeypatch.setattr(
        "coderai.network.security.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def install(adapter):
        def session():
            client = session_class()
            client.mount("http://", adapter)
            client.mount("https://", adapter)
            return client

        monkeypatch.setattr("coderai.mcp.transport.requests.Session", session)
        return session

    return install


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 500])
async def test_sse_get_rejects_redirects_and_http_errors(status, install_adapter):
    adapter = RecordingAdapter(status)
    install_adapter(adapter)
    transport = SseMcpTransport("test", PUBLIC_URL, headers={"X-Api-Key": "test-key"})

    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        await transport.connect(timeout_s=2)

    assert transport.last_http_status == status
    assert not transport.is_connected()
    assert [request.url for request in adapter.requests] == [PUBLIC_URL]
    assert adapter.responses[0].close_called


@pytest.mark.parametrize("kind", ["sse", "streamable"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 500])
async def test_post_rejects_redirects_and_reports_http_errors(kind, status, install_adapter):
    adapter = RecordingAdapter(status)
    session_factory = install_adapter(adapter)
    received = []
    completed = threading.Event()

    def on_message(message):
        received.append(message)
        completed.set()

    if kind == "sse":
        transport = SseMcpTransport("test", PUBLIC_URL)
        transport._post_endpoint = PUBLIC_URL
        transport._post_session = session_factory()
        transport._post_session.headers.update({"X-Api-Key": "test-key"})
    else:
        transport = StreamableHttpMcpTransport(
            "test", PUBLIC_URL, headers={"X-Api-Key": "test-key"}
        )
        await transport.connect()
        transport.mcp_session_id = "existing-session"
    transport.on_message = on_message

    try:
        transport.send({"jsonrpc": "2.0", "id": 7, "method": "initialize"})
        assert await asyncio.to_thread(completed.wait, 2)
        assert [request.url for request in adapter.requests] == [PUBLIC_URL]
        assert adapter.requests[0].headers["X-Api-Key"] == "test-key"
        assert received[0]["id"] == 7
        assert received[0]["error"]["code"] == status
        assert f"HTTP {status}" in received[0]["error"]["message"]
        assert transport.last_http_status == status
        if status < 400:
            assert "redirects are disabled" in received[0]["error"]["message"]
        if kind == "streamable":
            assert transport.mcp_session_id == "existing-session"
    finally:
        await transport.disconnect()


async def test_streamable_success_delivers_result_and_session_id(install_adapter):
    adapter = RecordingAdapter(200)
    install_adapter(adapter)
    transport = StreamableHttpMcpTransport("test", PUBLIC_URL)
    await transport.connect()
    completed = threading.Event()
    received = []

    def on_message(message):
        received.append(message)
        completed.set()

    transport.on_message = on_message
    try:
        transport.send({"jsonrpc": "2.0", "id": 7, "method": "initialize"})
        assert await asyncio.to_thread(completed.wait, 2)
        assert received == [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}]
        assert transport.mcp_session_id == "response-session"
        assert transport.last_http_status == 200
    finally:
        await transport.disconnect()


@pytest.mark.parametrize("kind", ["sse", "streamable"])
async def test_notification_redirect_surfaces_disconnect(kind, install_adapter):
    adapter = RecordingAdapter(307)
    session_factory = install_adapter(adapter)
    if kind == "sse":
        transport = SseMcpTransport("test", PUBLIC_URL)
        transport._post_endpoint = PUBLIC_URL
        transport._post_session = session_factory()
    else:
        transport = StreamableHttpMcpTransport("test", PUBLIC_URL)
        await transport.connect()
    completed = threading.Event()
    errors = []

    def on_disconnect(reason):
        errors.append(reason)
        completed.set()

    transport.on_disconnect = on_disconnect
    try:
        transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert await asyncio.to_thread(completed.wait, 2)
        assert "HTTP 307" in errors[0]
        assert len(adapter.requests) == 1
    finally:
        await transport.disconnect()

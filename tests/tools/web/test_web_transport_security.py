"""Security regressions for bounded HTTP reads and redirect replay policy."""

import asyncio
from collections import deque

import pytest

from coderAI.tools.web import _http as http_mod
from coderAI.tools.web._http import HttpClient

pytestmark = pytest.mark.security


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        url: str = "https://example.com/final",
        headers: dict[str, str] | None = None,
        content: bytes = b"ok",
    ) -> None:
        self.status = status
        self.url = url
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self.content = _FakeContent(content)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.popleft()


class _LoopSession:
    def __init__(self, **_kwargs) -> None:
        self.loop_id = id(asyncio.get_running_loop())
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def no_rate_limit(monkeypatch):
    async def noop(_hostname):
        return None

    monkeypatch.setattr(http_mod, "_rate_limit_async", noop)


def _client_with_session(monkeypatch, session: _FakeSession) -> HttpClient:
    client = HttpClient()

    async def get_session(_allow_local=False):
        return session

    monkeypatch.setattr(client, "get_session", get_session)
    return client


@pytest.mark.asyncio
async def test_chunked_response_reads_only_max_plus_one(monkeypatch, no_rate_limit):
    response = _FakeResponse(content=b"x" * 100)
    session = _FakeSession([response])
    client = _client_with_session(monkeypatch, session)

    result = await client.safe_request("GET", "https://example.com/data", max_bytes=10)

    assert result is not None
    assert result["oversize"] is True
    assert result["content"] == b"x" * 10
    assert response.content.offset == 11
    assert sum(response.content.read_sizes) == 11


@pytest.mark.asyncio
async def test_sensitive_cross_origin_redirect_is_rejected(monkeypatch, no_rate_limit):
    redirect = _FakeResponse(
        status=302,
        headers={"Location": "https://other.example/final"},
    )
    session = _FakeSession([redirect])
    client = _client_with_session(monkeypatch, session)

    result = await client.safe_request(
        "GET",
        "https://example.com/start",
        headers={"Authorization": "Bearer secret"},
    )

    assert result is None
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_preserved_body_cross_origin_redirect_is_rejected(monkeypatch, no_rate_limit):
    redirect = _FakeResponse(
        status=307,
        headers={"Location": "https://other.example/final"},
    )
    session = _FakeSession([redirect])
    client = _client_with_session(monkeypatch, session)

    result = await client.safe_request(
        "POST",
        "https://example.com/start",
        json_body={"secret": "value"},
    )

    assert result is None
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_303_cross_origin_redirect_drops_body(monkeypatch, no_rate_limit):
    redirect = _FakeResponse(
        status=303,
        headers={"Location": "https://other.example/final"},
    )
    final = _FakeResponse(url="https://other.example/final", content=b"done")
    session = _FakeSession([redirect, final])
    client = _client_with_session(monkeypatch, session)

    result = await client.safe_request(
        "POST",
        "https://example.com/start",
        headers={"Content-Type": "application/json"},
        json_body={"not": "replayed"},
    )

    assert result is not None
    method, _url, kwargs = session.requests[1]
    assert method == "GET"
    assert kwargs["json"] is None
    assert kwargs["data"] is None
    assert "Content-Type" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_default_port_is_same_origin(monkeypatch, no_rate_limit):
    redirect = _FakeResponse(
        status=307,
        headers={"Location": "HTTPS://EXAMPLE.com:443/final"},
    )
    final = _FakeResponse(url="https://example.com/final")
    session = _FakeSession([redirect, final])
    client = _client_with_session(monkeypatch, session)

    result = await client.safe_request(
        "POST",
        "https://example.com/start",
        headers={"Authorization": "Bearer same-origin"},
        body="payload",
    )

    assert result is not None
    assert len(session.requests) == 2
    assert session.requests[1][2]["data"] == "payload"


@pytest.mark.asyncio
async def test_page_cache_keeps_full_content(monkeypatch):
    stored = {}

    async def request(*_args, **_kwargs):
        return {
            "status": 200,
            "url": "https://example.com/page",
            "content_type": "text/plain",
            "text": "abcdefghijklmnop",
            "content": b"abcdefghijklmnop",
        }

    monkeypatch.setattr("coderAI.tools.web._safe_request_cf", request)
    monkeypatch.setattr("coderAI.tools.web._get_cached", lambda _key: stored.get("value"))
    monkeypatch.setattr(
        "coderAI.tools.web._set_cached", lambda _key, value, _ttl: stored.update(value=value)
    )
    client = HttpClient()

    assert await client.fetch_page_text("https://example.com/page", 5) == "abcde"
    assert stored["value"] == "abcdefghijklmnop"
    assert await client.fetch_page_text("https://example.com/page", 10) == "abcdefghij"


@pytest.mark.asyncio
async def test_local_and_public_sessions_track_event_loops_separately(monkeypatch):
    monkeypatch.setattr(http_mod.aiohttp, "ClientSession", _LoopSession)
    monkeypatch.setattr(http_mod.aiohttp, "TCPConnector", lambda **_kwargs: object())
    client = HttpClient()
    client.get_ssl_ctx = lambda: None  # type: ignore[method-assign,return-value]
    first_local = _LoopSession()
    client._allow_local_session = first_local
    client._session_loop_id = id(asyncio.get_running_loop())
    client._allow_local_session_loop_id = -1

    second_local = await client.get_session(allow_local=True)
    assert second_local is not first_local
    assert first_local.closed is True


@pytest.mark.asyncio
async def test_http_client_close_releases_both_session_pools():
    client = HttpClient()
    public = _LoopSession()
    local = _LoopSession()
    client._session = public  # type: ignore[assignment]
    client._allow_local_session = local  # type: ignore[assignment]
    client._session_loop_id = 1
    client._allow_local_session_loop_id = 2

    await client.close()
    await client.close()

    assert public.closed is True
    assert local.closed is True
    assert client._session is None
    assert client._allow_local_session is None
    assert client._session_loop_id is None
    assert client._allow_local_session_loop_id is None

"""Security regressions for bounded HTTP reads and redirect replay policy."""

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

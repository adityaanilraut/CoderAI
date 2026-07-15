"""Phase 6 — network / SSRF / browser hardening regression tests.

Threat model: a *public* URL (or a hostname the model was handed by untrusted
repo/web/MCP content) must never become a lever to reach the cloud metadata
endpoint, loopback, or an RFC1918 host — not on the first request and not via a
redirect hop — and a credentialed request must not replay its secrets to a host
the redirect handed it. The browser layer must resolve-and-validate hostnames
(not just literal IPs) and re-check the final URL after redirects.

* 6.1 literal-IP redirect re-validation — every hop's host is re-checked, and a
      redirect to an internal literal IP is blocked *before* it is fetched.
* 6.4 credentialed cross-origin / https→http redirects are rejected.
* 6.2 browser navigation resolves the hostname and rejects internal-resolving
      hosts; the final ``page.url`` is re-validated after redirects.
* 6.3 ``browser_evaluate`` is reclassified (not read-only, requires confirmation).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import INTERNAL_IP_TARGETS

# ═══════════════════════════════════════════════════════════════════════════
# Fake aiohttp session — drives HttpClient.safe_request's manual redirect loop
# deterministically and records every hop (method, url, headers) so a test can
# prove a blocked target was never requested and that credentials were not replayed.
# ═══════════════════════════════════════════════════════════════════════════


class _FakeResp:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self._offset = 0
        self.content = self
        self.url = "http://fake.invalid/"

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Serves a queued list of ``(status, headers, body)`` responses in order."""

    def __init__(self, responses: List[Tuple[int, Dict[str, str], bytes]]) -> None:
        self._responses = list(responses)
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []

    @property
    def closed(self) -> bool:
        return False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        **_kwargs: Any,
    ) -> _FakeResp:
        self.requests.append((method, url, dict(headers or {})))
        status, hdrs, body = self._responses.pop(0)
        return _FakeResp(status, hdrs, body)


def _redirect(target: str, code: int = 302) -> Tuple[int, Dict[str, str], bytes]:
    return (code, {"Location": target}, b"")


def _ok() -> Tuple[int, Dict[str, str], bytes]:
    return (200, {"Content-Type": "text/plain"}, b"OK")


async def _run_safe_request(
    responses: List[Tuple[int, Dict[str, str], bytes]],
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    allow_local: bool = False,
) -> Tuple[Optional[Dict[str, Any]], _FakeSession]:
    """Drive ``HttpClient.safe_request`` against a fake session; return result + session."""
    from coderAI.tools.web._http import HttpClient

    client = HttpClient()
    fake = _FakeSession(responses)
    with (
        patch.object(client, "get_session", AsyncMock(return_value=fake)),
        patch("coderAI.tools.web._http._rate_limit_async", AsyncMock(return_value=None)),
    ):
        result = await client.safe_request("GET", url, headers=headers, allow_local=allow_local)
    return result, fake


# ═══════════════════════════════════════════════════════════════════════════
# 6.1 — literal-IP redirect re-validation on every hop
# ═══════════════════════════════════════════════════════════════════════════


class TestRedirectLiteralIPRevalidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("target_host", INTERNAL_IP_TARGETS[:4])  # literal IPs only
    async def test_redirect_to_internal_ip_blocked_and_not_fetched(self, target_host: str) -> None:
        """A public URL that 302s to an internal literal IP is blocked, and the
        internal target is never requested."""
        internal_url = f"http://{target_host}/latest/meta-data/"
        # Response #2 must NEVER be served — if the guard fails, the loop would
        # request the internal IP and consume it, which the assertions catch.
        result, fake = await _run_safe_request(
            [_redirect(internal_url), _ok()],
            "https://public.example/start",
        )

        assert result is None  # blocked
        # Exactly one hop was issued (the public start); the internal target was
        # never fetched.
        assert len(fake.requests) == 1
        assert all(target_host not in req_url for _m, req_url, _h in fake.requests)

    @pytest.mark.asyncio
    async def test_multi_hop_public_then_internal_blocked(self) -> None:
        """public → public → internal: the block fires on the *third* hop's
        host, after two legitimate public hops."""
        result, fake = await _run_safe_request(
            [
                _redirect("https://second.example/next"),
                _redirect("http://169.254.169.254/latest/"),
                _ok(),  # must never be served
            ],
            "https://first.example/start",
        )

        assert result is None
        assert len(fake.requests) == 2  # first + second, never the metadata IP
        assert all("169.254.169.254" not in u for _m, u, _h in fake.requests)

    @pytest.mark.asyncio
    async def test_legit_cross_host_public_redirect_still_works(self) -> None:
        """A public→public redirect chain is followed to the 200 (no regression)."""
        result, fake = await _run_safe_request(
            [_redirect("https://cdn.example/final"), _ok()],
            "https://www.example/start",
        )

        assert result is not None
        assert result["status"] == 200
        assert result["text"] == "OK"
        assert len(fake.requests) == 2
        assert fake.requests[1][1] == "https://cdn.example/final"

    @pytest.mark.asyncio
    async def test_initial_literal_internal_ip_blocked(self) -> None:
        """The pre-loop literal-IP guard still blocks a direct internal target."""
        result, fake = await _run_safe_request([_ok()], "http://169.254.169.254/")

        assert result is None
        assert fake.requests == []  # never even issued


# ═══════════════════════════════════════════════════════════════════════════
# 6.4 — reject credentialed cross-origin / downgrade redirects
# ═══════════════════════════════════════════════════════════════════════════

_CREDS = {
    "Authorization": "Bearer secret-token",
    "Cookie": "session=abc",
    "X-Api-Key": "k-123",
    "User-Agent": "coderAI-test",
}


class TestCredentialRedirectRejection:
    @pytest.mark.asyncio
    async def test_cross_host_redirect_with_credentials_is_rejected(self) -> None:
        result, fake = await _run_safe_request(
            [_redirect("https://attacker.example/collect"), _ok()],
            "https://api.trusted.example/start",
            headers=dict(_CREDS),
        )

        assert result is None
        assert len(fake.requests) == 1
        assert fake.requests[0][2].get("Authorization") == "Bearer secret-token"

    @pytest.mark.asyncio
    async def test_same_host_redirect_keeps_credentials(self) -> None:
        result, fake = await _run_safe_request(
            [_redirect("https://api.trusted.example/v2/data"), _ok()],
            "https://api.trusted.example/v1/data",
            headers=dict(_CREDS),
        )

        assert result is not None
        assert fake.requests[1][2].get("Authorization") == "Bearer secret-token"
        assert fake.requests[1][2].get("Cookie") == "session=abc"

    @pytest.mark.asyncio
    async def test_https_to_http_downgrade_with_credentials_is_rejected(self) -> None:
        result, fake = await _run_safe_request(
            [_redirect("http://api.trusted.example/insecure"), _ok()],
            "https://api.trusted.example/secure",
            headers=dict(_CREDS),
        )

        assert result is None
        assert len(fake.requests) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6.1 — end-to-end against the real loopback redirect server (regression: the
# manual redirect loop still follows a legitimate multi-hop chain).
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.enable_socket  # real localhost aiohttp redirect server
async def test_real_server_follows_legit_redirect_chain(ssrf_redirect_server) -> None:
    from coderAI.tools.web._http import HttpClient

    server = ssrf_redirect_server
    # loopback is only reachable with allow_local; this proves the hardened loop
    # still walks a redirect chain end-to-end and returns the final 200 body.
    chain = server.redirect_chain([server.ok_url])
    client = HttpClient()
    result = await client.safe_request("GET", chain, allow_local=True)

    assert result is not None
    assert result["status"] == 200
    assert result["text"] == "OK"
    assert any("/ok" in h for h in server.hits)


# ═══════════════════════════════════════════════════════════════════════════
# 6.2 — browser navigation: resolve-and-pin + post-redirect re-check
# ═══════════════════════════════════════════════════════════════════════════


class TestBrowserNavigationSSRF:
    @pytest.mark.asyncio
    async def test_literal_private_ip_blocked(self) -> None:
        from coderAI.tools.browser import _validate_navigation_url

        err = await _validate_navigation_url("http://169.254.169.254/latest/")
        assert err is not None and "SSRF guard" in err["error"]

    @pytest.mark.asyncio
    async def test_non_http_scheme_blocked(self) -> None:
        from coderAI.tools.browser import _validate_navigation_url

        err = await _validate_navigation_url("file:///etc/passwd")
        assert err is not None and "scheme" in err["error"].lower()

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_internal_is_blocked(self) -> None:
        """A hostname (not a literal IP) that resolves to a private address is
        rejected — the DNS-rebinding defense (6.2)."""
        from coderAI.tools import browser

        with patch.object(browser, "_resolve_host_addrs", AsyncMock(return_value=["10.0.0.5"])):
            err = await browser._validate_navigation_url("http://rebind.evil.example/")
        assert err is not None
        assert "non-public" in err["error"]

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_public_is_allowed(self) -> None:
        from coderAI.tools import browser

        with patch.object(
            browser, "_resolve_host_addrs", AsyncMock(return_value=["93.184.216.34"])
        ):
            err = await browser._validate_navigation_url("http://example.com/")
        assert err is None

    @pytest.mark.asyncio
    async def test_unresolvable_host_blocked(self) -> None:
        from coderAI.tools import browser

        with patch.object(browser, "_resolve_host_addrs", AsyncMock(return_value=[])):
            err = await browser._validate_navigation_url("http://nope.invalid/")
        assert err is not None and "could not resolve" in err["error"]

    @pytest.mark.asyncio
    async def test_navigate_rechecks_final_url_after_redirect(self) -> None:
        """navigate() re-validates the post-redirect page.url and resets the page
        when it resolves to an internal host."""
        from coderAI.tools import browser
        from coderAI.tools.browser import BrowserSession

        session = BrowserSession(agent_id="a")
        session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
        page = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Internal")
        page.url = "http://internal-after-redirect.example/"
        session._page = page

        with patch.object(
            browser, "_resolve_host_addrs", AsyncMock(return_value=["169.254.169.254"])
        ):
            result = await session.navigate("http://looks-fine.example/")

        assert result["success"] is False
        assert "SSRF guard" in result["error"]
        # The page was reset so the internal content isn't left loaded.
        assert any(call.args and call.args[0] == "about:blank" for call in page.goto.call_args_list)

    @pytest.mark.asyncio
    async def test_navigate_succeeds_for_public_final_url(self) -> None:
        from coderAI.tools import browser
        from coderAI.tools.browser import BrowserSession

        session = BrowserSession(agent_id="a")
        session._ensure_browser = AsyncMock()  # type: ignore[method-assign]
        page = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Home")
        page.url = "http://example.com/"
        session._page = page

        with patch.object(
            browser, "_resolve_host_addrs", AsyncMock(return_value=["93.184.216.34"])
        ):
            result = await session.navigate("http://example.com/")

        assert result["success"] is True
        assert result["title"] == "Home"


# ═══════════════════════════════════════════════════════════════════════════
# 6.3 — browser_evaluate reclassified
# ═══════════════════════════════════════════════════════════════════════════


class TestBrowserEvaluateClassification:
    def test_browser_evaluate_is_not_read_only_and_requires_confirmation(self) -> None:
        from coderAI.tools.browser import BrowserEvaluateTool

        tool = BrowserEvaluateTool()
        assert tool.is_read_only is False
        assert tool.requires_confirmation is True

    def test_permission_layer_requires_confirmation(self) -> None:
        from coderAI.core.permissions import tool_requires_confirmation
        from coderAI.tools.browser import BrowserEvaluateTool

        assert tool_requires_confirmation(BrowserEvaluateTool()) is True

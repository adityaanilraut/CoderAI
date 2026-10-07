"""Pooled, bounded HTTP transport with policy checks on every redirect."""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from coderai.network.cache import ResponseCache
    from coderai.network.security import NetworkPolicy

DEFAULT_USER_AGENT = "CoderAI/1.0 (+https://github.com/adityaanilraut/CoderAI; AI Pair Programmer)"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_RETRIES = 2
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass
class HttpResponse:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]
    url: str
    elapsed_ms: float
    ok: bool
    from_cache: bool = False
    error: str | None = None
    fetched_at: float = 0.0
    security_blocked: bool = False


class HttpClient:
    """HTTP client with bounded decoded bodies and anonymous public GET redirects."""

    def __init__(
        self,
        policy: NetworkPolicy | None = None,
        cache: ResponseCache | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        pool_connections: int = 20,
        pool_maxsize: int = 20,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        from coderai.network.cache import get_fetch_cache
        from coderai.network.security import NetworkPolicy

        self.policy = policy or NetworkPolicy()
        self.cache = cache or get_fetch_cache()
        self.user_agent = user_agent
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.max_response_bytes = min(max_response_bytes, MAX_RESPONSE_BYTES)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=Retry(
                total=MAX_RETRIES,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                respect_retry_after_header=False,
                raise_on_status=False,
            ),
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_data: Any = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        use_cache: bool = False,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        from coderai.network.security import (
            NetworkSecurityError,
            check_outbound_url,
            is_same_origin,
        )

        check_outbound_url(url, self.policy)
        req_headers = {"User-Agent": self.user_agent, **(headers or {})}
        cache_key = self.cache._generate_key(
            "http_get",
            {
                "url": url,
                "params": params,
                "headers": req_headers,
                "policy": vars(self.policy),
                "limit": self.max_response_bytes,
            },
        )
        if use_cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, HttpResponse):
                check_outbound_url(cached.url, self.policy)
                cached.from_cache = True
                return cached

        started = time.perf_counter()
        current = url
        response = None

        def failure(message: str) -> HttpResponse:
            return HttpResponse(
                status_code=response.status_code if response is not None else 0,
                text="",
                content=b"",
                headers={},
                url=current,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                ok=False,
                error=message,
            )

        try:
            for _ in range(10):
                check_outbound_url(current, self.policy)
                kwargs: dict[str, Any] = {
                    "headers": req_headers,
                    "timeout": timeout,
                    "allow_redirects": False,
                    "stream": True,
                }
                if method == "GET":
                    response = self._session.get(current, params=params, **kwargs)
                else:
                    response = self._session.post(current, data=data, json=json_data, **kwargs)
                if not (response.is_redirect or response.is_permanent_redirect):
                    break
                location = response.headers.get("Location") or response.headers.get("location")
                if not location:
                    return failure("Redirect response is missing Location")
                next_url = urllib.parse.urljoin(current, location)
                try:
                    check_outbound_url(next_url, self.policy)
                except NetworkSecurityError as exc:
                    blocked = failure(f"Redirect blocked: {exc}")
                    blocked.security_blocked = True
                    return blocked
                same_origin = is_same_origin(current, next_url)
                if (
                    urllib.parse.urlsplit(current).scheme == "https"
                    and urllib.parse.urlsplit(next_url).scheme != "https"
                ):
                    return failure("HTTPS downgrade redirect blocked")
                if not same_origin:
                    if method != "GET":
                        return failure("Redirect to different origin blocked for POST")
                    req_headers = {
                        k: v
                        for k, v in req_headers.items()
                        if k.lower()
                        in {"user-agent", "accept", "accept-language", "accept-encoding"}
                    }
                response.close()
                response = None
                current, params = next_url, None
            else:
                return failure("Redirect limit exceeded")

            assert response is not None
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            declared_size = response_headers.get("content-length", "")
            if declared_size.isdigit() and int(declared_size) > self.max_response_bytes:
                return failure(f"Response exceeds {self.max_response_bytes} byte download limit")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=16_384):
                if len(content) + len(chunk) > self.max_response_bytes:
                    return failure(
                        f"Response exceeds {self.max_response_bytes} byte download limit"
                    )
                content.extend(chunk)
                if time.perf_counter() - started > 60:
                    return failure("Response exceeded total download time limit")
            encoding = response.encoding or "utf-8"
            try:
                decoded = bytes(content).decode(encoding, errors="replace")
            except LookupError:
                decoded = bytes(content).decode("utf-8", errors="replace")
            result = HttpResponse(
                status_code=response.status_code,
                text=decoded,
                content=bytes(content),
                headers=response_headers,
                url=response.url,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                ok=200 <= response.status_code < 300,
                fetched_at=time.time(),
            )
            if use_cache and result.ok:
                self.cache.set(cache_key, result, ttl_seconds=cache_ttl)
            return result
        except requests.exceptions.RequestException as exc:
            return failure(str(exc))
        finally:
            if response is not None:
                response.close()

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        use_cache: bool = True,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        return self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
        )

    def post(
        self,
        url: str,
        data: Any = None,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    ) -> HttpResponse:
        return self._request(
            "POST", url, data=data, json_data=json_data, headers=headers, timeout=timeout
        )

    async def get_async(self, url: str, **kwargs: Any) -> HttpResponse:
        return await asyncio.to_thread(self.get, url, **kwargs)

    async def post_async(self, url: str, **kwargs: Any) -> HttpResponse:
        return await asyncio.to_thread(self.post, url, **kwargs)

    def close(self) -> None:
        self._session.close()


_default_http_client: HttpClient | None = None


def get_http_client() -> HttpClient:
    global _default_http_client
    if _default_http_client is None:
        _default_http_client = HttpClient()
    return _default_http_client


def new_client_session(*, timeout: Any | None = None) -> Any:
    """Native aiohttp transport for asynchronous telemetry callers."""
    import ssl
    import aiohttp
    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=context),
        timeout=timeout or aiohttp.ClientTimeout(total=120, sock_read=60, sock_connect=15),
    )

"""Resilient HTTP Client with connection pooling, retries, SSRF rails, and caching."""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from coderai.core.network.cache import ResponseCache, get_fetch_cache
from coderai.core.network.security import NetworkPolicy, check_outbound_url, is_same_origin

DEFAULT_USER_AGENT = "CoderAI/1.0 (+https://github.com/adityaanilraut/CoderAI; AI Pair Programmer)"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_RETRIES = 3


@dataclass
class HttpResponse:
    """Structured HTTP response data."""

    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]
    url: str
    elapsed_ms: float
    ok: bool
    from_cache: bool = False
    error: str | None = None


class HttpClient:
    """Resilient HTTP client with security enforcement, retry logic, connection pooling, and caching."""

    def __init__(
        self,
        policy: NetworkPolicy | None = None,
        cache: ResponseCache | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        pool_connections: int = 20,
        pool_maxsize: int = 20,
    ) -> None:
        self.policy = policy or NetworkPolicy()
        self.cache = cache or get_fetch_cache()
        self.user_agent = user_agent

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
            max_retries=retry_strategy,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        use_cache: bool = True,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        """Perform a synchronous HTTP GET request with security validation and caching."""
        # 1. Security validation (SSRF & Domain policy)
        check_outbound_url(url, self.policy)

        # 2. Check cache if enabled
        cache_key = ""
        if use_cache:
            cache_key = self.cache._generate_key("http_get", {"url": url, "params": params})
            cached_val = self.cache.get(cache_key)
            if cached_val is not None and isinstance(cached_val, HttpResponse):
                cached_val.from_cache = True
                return cached_val

        start_time = time.perf_counter()
        req_headers = {"User-Agent": self.user_agent}
        if headers:
            req_headers.update(headers)

        try:
            current = url
            resp = None
            for _ in range(10):
                check_outbound_url(current, self.policy)
                resp = self._session.get(
                    current,
                    params=params,
                    headers=req_headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
                if resp.is_redirect or resp.is_permanent_redirect:
                    location = resp.headers.get("Location") or resp.headers.get("location")
                    if not location:
                        break
                    nxt = urllib.parse.urljoin(current, location)
                    if not is_same_origin(current, nxt):
                        return HttpResponse(
                            status_code=resp.status_code,
                            text="",
                            content=b"",
                            headers={k.lower(): v for k, v in resp.headers.items()},
                            url=current,
                            elapsed_ms=(time.perf_counter() - start_time) * 1000.0,
                            ok=False,
                            error=f"Redirect to different origin blocked: {nxt}",
                        )
                    current = nxt
                    params = None
                    continue
                break
            assert resp is not None
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}

            result = HttpResponse(
                status_code=resp.status_code,
                text=resp.text,
                content=resp.content,
                headers=resp_headers,
                url=resp.url,
                elapsed_ms=elapsed_ms,
                ok=resp.ok,
            )

            # Cache successful 200 OK responses
            if use_cache and resp.ok and cache_key:
                self.cache.set(cache_key, result, ttl_seconds=cache_ttl)

            return result

        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return HttpResponse(
                status_code=0,
                text="",
                content=b"",
                headers={},
                url=url,
                elapsed_ms=elapsed_ms,
                ok=False,
                error=str(e),
            )

    def post(
        self,
        url: str,
        data: Any = None,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    ) -> HttpResponse:
        """Perform a synchronous HTTP POST request with security validation."""
        check_outbound_url(url, self.policy)

        start_time = time.perf_counter()
        req_headers = {"User-Agent": self.user_agent}
        if headers:
            req_headers.update(headers)

        try:
            resp = self._session.post(
                url, data=data, json=json_data, headers=req_headers, timeout=timeout
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}

            return HttpResponse(
                status_code=resp.status_code,
                text=resp.text,
                content=resp.content,
                headers=resp_headers,
                url=resp.url,
                elapsed_ms=elapsed_ms,
                ok=resp.ok,
            )
        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return HttpResponse(
                status_code=0,
                text="",
                content=b"",
                headers={},
                url=url,
                elapsed_ms=elapsed_ms,
                ok=False,
                error=str(e),
            )

    async def get_async(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        use_cache: bool = True,
        cache_ttl: float | None = None,
    ) -> HttpResponse:
        """Asynchronous wrapper for HTTP GET."""
        return await asyncio.to_thread(
            self.get,
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
        )

    async def post_async(
        self,
        url: str,
        data: Any = None,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[float, float] | float = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    ) -> HttpResponse:
        """Asynchronous wrapper for HTTP POST."""
        return await asyncio.to_thread(
            self.post,
            url,
            data=data,
            json_data=json_data,
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._session.close()


# Default singleton instance
_default_http_client = HttpClient()


def get_http_client() -> HttpClient:
    return _default_http_client

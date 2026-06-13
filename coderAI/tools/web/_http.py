"""HTTP layer: shared sessions, SSRF protection, redirect-safe requests.

The session pool, SSL context, and SSRF-safe request methods live on the
:class:`HttpClient` class, owned by ``ToolServices`` (``get_services().http``).
The process-wide default container reuses one ``HttpClient``, so the shared
connection pool and the per-event-loop session recreation behave exactly as
they did when these were module globals.

The module-level ``_get_session`` / ``_get_ssl_ctx`` / ``_safe_request`` /
``_safe_request_cf`` / ``_fetch_page_text`` functions are thin wrappers that
delegate to the active container's client, so existing patch points
(``coderAI.tools.web._safe_request``) and the ``_web.<name>()`` late-binding
seams established in Phase 2a are unchanged.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import ssl
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

# Calls to patchable seams (_safe_request, _safe_request_cf, _get_cached,
# _set_cached) go through the package namespace so tests can patch
# coderAI.tools.web.<name> as a single point, exactly as they could when
# everything lived in one module.
import coderAI.tools.web as _web
from coderAI.core.services import get_services
from coderAI.tools.web._cache import _cache_key, _DEFAULT_PAGE_TTL
from coderAI.tools.web._constants import (
    _CF_BLOCK_HEADERS,
    _HEADERS_CHROME,
    _MAX_REDIRECTS,
    _MAX_RESPONSE_BYTES,
    _TRANSPARENT_UA,
)
from coderAI.tools.web._html import (
    _extract_main_content,
    _extract_pdf_text,
    _html_to_text,
    _looks_like_html,
)
from coderAI.tools.web._ratelimit import _rate_limit_async

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# SSRF Protection
# ═══════════════════════════════════════════════════════════════════════════


def _is_ip_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _allow_local(allow_local: bool) -> bool:
    return bool(allow_local) or os.getenv("CODERAI_ALLOW_LOCAL_URLS") == "1"


def _is_cloudflare_block(status: int, headers: Dict[str, str]) -> bool:
    if status not in (403, 429, 503):
        return False
    lower_keys = {k.lower() for k in headers}
    if any(h in lower_keys for h in _CF_BLOCK_HEADERS):
        return True
    server = headers.get("Server") or headers.get("server") or ""
    return "cloudflare" in server.lower()


class _SSRFResolver(aiohttp.abc.AbstractResolver):
    """Resolver that validates DNS resolutions against SSRF allowlist."""

    def __init__(self, allow_local: bool = False):
        self._allow_local = allow_local

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        except Exception as e:
            raise OSError(f"SSRF guard: DNS failed for {host}: {e}") from e

        results: List[Dict[str, Any]] = []
        for fam, _type, _proto, _canon, sockaddr in infos:
            if fam not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip = sockaddr[0]
            if self._allow_local or _is_ip_public(ip):
                results.append(
                    {
                        "hostname": host,
                        "host": ip,
                        "port": port,
                        "family": fam,
                        "proto": 0,
                        "flags": socket.AI_NUMERICHOST,
                    }
                )
        if not results:
            raise OSError(f"SSRF guard blocked {host}: no public addresses allowed")
        return results

    async def close(self) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# HttpClient — shared session pool + SSRF-aware, redirect-safe requests
# ═══════════════════════════════════════════════════════════════════════════


class HttpClient:
    """Owns the aiohttp session pool, SSL context, and SSRF-safe requests.

    One instance is held by ``ToolServices`` (``get_services().http``). The
    sessions are created lazily and recreated when the running event loop
    changes (e.g. across ``asyncio.run`` calls in tests), matching the prior
    module-global behavior.
    """

    def __init__(self) -> None:
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._allow_local_session: Optional[aiohttp.ClientSession] = None
        self._session_loop_id: Optional[int] = None

    def get_ssl_ctx(self) -> ssl.SSLContext:
        if self._ssl_ctx is not None:
            return self._ssl_ctx
        try:
            import certifi

            self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            self._ssl_ctx = ssl.create_default_context()
        return self._ssl_ctx

    async def get_session(self, allow_local: bool = False) -> aiohttp.ClientSession:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        loop_id = id(loop) if loop else None

        if allow_local:
            if (
                self._allow_local_session is None
                or self._allow_local_session.closed
                or self._session_loop_id != loop_id
            ):
                self._allow_local_session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        ssl=self.get_ssl_ctx(),
                        resolver=_SSRFResolver(allow_local=True),
                        limit=100,
                        limit_per_host=20,
                        ttl_dns_cache=300,
                        enable_cleanup_closed=True,
                    ),
                )
                self._session_loop_id = loop_id
            return self._allow_local_session
        else:
            if self._session is None or self._session.closed or self._session_loop_id != loop_id:
                self._session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        ssl=self.get_ssl_ctx(),
                        resolver=_SSRFResolver(allow_local=False),
                        limit=100,
                        limit_per_host=20,
                        ttl_dns_cache=300,
                        enable_cleanup_closed=True,
                    ),
                )
                self._session_loop_id = loop_id
            return self._session

    async def safe_request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        body: Any = None,
        timeout_s: float = 15.0,
        allow_local: bool = False,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> Optional[Dict[str, Any]]:
        """Issue an HTTP request with SSRF protection via a shared session pool.

        Redirects are handled manually so a public→private redirect cannot
        bypass SSRF validation. The shared session provides connection pooling.
        """
        # Resolve effective allow_local (env var or parameter)
        allow_local = _allow_local(allow_local)
        session = await self.get_session(allow_local)
        current = url
        seen_urls: set = set()

        parsed = urlparse(current)
        await _rate_limit_async(parsed.hostname)

        # Pre-validate IP: aiohttp resolver only fires for hostname lookups,
        # not literal IP addresses. Do explicit validation here.
        if parsed.hostname:
            try:
                ip = ipaddress.ip_address(parsed.hostname)
                if not (allow_local or _is_ip_public(str(ip))):
                    logger.warning(f"SSRF guard: blocked direct IP {parsed.hostname}")
                    return None
            except ValueError:
                pass  # not an IP, resolver will handle it

        for _ in range(_MAX_REDIRECTS + 1):
            try:
                async with session.request(
                    method,
                    current,
                    headers=headers or _HEADERS_CHROME,
                    timeout=aiohttp.ClientTimeout(total=timeout_s, connect=10),
                    allow_redirects=False,
                    json=json_body,
                    data=body,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return None
                        next_url = urljoin(current, loc)
                        if next_url in seen_urls:
                            logger.warning(f"SSRF guard: redirect loop at {next_url}")
                            return None
                        seen_urls.add(next_url)
                        if not next_url.startswith(("http://", "https://")):
                            logger.warning(f"SSRF guard: non-http redirect to {next_url}")
                            return None
                        current = next_url
                        continue

                    content_type = resp.headers.get("Content-Type", "")

                    cl_header = resp.headers.get("Content-Length")
                    if cl_header is not None:
                        try:
                            if int(cl_header) > max_bytes:
                                return {
                                    "status": resp.status,
                                    "headers": dict(resp.headers),
                                    "url": str(resp.url),
                                    "content_type": content_type,
                                    "text": "",
                                    "content": b"",
                                    "oversize": True,
                                }
                        except ValueError:
                            pass

                    raw_bytes = await resp.read()
                    oversize = len(raw_bytes) > max_bytes
                    if oversize:
                        raw_bytes = raw_bytes[:max_bytes]
                    try:
                        text = raw_bytes.decode("utf-8", errors="replace")
                    except Exception:
                        # errors="replace" should never raise; guard so a pathological
                        # payload can't kill the request — binary callers use "content".
                        logger.debug("response decode failed; returning empty text", exc_info=True)
                        text = ""
                    return {
                        "status": resp.status,
                        "headers": dict(resp.headers),
                        "url": str(resp.url),
                        "content_type": content_type,
                        "text": text,
                        "content": raw_bytes,
                        "oversize": oversize,
                    }
            except aiohttp.ClientError as e:
                # aiohttp wraps SSRF resolver errors as ClientConnectorError/DNSError;
                # the original OSError is preserved in __cause__
                msg = str(e)
                cause_msg = str(e.__cause__) if e.__cause__ else ""
                if "SSRF guard" in msg or "SSRF guard" in cause_msg:
                    logger.warning(msg)
                    return None
                raise
            except OSError as e:
                # SSRF resolver raises OSError for blocked hosts
                if "SSRF guard" in str(e):
                    logger.warning(str(e))
                    return None
                raise

        logger.warning(f"SSRF guard: exceeded {_MAX_REDIRECTS} redirects from {url}")
        return None

    async def safe_request_cf(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        resp = await _web._safe_request(method, url, headers=headers, **kwargs)
        if resp is None:
            return None
        if not _is_cloudflare_block(resp.get("status", 0), resp.get("headers", {})):
            return resp

        fallback_headers = dict(headers or _HEADERS_CHROME)
        fallback_headers["User-Agent"] = _TRANSPARENT_UA
        logger.info(f"CF block detected for {url}; retrying with transparent UA")
        return await _web._safe_request(method, url, headers=fallback_headers, **kwargs)

    async def fetch_page_text(
        self,
        url: str,
        max_length: int,
        fmt: str = "markdown",
        extract_main: bool = False,
    ) -> Optional[str]:
        cache_key = _cache_key("page", url, fmt, str(extract_main))
        cached = _web._get_cached(cache_key)
        if cached is not None:
            return str(cached)[:max_length]

        resp = await _web._safe_request_cf("GET", url, timeout_s=15.0)
        if resp is None or resp["status"] != 200:
            return None

        raw = resp["text"]
        content_type = resp.get("content_type", "")

        is_pdf = "pdf" in content_type.lower() or url.lower().endswith(".pdf")
        if is_pdf and "pdf" in content_type.lower():
            pdf_text = _extract_pdf_text(resp.get("content", b""))
            if pdf_text:
                text = pdf_text
            else:
                return None
        else:
            if extract_main and _looks_like_html(content_type, raw):
                raw = _extract_main_content(raw)
            text = _html_to_text(raw, fmt)

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[...truncated...]"

        if text and len(text) > 0:
            try:
                ttl = get_services().config.page_cache_ttl_seconds
            except Exception:
                # Config unavailable → default TTL; caching must not break fetches.
                logger.debug("page_cache_ttl config unavailable, using default", exc_info=True)
                ttl = _DEFAULT_PAGE_TTL
            _web._set_cached(cache_key, text, ttl)

        return text if len(text) > 0 else None


# ═══════════════════════════════════════════════════════════════════════════
# Module-level wrappers — thin delegates to the active container's HttpClient.
# Kept so test patch points (coderAI.tools.web._safe_request, ...) and the
# _web.<name>() late-binding seams stay valid and single-point.
# ═══════════════════════════════════════════════════════════════════════════


def _get_ssl_ctx() -> ssl.SSLContext:
    return get_services().http.get_ssl_ctx()


async def _get_session(allow_local: bool = False) -> aiohttp.ClientSession:
    return await get_services().http.get_session(allow_local)


async def _safe_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    body: Any = None,
    timeout_s: float = 15.0,
    allow_local: bool = False,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> Optional[Dict[str, Any]]:
    return await get_services().http.safe_request(
        method,
        url,
        headers=headers,
        json_body=json_body,
        body=body,
        timeout_s=timeout_s,
        allow_local=allow_local,
        max_bytes=max_bytes,
    )


async def _safe_request_cf(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    return await get_services().http.safe_request_cf(method, url, headers=headers, **kwargs)


async def _fetch_page_text(
    url: str,
    max_length: int,
    fmt: str = "markdown",
    extract_main: bool = False,
) -> Optional[str]:
    return await get_services().http.fetch_page_text(
        url, max_length, fmt=fmt, extract_main=extract_main
    )

"""Web and URL tools for search and content fetching."""

import asyncio
import html as html_lib
import logging
import re
import ssl
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
import ipaddress
import socket
import os

import aiohttp
from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_HEADERS_CHROME = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_HEADERS_FIREFOX = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_ssl_ctx: Optional[ssl.SSLContext] = None


def _get_ssl_ctx() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx

    try:
        import certifi
        _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        return _ssl_ctx
    except ImportError:
        pass
    try:
        ctx = ssl.create_default_context()
        _ssl_ctx = ctx
        return _ssl_ctx
    except Exception:
        pass

    logger.warning(
        "Strict SSL checking is enforced. If requests fail, install certifi "
        "(`pip install certifi`) or verify local CA roots."
    )
    ctx = ssl.create_default_context()
    _ssl_ctx = ctx
    return _ssl_ctx


def _connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=_get_ssl_ctx())


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that pins hostnames to pre-validated IPs.

    Mitigates DNS rebinding: hostnames are resolved once at validation time,
    then the socket connects to that exact IP. A second DNS lookup (which
    aiohttp would otherwise do) cannot swap in a private IP mid-request.
    """

    def __init__(self, pinned: Dict[str, str]):
        # host → IPv4 literal. Case-insensitive host keys.
        self._pinned = {h.lower(): ip for h, ip in pinned.items()}

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ):  # type: ignore[override]
        ip = self._pinned.get(host.lower())
        if ip is None:
            raise OSError(f"SSRF guard: host {host!r} not in pinned allowlist")
        return [{
            "hostname": host,
            "host": ip,
            "port": port,
            "family": socket.AF_INET,
            "proto": 0,
            "flags": 0,
        }]

    async def close(self) -> None:  # type: ignore[override]
        return None


def _pinned_connector(host_to_ip: Dict[str, str]) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        ssl=_get_ssl_ctx(),
        resolver=_PinnedResolver(host_to_ip),
    )


def _strip_tags(raw: str) -> str:
    """Strip HTML tags and unescape entities."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _resolve_ddg_url(url: str) -> str:
    """Decode DuckDuckGo redirect URLs (//duckduckgo.com/l/?uddg=…) to the real URL."""
    url = url.replace("&amp;", "&")
    if "duckduckgo.com/l/" in url or "duckduckgo.com/y.js" in url:
        full = url if url.startswith("http") else ("https:" + url)
        qs = parse_qs(urlparse(full).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    if url.startswith("//"):
        return "https:" + url
    return url


# ---------------------------------------------------------------------------
# HTML → readable text
# ---------------------------------------------------------------------------

_STRIP_BLOCKS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "header"}
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SP = re.compile(r"[ \t]{2,}")


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text with structure preservation."""
    for tag in _STRIP_BLOCKS:
        html = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    # Headings → markdown-style markers
    for lvl in range(1, 7):
        html = re.sub(
            rf"<h{lvl}[^>]*>(.*?)</h{lvl}>",
            lambda m, level=lvl: f"\n\n{'#' * level} {_strip_tags(m.group(1))}\n",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # List items
    html = re.sub(
        r"<li[^>]*>(.*?)</li>",
        lambda m: f"\n• {_strip_tags(m.group(1))}",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Links → "text (url)" when the href looks useful
    def _link_repl(m):
        href, text = m.group(1), _strip_tags(m.group(2))
        if href.startswith(("http://", "https://")) and text and text != href:
            return f"{text} ({href})"
        return text

    html = re.sub(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        _link_repl, html, flags=re.DOTALL | re.IGNORECASE,
    )

    # Code blocks
    html = re.sub(
        r"<pre[^>]*>(.*?)</pre>",
        lambda m: f"\n```\n{_strip_tags(m.group(1))}\n```\n",
        html, flags=re.DOTALL | re.IGNORECASE,
    )

    # Block-level breaks
    html = re.sub(
        r"<(?:br|/p|/div|/li|/tr|/blockquote|/section|/article)[^>]*>",
        "\n", html, flags=re.IGNORECASE,
    )
    html = re.sub(
        r"<(?:p|div|tr|blockquote|section|article)\b[^>]*>",
        "\n", html, flags=re.IGNORECASE,
    )
    html = re.sub(r"<(?:td|th)[^>]*>", "\t", html, flags=re.IGNORECASE)

    text = _TAG_RE.sub("", html)
    text = html_lib.unescape(text)
    text = _MULTI_SP.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query string")
    num_results: int = Field(
        5, description="Number of results to return (default 5, max 10)"
    )
    fetch_content: bool = Field(
        False,
        description=(
            "If true, automatically fetch and include the text content of the "
            "top search results (up to 3). Use this when you need to read the "
            "actual page content, not just titles and snippets."
        ),
    )
    max_content_length: int = Field(
        8000,
        description="Max characters of page content per result when fetch_content=true",
    )


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo and optionally read page content."""

    name = "web_search"
    description = (
        "Search the web for information using DuckDuckGo. Returns titles, URLs, "
        "and snippets. Set fetch_content=true to also read the full text of the "
        "top 3 results (saves you from needing separate read_url calls)."
    )
    is_read_only = True
    parameters_model = WebSearchParams

    DDG_URL = "https://html.duckduckgo.com/lite/"

    # ---- public entry point --------------------------------------------------

    async def execute(
        self,
        query: str,
        num_results: int = 5,
        fetch_content: bool = False,
        max_content_length: int = 8000,
    ) -> Dict[str, Any]:
        num_results = max(1, min(num_results, 10))

        try:
            results = await self._search(query, num_results)

            if not results:
                return {
                    "success": True,
                    "results": [],
                    "query": query,
                    "note": (
                        "No results found. Try rephrasing the query or use "
                        "read_url with a known URL."
                    ),
                    "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
                }

            if fetch_content:
                results = await self._fetch_top_results(
                    results, min(3, len(results)), max_content_length
                )

            return {
                "success": True,
                "results": results,
                "query": query,
                "result_count": len(results),
            }

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return {
                "success": False,
                "error": str(e),
                "hint": "Search failed. Try read_url with a direct URL instead.",
                "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
            }

    # ---- DuckDuckGo HTML search with retries ---------------------------------

    async def _search(
        self, query: str, num_results: int
    ) -> List[Dict[str, str]]:
        """Search DDG HTML endpoint with retries and header rotation."""
        header_sets = [_HEADERS_CHROME, _HEADERS_FIREFOX, _HEADERS_CHROME, _HEADERS_FIREFOX]
        last_error = "Unknown error"

        for attempt, headers in enumerate(header_sets):
            try:
                resp = await _safe_request(
                    "POST",
                    self.DDG_URL,
                    headers=headers,
                    body={"q": query},
                    timeout_s=15.0,
                )
                if resp is None or resp["status"] != 200:
                    status = resp['status'] if resp else 'blocked'
                    last_error = f"HTTP {status}"
                    logger.debug(f"DDG attempt {attempt + 1}: {last_error}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                
                html_text = resp["text"]
                results = self._parse_results(html_text, num_results)
                
                if results:
                    return results
                
                # Empty results → try next header set
                last_error = "Empty results returned"
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_error = str(e)
                logger.debug(f"DDG attempt {attempt + 1}: {e}")
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Search failed after {len(header_sets)} attempts. Last error: {last_error}")

    def _parse_results(
        self, html_text: str, max_results: int
    ) -> List[Dict[str, str]]:
        """Parse search results from DuckDuckGo Lite page."""
        results: List[Dict[str, str]] = []
        
        pattern = r"<a[^>]+href=[\'\"]([^\'\"]+)[\'\"][^>]*class=[\'\"]result-link[\'\"][^>]*>(.*?)</a>(.*?)(?:<td class=[\'\"]result-snippet[\'\"]>|<a class=[\'\"]result-snippet)(.*?)(?:</td>|</a>)"
        
        for m in re.finditer(pattern, html_text, re.IGNORECASE | re.DOTALL):
            if len(results) >= max_results:
                break

            raw_url = html_lib.unescape(m.group(1))
            url = _resolve_ddg_url(raw_url)
            title = _strip_tags(m.group(2))
            snippet = _strip_tags(m.group(4))

            if not url.startswith("http"):
                continue

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })

        return results

    # ---- auto-fetch page content for top results -----------------------------

    async def _fetch_top_results(
        self,
        results: List[Dict[str, str]],
        count: int,
        max_length: int,
    ) -> List[Dict[str, str]]:
        """Fetch page content for the top N results in parallel."""

        async def _fetch_one(result: Dict[str, str]) -> Dict[str, str]:
            url = result.get("url", "")
            try:
                text = await _fetch_page_text(url, max_length)
                if text:
                    result["page_content"] = text
                    result["page_content_length"] = len(text)
            except Exception as e:
                logger.debug(f"Auto-fetch failed for {url}: {e}")
                result["page_content_error"] = str(e)
            return result

        fetched = await asyncio.gather(*[_fetch_one(r) for r in results[:count]])
        return list(fetched) + results[count:]


# ---------------------------------------------------------------------------
# Shared page-fetcher (used by both WebSearchTool and ReadURLTool)
# ---------------------------------------------------------------------------


_MAX_REDIRECTS = 5


def _is_ip_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _allow_local(allow_local: bool) -> bool:
    """Per-request allow-local flag, OR'd with the legacy env opt-in."""
    return bool(allow_local) or os.getenv("CODERAI_ALLOW_LOCAL_URLS") == "1"


def _resolve_and_validate(
    url: str, *, allow_local: bool = False
) -> Optional[Dict[str, str]]:
    """Resolve hostname once and validate the IP.

    Returns ``{"host": hostname, "ip": ipv4}`` on success, or ``None`` if the
    URL is malformed or resolves to a blocked range.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return None
        ip_addr = socket.gethostbyname(hostname)
    except Exception as e:
        logger.warning(f"URL resolution failed for {url}: {e}")
        return None

    if not _allow_local(allow_local) and not _is_ip_public(ip_addr):
        logger.warning(
            f"SSRF guard blocked {url}: resolved to non-public {ip_addr}"
        )
        return None
    return {"host": hostname, "ip": ip_addr}


def _is_safe_url(url: str, *, allow_local: bool = False) -> bool:
    """Back-compat boolean gate. Prefer ``_resolve_and_validate``."""
    return _resolve_and_validate(url, allow_local=allow_local) is not None


async def _safe_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    body: Any = None,
    timeout_s: float = 15.0,
    allow_local: bool = False,
) -> Optional[Dict[str, Any]]:
    """Issue an HTTP request with DNS-rebind-resistant redirects.

    Each hop is resolved once, validated against the public-IP allowlist, and
    the connection is pinned to that IP. Redirects are handled manually (up
    to ``_MAX_REDIRECTS``) so a public→private redirect cannot bypass the
    guard. Returns ``{"status", "headers", "url", "content_type", "text",
    "content"}`` or ``None`` on validation failure.
    """
    current = url
    seen_urls: set = set()
    for _ in range(_MAX_REDIRECTS + 1):
        resolved = _resolve_and_validate(current, allow_local=allow_local)
        if resolved is None:
            # ``None`` is reserved for SSRF / validation failures — callers
            # surface this as "blocked". Network errors raise instead.
            return None
        connector = _pinned_connector({resolved["host"]: resolved["ip"]})
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.request(
                method,
                current,
                headers=headers or _HEADERS_CHROME,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
                allow_redirects=False,
                json=json_body,
                data=body,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return None
                    from urllib.parse import urljoin
                    next_url = urljoin(current, loc)
                    if next_url in seen_urls:
                        logger.warning(
                            f"SSRF guard: redirect loop at {next_url}"
                        )
                        return None
                    seen_urls.add(next_url)
                    if not next_url.startswith(("http://", "https://")):
                        logger.warning(
                            f"SSRF guard: non-http redirect to {next_url}"
                        )
                        return None
                    current = next_url
                    continue

                content_type = resp.headers.get("Content-Type", "")
                raw_bytes = await resp.read()
                try:
                    text = raw_bytes.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "url": str(resp.url),
                    "content_type": content_type,
                    "text": text,
                    "content": raw_bytes,
                }

    logger.warning(f"SSRF guard: exceeded {_MAX_REDIRECTS} redirects from {url}")
    return None


async def _fetch_page_text(url: str, max_length: int) -> Optional[str]:
    """Fetch a single URL and return cleaned text, or None on failure."""
    resp = await _safe_request("GET", url, timeout_s=15.0)
    if resp is None or resp["status"] != 200:
        return None
    content_type = resp["content_type"]
    raw = resp["text"]
    if "html" in content_type.lower() or raw.lstrip().startswith("<"):
        text = _html_to_text(raw)
    else:
        text = raw
    if len(text) > max_length:
        text = text[:max_length] + "\n\n[...truncated...]"
    return text if len(text) > 50 else None


# ---------------------------------------------------------------------------
# ReadURLTool
# ---------------------------------------------------------------------------


class ReadURLParams(BaseModel):
    url: str = Field(..., description="URL to fetch and read")
    max_length: int = Field(
        8000,
        description="Maximum characters of page text to return (default 8000)",
    )


class ReadURLTool(Tool):
    """Fetch a web page and return its text content."""

    name = "read_url"
    description = (
        "Fetch a web page URL and return its text content. "
        "Useful for reading documentation, articles, API references, or any web page."
    )
    is_read_only = True
    parameters_model = ReadURLParams

    async def execute(self, url: str, max_length: int = 8000) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            resp = await _safe_request("GET", url, timeout_s=25.0)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout fetching {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch {url}: {e}"}

        if resp is None:
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }
        if resp["status"] != 200:
            return {"success": False, "error": f"HTTP {resp['status']} for {url}"}

        content_type = resp["content_type"]
        raw = resp["text"]
        if "html" in content_type.lower() or raw.lstrip().startswith("<"):
            text = _html_to_text(raw)
        else:
            text = raw

        truncated = False
        if len(text) > max_length:
            text = text[:max_length]
            truncated = True

        return {
            "success": True,
            "url": resp["url"],
            "content": text,
            "length": len(text),
            "truncated": truncated,
        }


# ---------------------------------------------------------------------------
# DownloadFileTool
# ---------------------------------------------------------------------------


class DownloadFileParams(BaseModel):
    url: str = Field(..., description="URL of the file to download")
    destination_path: str = Field(
        ...,
        description=(
            "Absolute path where the file should be saved."
        ),
    )


class DownloadFileTool(Tool):
    """Download a file (binary or text) from a URL to the local filesystem."""

    name = "download_file"
    description = (
        "Download a file (like a ZIP, image, or raw code snippet) from a given URL to a "
        "local destination. Returns the absolute path to the downloaded file."
    )
    is_read_only = False
    parameters_model = DownloadFileParams
    timeout = 300.0

    async def execute(
        self, url: str, destination_path: str
    ) -> Dict[str, Any]:
        from pathlib import Path

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            resp = await _safe_request("GET", url, timeout_s=60.0)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout downloading {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to download {url}: {e}"}

        if resp is None:
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }
        if resp["status"] != 200:
            return {"success": False, "error": f"HTTP {resp['status']} for {url}"}
        content = resp["content"]

        try:
            dest = Path(destination_path).expanduser().resolve()

            # Guard against writing to protected system/home paths
            from .filesystem import _is_path_protected
            if _is_path_protected(dest):
                return {
                    "success": False,
                    "error": f"Refusing to download to protected path: {dest}",
                }

            # Ensure the parent directory exists
            dest.parent.mkdir(parents=True, exist_ok=True)

            def _write_file():
                with open(dest, "wb") as f:
                    f.write(content)

            await asyncio.to_thread(_write_file)

            return {
                "success": True,
                "url": url,
                "destination_path": str(dest),
                "bytes_downloaded": len(content),
            }

        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout downloading {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to download {url}: {e}"}



# ---------------------------------------------------------------------------
# HTTPRequestTool — generic HTTP client for API calls
# ---------------------------------------------------------------------------


class HTTPRequestParams(BaseModel):
    url: str = Field(..., description="Full URL to send the request to")
    method: str = Field(
        "GET",
        description="HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD (default: GET)",
    )
    headers: Optional[Dict[str, str]] = Field(
        None, description="Optional HTTP headers as a key-value mapping"
    )
    json_body: Optional[Dict[str, Any]] = Field(
        None, description="Request body as a JSON object (sets Content-Type: application/json)"
    )
    body: Optional[str] = Field(
        None, description="Raw request body string (used when json_body is not set)"
    )
    timeout: int = Field(30, description="Request timeout in seconds (default: 30)")
    max_response_length: int = Field(
        16000,
        description="Maximum characters of response body to return (default: 16000)",
    )


class HTTPRequestTool(Tool):
    """Make arbitrary HTTP requests (GET, POST, PUT, PATCH, DELETE)."""

    name = "http_request"
    category = "web"
    description = (
        "Send an HTTP request to any URL with custom method, headers, and body. "
        "Use this for REST API calls, webhooks, or any endpoint that needs "
        "authentication headers or a non-GET method. SSRF protection blocks "
        "requests to private/loopback IPs."
    )
    is_read_only = False
    parameters_model = HTTPRequestParams

    async def execute(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        body: Optional[str] = None,
        timeout: int = 30,
        max_response_length: int = 16000,
    ) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        method = method.upper()
        allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
        if method not in allowed_methods:
            return {"success": False, "error": f"Method '{method}' not allowed. Use one of: {allowed_methods}"}

        req_headers = dict(_HEADERS_CHROME)
        if headers:
            req_headers.update(headers)

        try:
            resp = await _safe_request(
                method,
                url,
                headers=req_headers,
                json_body=json_body,
                body=body,
                timeout_s=float(timeout),
            )
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Request timed out after {timeout}s: {url}"}
        except Exception as e:
            return {"success": False, "error": f"Request failed: {e}"}

        if resp is None:
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }

        status = resp["status"]
        resp_headers = resp["headers"]
        content_type = resp["content_type"]
        raw = resp["text"]

        truncated = False
        if len(raw) > max_response_length:
            raw = raw[:max_response_length]
            truncated = True

        import json as _json
        parsed_json = None
        if "json" in content_type.lower():
            try:
                parsed_json = _json.loads(raw)
            except Exception:
                pass

        result: Dict[str, Any] = {
            "success": 200 <= status < 300,
            "status_code": status,
            "url": resp["url"],
            "method": method,
            "headers": resp_headers,
            "body": raw,
            "truncated": truncated,
        }
        if parsed_json is not None:
            result["json"] = parsed_json
        return result

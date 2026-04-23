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
        header_sets = [_HEADERS_CHROME, _HEADERS_FIREFOX]

        for attempt, headers in enumerate(header_sets):
            try:
                async with aiohttp.ClientSession(connector=_connector()) as session:
                    async with session.post(
                        self.DDG_URL,
                        data={"q": query},
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            logger.debug(
                                f"DDG attempt {attempt + 1}: HTTP {resp.status}"
                            )
                            await asyncio.sleep(1)
                            continue
                        html_text = await resp.text()

                results = self._parse_results(html_text, num_results)
                if results:
                    return results
                # Empty results → try next header set
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"DDG attempt {attempt + 1}: {e}")
                await asyncio.sleep(1)

        return []

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


def _is_safe_url(url: str) -> bool:
    """Check if the URL resolves to a public IP, preventing SSRF on local networks."""
    if os.getenv("CODERAI_ALLOW_LOCAL_URLS") == "1":
        return True
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
            
        ip_addr = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_addr)
        
        if (ip.is_loopback or ip.is_private or ip.is_link_local or 
            ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    except Exception as e:
        logger.warning(f"URL resolution failed or blocked for {url}: {e}")
        return False
    return True


async def _fetch_page_text(url: str, max_length: int) -> Optional[str]:
    """Fetch a single URL and return cleaned text, or None on failure."""
    if not _is_safe_url(url):
        logger.warning(f"SSRF Protection blocked request to: {url}")
        return None
    try:
        async with aiohttp.ClientSession(connector=_connector()) as session:
            async with session.get(
                url,
                headers=_HEADERS_CHROME,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return None
                content_type = resp.headers.get("Content-Type", "")
                raw = await resp.text(errors="replace")

        if "html" in content_type.lower() or raw.lstrip().startswith("<"):
            text = _html_to_text(raw)
        else:
            text = raw

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[...truncated...]"
        return text if len(text) > 50 else None
    except Exception:
        return None


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

        if not _is_safe_url(url):
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }

        try:
            async with aiohttp.ClientSession(connector=_connector()) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=25),
                    headers=_HEADERS_CHROME,
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return {
                            "success": False,
                            "error": f"HTTP {response.status} for {url}",
                        }

                    final_url = str(response.url)
                    content_type = response.headers.get("Content-Type", "")
                    raw = await response.text(errors="replace")

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
                "url": final_url,
                "content": text,
                "length": len(text),
                "truncated": truncated,
            }

        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout fetching {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch {url}: {e}"}


# ---------------------------------------------------------------------------
# DownloadFileTool
# ---------------------------------------------------------------------------


class DownloadFileParams(BaseModel):
    url: str = Field(..., description="URL of the file to download")
    destination_path: Optional[str] = Field(
        None,
        description=(
            "Absolute path where the file should be saved. "
            "If omitted, a temporary file in the current working directory will be created."
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

    async def execute(
        self, url: str, destination_path: Optional[str] = None
    ) -> Dict[str, Any]:
        import os
        import time
        from pathlib import Path

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        if not _is_safe_url(url):
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }

        try:
            async with aiohttp.ClientSession(connector=_connector()) as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=60),
                    headers=_HEADERS_CHROME,
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return {
                            "success": False,
                            "error": f"HTTP {response.status} for {url}",
                        }
                    
                    content = await response.read()

            if destination_path:
                dest = Path(destination_path).expanduser().resolve()
            else:
                # generate a reasonable filename based on url or timestamp
                parsed = urlparse(url)
                filename = os.path.basename(parsed.path)
                if not filename:
                    filename = f"download_{int(time.time())}.tmp"
                dest = Path(os.getcwd()) / filename

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

        if not _is_safe_url(url):
            return {
                "success": False,
                "error": f"SSRF Protection triggered. Blocked request to local/internal IP for {url}.",
            }

        method = method.upper()
        allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
        if method not in allowed_methods:
            return {"success": False, "error": f"Method '{method}' not allowed. Use one of: {allowed_methods}"}

        try:
            req_headers = dict(_HEADERS_CHROME)
            if headers:
                req_headers.update(headers)

            async with aiohttp.ClientSession(connector=_connector()) as session:
                request_kwargs: Dict[str, Any] = {
                    "headers": req_headers,
                    "timeout": aiohttp.ClientTimeout(total=timeout),
                    "allow_redirects": True,
                }
                if json_body is not None:
                    request_kwargs["json"] = json_body
                elif body is not None:
                    request_kwargs["data"] = body

                async with session.request(method, url, **request_kwargs) as response:
                    status = response.status
                    resp_headers = dict(response.headers)
                    content_type = resp_headers.get("Content-Type", "")

                    raw = await response.text(errors="replace")

            truncated = False
            if len(raw) > max_response_length:
                raw = raw[:max_response_length]
                truncated = True

            # Try to parse as JSON for convenience
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
                "url": url,
                "method": method,
                "headers": resp_headers,
                "body": raw,
                "truncated": truncated,
            }
            if parsed_json is not None:
                result["json"] = parsed_json
            return result

        except asyncio.TimeoutError:
            return {"success": False, "error": f"Request timed out after {timeout}s: {url}"}
        except Exception as e:
            return {"success": False, "error": f"Request failed: {e}"}

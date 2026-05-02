"""Web and URL tools for search and content fetching."""

import asyncio
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, unquote, urljoin, urlparse, parse_qs

import aiohttp
from pydantic import BaseModel, Field

from .base import Tool

from .filesystem import _enforce_project_scope, _is_path_protected

logger = logging.getLogger(__name__)


# User-Agent strings. Last updated 2025-04.
_HEADERS_CHROME = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_HEADERS_FIREFOX = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) "
        "Gecko/20100101 Firefox/136.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Used as a UA fallback when a CDN bot-detector (e.g. Cloudflare) blocks the
# Chrome UA. Being honest with the UA name is more likely to be allowlisted
# than rotating through more browser strings.
_TRANSPARENT_UA = "coderai/0.2.0"

# Hard cap on response bodies returned through ``_safe_request``. Pages above
# this are reported as oversize without being read into memory in full when a
# Content-Length header is present; otherwise they're truncated post-read.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB

_ssl_ctx: Optional[ssl.SSLContext] = None


def _get_ssl_ctx() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx
    try:
        import certifi
        _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that pins a hostname to a pre-validated IP.

    Mitigates DNS rebinding: the host is resolved once at validation time,
    then the socket connects to that exact IP. A second DNS lookup (which
    aiohttp would otherwise do) cannot swap in a private IP mid-request.
    """

    def __init__(self, host: str, ip: str, family: int):
        self._host = host.lower()
        self._ip = ip
        self._family = family

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ):  # type: ignore[override]
        if host.lower() != self._host:
            raise OSError(f"SSRF guard: host {host!r} not in pinned allowlist")
        return [{
            "hostname": host,
            "host": self._ip,
            "port": port,
            "family": self._family,
            "proto": 0,
            "flags": 0,
        }]

    async def close(self) -> None:  # type: ignore[override]
        return None


def _pinned_connector(host: str, ip: str, family: int) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        ssl=_get_ssl_ctx(),
        resolver=_PinnedResolver(host, ip, family),
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


_STRIP_BLOCKS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "header"}
_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SP = re.compile(r"[ \t]{2,}")


def _strip_blocks(html: str) -> str:
    for tag in _STRIP_BLOCKS:
        html = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown-ish text with structure preservation."""
    html = _strip_blocks(html)

    # Headings → ATX-style markdown
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
        lambda m: f"\n- {_strip_tags(m.group(1))}",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Links → [text](url) when the href looks useful
    def _link_repl(m):
        href, text = m.group(1), _strip_tags(m.group(2))
        if href.startswith(("http://", "https://")) and text and text != href:
            return f"[{text}]({href})"
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
    html = re.sub(
        r"<code[^>]*>(.*?)</code>",
        lambda m: f"`{_strip_tags(m.group(1))}`",
        html, flags=re.DOTALL | re.IGNORECASE,
    )

    # Inline emphasis
    for tag, marker in (("strong", "**"), ("b", "**"), ("em", "*"), ("i", "*")):
        html = re.sub(
            rf"<{tag}[^>]*>(.*?)</{tag}>",
            lambda m, mk=marker: f"{mk}{_strip_tags(m.group(1))}{mk}",
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


def _html_to_plain(html: str) -> str:
    """Strip all markup and return paragraph-spaced plain text."""
    html = _strip_blocks(html)
    # Block-level → newline
    html = re.sub(
        r"<(?:br|/p|/div|/li|/tr|/h[1-6]|/blockquote|/section|/article)[^>]*>",
        "\n", html, flags=re.IGNORECASE,
    )
    text = _TAG_RE.sub("", html)
    text = html_lib.unescape(text)
    text = _MULTI_SP.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def _looks_like_html(content_type: str, raw: str) -> bool:
    return "html" in (content_type or "").lower() or raw.lstrip().startswith("<")


def _convert_content(raw: str, content_type: str, fmt: str) -> str:
    """Render the response body in the requested format."""
    if fmt == "html":
        return raw
    if not _looks_like_html(content_type, raw):
        return raw  # not HTML — return as-is
    if fmt == "text":
        return _html_to_plain(raw)
    # default / markdown
    return _html_to_markdown(raw)


_MAX_REDIRECTS = 5
_CF_BLOCK_HEADERS = ("cf-mitigated", "cf-chl-bypass", "cf-ray")


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
    """Per-request allow-local flag, OR'd with the legacy env opt-in.

    WARNING: Setting ``CODERAI_ALLOW_LOCAL_URLS=1`` globally disables SSRF
    protection for *every* tool in this module. This is a dangerous setting
    that should only be used in isolated development environments.
    """
    return bool(allow_local) or os.getenv("CODERAI_ALLOW_LOCAL_URLS") == "1"


async def _resolve_and_validate(
    url: str, *, allow_local: bool = False
) -> Optional[Dict[str, Any]]:
    """Resolve hostname once and validate the IP.

    Returns ``{"host", "ip", "family"}`` on success, or ``None`` if the URL
    is malformed or every resolved address is in a blocked range. Uses async
    ``getaddrinfo`` so the event loop isn't blocked, and supports IPv6.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return None
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except Exception as e:
        logger.warning(f"URL resolution failed for {url}: {e}")
        return None

    allow = _allow_local(allow_local)
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        ip = sockaddr[0]
        if allow or _is_ip_public(ip):
            return {"host": hostname, "ip": ip, "family": family}

    logger.warning(f"SSRF guard blocked {url}: no public address resolved")
    return None


def _is_cloudflare_block(status: int, headers: Dict[str, str]) -> bool:
    """Detect Cloudflare bot-mitigation responses."""
    if status not in (403, 429, 503):
        return False
    lower_keys = {k.lower() for k in headers}
    if any(h in lower_keys for h in _CF_BLOCK_HEADERS):
        return True
    server = headers.get("Server") or headers.get("server") or ""
    return "cloudflare" in server.lower()


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
    """Issue an HTTP request with DNS-rebind-resistant redirects.

    Each hop is resolved once, validated against the public-IP allowlist, and
    the connection is pinned to that IP. Redirects are handled manually (up
    to ``_MAX_REDIRECTS``) so a public→private redirect cannot bypass the
    guard. Returns ``{"status", "headers", "url", "content_type", "text",
    "content", "oversize"}`` or ``None`` on validation failure.
    """
    current = url
    seen_urls: set = set()
    for _ in range(_MAX_REDIRECTS + 1):
        resolved = await _resolve_and_validate(current, allow_local=allow_local)
        if resolved is None:
            # ``None`` is reserved for SSRF / validation failures — callers
            # surface this as "blocked". Network errors raise instead.
            return None
        connector = _pinned_connector(
            resolved["host"], resolved["ip"], resolved["family"]
        )
        async with aiohttp.ClientSession(connector=connector) as session:
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

                # Pre-flight size check via Content-Length when available.
                cl_header = resp.headers.get("Content-Length")
                if cl_header is not None:
                    try:
                        if int(cl_header) > max_bytes:
                            logger.warning(
                                f"Response oversize: Content-Length={cl_header} > {max_bytes} for {current}"
                            )
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

    logger.warning(f"SSRF guard: exceeded {_MAX_REDIRECTS} redirects from {url}")
    return None


async def _safe_request_cf(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Wrap ``_safe_request`` with a Cloudflare-aware UA fallback.

    On a CF-style block (403/429/503 + cf-* header or `Server: cloudflare`),
    retry once with a transparent ``coderai/<ver>`` UA. CDNs sometimes
    allowlist honest UAs that they otherwise mitigate as bot traffic.
    """
    resp = await _safe_request(method, url, headers=headers, **kwargs)
    if resp is None:
        return None
    if not _is_cloudflare_block(resp.get("status", 0), resp.get("headers", {})):
        return resp

    fallback_headers = dict(headers or _HEADERS_CHROME)
    fallback_headers["User-Agent"] = _TRANSPARENT_UA
    logger.info(f"CF block detected for {url}; retrying with transparent UA")
    return await _safe_request(method, url, headers=fallback_headers, **kwargs)


async def _fetch_page_text(
    url: str, max_length: int, fmt: str = "markdown"
) -> Optional[str]:
    """Fetch a single URL and return cleaned text, or None on failure."""
    resp = await _safe_request_cf("GET", url, timeout_s=15.0)
    if resp is None or resp["status"] != 200:
        return None
    text = _convert_content(resp["text"], resp["content_type"], fmt)
    if len(text) > max_length:
        text = text[:max_length] + "\n\n[...truncated...]"
    # Pages ≤0 chars (i.e. genuinely empty after conversion) are discarded.
    return text if len(text) > 0 else None


@dataclass
class _SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class _SearchBackend:
    name: str = "base"

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        raise NotImplementedError


class _TavilyBackend(_SearchBackend):
    """Tavily AI search — REST API, content snippets included."""

    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        # NOTE: Tavily requires the API key in the POST body; it does not
        # support authorization headers as of 2025-04.
        body: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "max_results": num_results,
            "search_depth": "basic",
        }
        if allowed_domains:
            body["include_domains"] = allowed_domains
        if blocked_domains:
            body["exclude_domains"] = blocked_domains

        resp = await _safe_request(
            "POST", self.ENDPOINT, json_body=body, timeout_s=20.0
        )
        if resp is None:
            raise RuntimeError("Tavily request blocked by SSRF guard")
        if resp["status"] != 200:
            raise RuntimeError(f"Tavily HTTP {resp['status']}")
        try:
            data = json.loads(resp["text"])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Tavily returned non-JSON: {e}") from e
        return [
            _SearchResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            )
            for r in data.get("results", [])
        ]


class _ExaBackend(_SearchBackend):
    """Exa neural search — REST API."""

    name = "exa"
    ENDPOINT = "https://api.exa.ai/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        body: Dict[str, Any] = {
            "query": query,
            "numResults": num_results,
            "type": "auto",
            "contents": {"text": {"maxCharacters": 500}},
        }
        if allowed_domains:
            body["includeDomains"] = allowed_domains
        if blocked_domains:
            body["excludeDomains"] = blocked_domains

        headers = {
            "User-Agent": _TRANSPARENT_UA,
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }
        resp = await _safe_request(
            "POST", self.ENDPOINT, headers=headers, json_body=body, timeout_s=25.0
        )
        if resp is None:
            raise RuntimeError("Exa request blocked by SSRF guard")
        if resp["status"] != 200:
            raise RuntimeError(f"Exa HTTP {resp['status']}")
        try:
            data = json.loads(resp["text"])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Exa returned non-JSON: {e}") from e
        out = []
        for r in data.get("results", []):
            snippet = r.get("text") or r.get("highlights", [""])[0] or ""
            out.append(
                _SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=snippet[:500] if snippet else "",
                )
            )
        return out


class _DDGBackend(_SearchBackend):
    """DuckDuckGo HTML scrape — fallback when no API key is configured."""

    name = "ddg"
    DDG_URL = "https://html.duckduckgo.com/lite/"

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        # DDG-Lite has no native domain filter; we filter client-side after.
        # Ask for headroom so post-filter still has enough hits.
        wanted = num_results
        if allowed_domains or blocked_domains:
            wanted = min(20, num_results * 3)

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
                    status = resp["status"] if resp else "blocked"
                    last_error = f"HTTP {status}"
                    logger.debug(f"DDG attempt {attempt + 1}: {last_error}")
                elif (results := _parse_ddg_results(resp["text"], wanted)):
                    return results
                else:
                    last_error = "Empty results returned"
            except Exception as e:
                last_error = str(e)
                logger.debug(f"DDG attempt {attempt + 1}: {e}")

            if attempt < len(header_sets) - 1:
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"Search failed after {len(header_sets)} attempts. Last error: {last_error}"
        )


def _parse_ddg_results(html_text: str, max_results: int) -> List[_SearchResult]:
    results: List[_SearchResult] = []
    pattern = (
        r"<a[^>]+href=[\'\"]([^\'\"]+)[\'\"][^>]*class=[\'\"]result-link[\'\"][^>]*>(.*?)</a>"
        r"(.*?)(?:<td class=[\'\"]result-snippet[\'\"]>|<a class=[\'\"]result-snippet)(.*?)(?:</td>|</a>)"
    )
    for m in re.finditer(pattern, html_text, re.IGNORECASE | re.DOTALL):
        if len(results) >= max_results:
            break
        raw_url = html_lib.unescape(m.group(1))
        url = _resolve_ddg_url(raw_url)
        title = _strip_tags(m.group(2))
        snippet = _strip_tags(m.group(4))
        if not url.startswith("http"):
            continue
        results.append(_SearchResult(title=title, url=url, snippet=snippet))
    return results


def _select_search_backend() -> _SearchBackend:
    """Pick a search backend from env. Order: explicit override > Tavily > Exa > DDG."""
    explicit = os.getenv("CODERAI_SEARCH_BACKEND", "auto").lower().strip()
    tavily_key = os.getenv("TAVILY_API_KEY")
    exa_key = os.getenv("EXA_API_KEY")

    if explicit == "tavily":
        if tavily_key:
            return _TavilyBackend(tavily_key)
        logger.warning("CODERAI_SEARCH_BACKEND=tavily but TAVILY_API_KEY unset; falling back to DDG")
        return _DDGBackend()
    if explicit == "exa":
        if exa_key:
            return _ExaBackend(exa_key)
        logger.warning("CODERAI_SEARCH_BACKEND=exa but EXA_API_KEY unset; falling back to DDG")
        return _DDGBackend()
    if explicit == "ddg":
        return _DDGBackend()

    # auto
    if tavily_key:
        return _TavilyBackend(tavily_key)
    if exa_key:
        return _ExaBackend(exa_key)
    return _DDGBackend()


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _matches_domain(domain: str, patterns: List[str]) -> bool:
    """Match domain against a list of patterns. Suffix match (sub.example.com matches example.com)."""
    domain = domain.lower()
    for p in patterns:
        p = p.lower().lstrip(".")
        if not p:
            continue
        if domain == p or domain.endswith("." + p):
            return True
    return False


def _filter_by_domain(
    results: List[_SearchResult],
    allowed: Optional[List[str]],
    blocked: Optional[List[str]],
) -> List[_SearchResult]:
    out = []
    for r in results:
        d = _domain_of(r.url)
        if blocked and _matches_domain(d, blocked):
            continue
        if allowed and not _matches_domain(d, allowed):
            continue
        out.append(r)
    return out


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
    allowed_domains: Optional[List[str]] = Field(
        None,
        description=(
            "If set, restrict results to these domains (suffix match — "
            "'example.com' also matches 'docs.example.com')."
        ),
    )
    blocked_domains: Optional[List[str]] = Field(
        None,
        description="If set, drop results whose domain matches any of these (suffix match).",
    )


class WebSearchTool(Tool):
    """Search the web (Tavily / Exa / DuckDuckGo) and optionally read page content."""

    name = "web_search"
    description = (
        "Search the web for information. Returns titles, URLs, and snippets. "
        "Backend is chosen automatically: Tavily if TAVILY_API_KEY is set, "
        "else Exa if EXA_API_KEY is set, else DuckDuckGo HTML scrape. "
        "Override with CODERAI_SEARCH_BACKEND=tavily|exa|ddg. "
        "Set fetch_content=true to also read the full text of the top 3 results. "
        "Use allowed_domains/blocked_domains to constrain results."
    )
    is_read_only = True
    parameters_model = WebSearchParams

    async def execute(
        self,
        query: str,
        num_results: int = 5,
        fetch_content: bool = False,
        max_content_length: int = 8000,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        num_results = max(1, min(num_results, 10))

        backend = _select_search_backend()
        try:
            results = await self._search(
                query,
                num_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                backend=backend,
            )

            if not results:
                return {
                    "success": True,
                    "results": [],
                    "query": query,
                    "backend": backend.name,
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
                "backend": backend.name,
                "result_count": len(results),
            }

        except Exception as e:
            logger.error(f"Web search error ({backend.name}): {e}")
            return {
                "success": False,
                "error": str(e),
                "backend": backend.name,
                "hint": "Search failed. Try read_url with a direct URL instead.",
                "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
            }

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

    async def _search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
        backend: Optional[_SearchBackend] = None,
    ) -> List[Dict[str, str]]:
        """Run the search via the chosen backend, then filter by domain.

        Defense in depth: Tavily/Exa honor include/exclude natively, but the
        client-side filter also catches DDG results and any backend that
        quietly ignores those params.
        """
        backend = backend or _select_search_backend()
        raw = await backend.search(query, num_results, allowed_domains, blocked_domains)
        filtered = _filter_by_domain(raw, allowed_domains, blocked_domains)
        return [r.to_dict() for r in filtered[:num_results]]


class ReadURLParams(BaseModel):
    url: str = Field(..., description="URL to fetch and read")
    max_length: int = Field(
        8000,
        description="Maximum characters of page text to return (default 8000)",
    )
    format: str = Field(
        "markdown",
        description=(
            "Output format: 'markdown' (HTML → markdown-ish, default), "
            "'text' (plain text), or 'html' (raw HTML, no conversion)."
        ),
    )


_VALID_FORMATS = {"markdown", "text", "html"}


class ReadURLTool(Tool):
    """Fetch a web page and return its text content."""

    name = "read_url"
    description = (
        "Fetch a web page URL and return its content. Choose the output "
        "format with format='markdown'|'text'|'html'. Useful for reading "
        "documentation, articles, API references, or any web page. "
        "Responses larger than 5MB are reported as oversize and truncated."
    )
    is_read_only = True
    parameters_model = ReadURLParams

    async def execute(
        self,
        url: str,
        max_length: int = 8000,
        format: str = "markdown",
    ) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        fmt = format.lower().strip()
        if fmt not in _VALID_FORMATS:
            return {
                "success": False,
                "error": f"Invalid format {format!r}. Must be one of: {sorted(_VALID_FORMATS)}",
            }

        try:
            resp = await _safe_request_cf("GET", url, timeout_s=25.0)
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
            return {
                "success": False,
                "error": f"HTTP {resp['status']} for {url}",
                "oversize": resp.get("oversize", False),
            }

        text = _convert_content(resp["text"], resp["content_type"], fmt)

        truncated = False
        if len(text) > max_length:
            text = text[:max_length]
            truncated = True

        return {
            "success": True,
            "url": resp["url"],
            "format": fmt,
            "content": text,
            "length": len(text),
            "truncated": truncated,
            "oversize": resp.get("oversize", False),
        }


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
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            # Allow larger downloads here than the default 5MB ceiling — this
            # tool's contract is "save the file"; ZIPs and binaries can exceed
            # the read-time guardrail.
            resp = await _safe_request_cf(
                "GET", url, timeout_s=60.0, max_bytes=50 * 1024 * 1024
            )
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

            # Guard against writing to protected system/home paths AND keep
            # downloads inside the project root. ``write_file`` enforces both;
            # ``download_file`` previously only checked the protected list,
            # which let a model drop arbitrary bytes into ``~/.ssh`` or
            # similar so long as the path wasn't on the protected list.
            if _is_path_protected(dest):
                return {
                    "success": False,
                    "error": f"Refusing to download to protected path: {dest}",
                }
            scope_err = _enforce_project_scope(dest, "download_file")
            if scope_err is not None:
                return scope_err

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
            resp = await _safe_request_cf(
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

        parsed_json = None
        if "json" in content_type.lower():
            try:
                parsed_json = json.loads(raw)
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
            "oversize": resp.get("oversize", False),
        }
        if parsed_json is not None:
            result["json"] = parsed_json
        return result

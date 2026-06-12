"""Web and URL tools for search, content fetching, feeds, and sitemaps."""

import asyncio
import hashlib
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse

import aiohttp
from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.tools.filesystem import _enforce_project_scope, _is_path_protected

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

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
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_TRANSPARENT_UA = "coderai/0.3.0"

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
_MAX_REDIRECTS = 5
_CF_BLOCK_HEADERS = ("cf-mitigated", "cf-chl-bypass", "cf-ray")

_SEARXNG_INSTANCES = [
    "https://search.sapti.me",
    "https://searx.tiekoetter.com",
    "https://searx.be",
    "https://search.privacyguides.net",
    "https://search.hbubli.cc",
    "https://paulgo.io",
]

_VALID_FORMATS = {"markdown", "text", "html"}


# ═══════════════════════════════════════════════════════════════════════════
# HTML2Text — shared instance for HTML→Markdown conversion
# ═══════════════════════════════════════════════════════════════════════════

_h2t = None


def _get_h2t():
    global _h2t
    if _h2t is None:
        import html2text

        _h2t = html2text.HTML2Text()
        _h2t.body_width = 0  # don't wrap
        _h2t.ignore_links = False
        _h2t.ignore_images = True
        _h2t.ignore_emphasis = False
        _h2t.ignore_tables = False
        _h2t.protect_links = False
        _h2t.unicode_snob = True
        _h2t.skip_internal_links = True
        _h2t.single_line_break = False
        _h2t.mark_code = True
        _h2t.wrap_links = False
    return _h2t


# ═══════════════════════════════════════════════════════════════════════════
# Precompiled Regex Patterns
# ═══════════════════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SP = re.compile(r"[ \t]{2,}")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_STRIP_BLOCK_RE = re.compile(
    r"<(?:script|style|noscript|svg|head|nav|footer|header)[^>]*>.*?</(?:script|style|noscript|svg|head|nav|footer|header)>",
    re.DOTALL | re.IGNORECASE,
)

_META_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:(\w+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_NAME_RE = re.compile(
    r'<meta[^>]+name=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_TWITTER_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:(\w+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.DOTALL)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.DOTALL
)

# DDG result parsing — precompiled
_DDG_RESULT_RE = re.compile(
    r"<a[^>]+href=[\'\"]([^\'\"]+)[\'\"][^>]*class=[\'\"]result-link[\'\"][^>]*>(.*?)</a>"
    r"(.*?)(?:<td class=[\'\"]result-snippet[\'\"]>"
    r"|<a class=[\'\"]result-snippet)(.*?)(?:</td>|</a>)",
    re.IGNORECASE | re.DOTALL,
)

# SearXNG result parsing — precompiled
_SEARXNG_BLOCK_RE = re.compile(
    r"<article[^>]+class=[\"']result[^\"']*[\"'][^>]*>(.*?)</article>",
    re.DOTALL | re.IGNORECASE,
)
_SEARXNG_LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SEARXNG_SNIPPET_RE = re.compile(
    r'<p[^>]+class=["\'](?:result-content|content|snippet)[^"\']*["\'][^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
_SEARXNG_ANY_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)

# Readability extraction — regex-based replacement for HTMLParser scorer
_READABILITY_ARTICLE_RE = re.compile(
    r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>", re.DOTALL | re.IGNORECASE
)
_READABILITY_SECTION_RE = re.compile(
    r"<(?:section|div)[^>]*>(.*?)</(?:section|div)>", re.DOTALL | re.IGNORECASE
)
_READABILITY_NAV_FOOTER_RE = re.compile(
    r"<(?:nav|footer|header|aside)[^>]*>.*?</(?:nav|footer|header|aside)>",
    re.DOTALL | re.IGNORECASE,
)
_LINK_DENSITY_RE = re.compile(r"<a[^>]*>.*?</a>", re.DOTALL | re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════
# Caching Layer
# ═══════════════════════════════════════════════════════════════════════════

_CACHE_DIR = Path.home() / ".coderAI" / "cache"
_DEFAULT_SEARCH_TTL = 300
_DEFAULT_PAGE_TTL = 3600


def _cache_dir() -> Path:
    # 0700: cached search queries / page content can be sensitive
    _CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _CACHE_DIR


def _cache_key(prefix: str, *parts: str) -> str:
    raw = "|".join((prefix,) + parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _get_cached(key: str) -> Optional[Any]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("expires", 0) < time.time():
            path.unlink(missing_ok=True)
            return None
        return data.get("value")
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return None


def _set_cached(key: str, value: Any, ttl: int) -> None:
    try:
        data = {
            "value": value,
            "expires": time.time() + ttl,
            "cached_at": time.time(),
        }
        path = _cache_path(key)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as e:
        logger.debug(f"Cache write failed for {key}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════════════

_last_request: Dict[str, float] = {}
_rate_limit_delay: float = 1.0


def _get_rate_limit_delay() -> float:
    global _rate_limit_delay
    try:
        from coderAI.system.config import config_manager

        _rate_limit_delay = config_manager.load().rate_limit_delay_seconds
    except Exception:
        env_val = os.getenv("CODERAI_RATE_LIMIT_DELAY")
        if env_val:
            try:
                _rate_limit_delay = float(env_val)
            except ValueError:
                pass
    return _rate_limit_delay


async def _rate_limit_async(hostname: Optional[str]) -> None:
    if not hostname:
        return
    delay = _get_rate_limit_delay()
    if delay <= 0:
        return
    domain = hostname.lower()
    now = time.monotonic()
    last = _last_request.get(domain, 0)
    wait = delay - (now - last)
    if wait > 0:
        logger.debug(f"Rate limiting {domain}: waiting {wait:.2f}s")
        await asyncio.sleep(wait)
    _last_request[domain] = time.monotonic()


# ═══════════════════════════════════════════════════════════════════════════
# Shared aiohttp Session (connection pooling + SSRF-aware resolver)
# ═══════════════════════════════════════════════════════════════════════════

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


class _SSRFResolver(aiohttp.abc.AbstractResolver):
    """Resolver that validates DNS resolutions against SSRF allowlist."""

    def __init__(self, allow_local: bool = False):
        self._allow_local = allow_local

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        except Exception as e:
            raise OSError(f"SSRF guard: DNS failed for {host}: {e}")

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


_session: Optional[aiohttp.ClientSession] = None
_allow_local_session: Optional[aiohttp.ClientSession] = None
_session_loop_id: Optional[int] = None


async def _get_session(allow_local: bool = False) -> aiohttp.ClientSession:
    global _session, _allow_local_session, _session_loop_id
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    loop_id = id(loop) if loop else None

    if allow_local:
        if (
            _allow_local_session is None
            or _allow_local_session.closed
            or _session_loop_id != loop_id
        ):
            _allow_local_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    ssl=_get_ssl_ctx(),
                    resolver=_SSRFResolver(allow_local=True),
                    limit=100,
                    limit_per_host=20,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                ),
            )
            _session_loop_id = loop_id
        return _allow_local_session
    else:
        if _session is None or _session.closed or _session_loop_id != loop_id:
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    ssl=_get_ssl_ctx(),
                    resolver=_SSRFResolver(allow_local=False),
                    limit=100,
                    limit_per_host=20,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                ),
            )
            _session_loop_id = loop_id
        return _session


# ═══════════════════════════════════════════════════════════════════════════
# HTML Utilities
# ═══════════════════════════════════════════════════════════════════════════


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _resolve_ddg_url(url: str) -> str:
    url = url.replace("&amp;", "&")
    if "duckduckgo.com/l/" in url or "duckduckgo.com/y.js" in url:
        from urllib.parse import parse_qs

        full = url if url.startswith("http") else ("https:" + url)
        qs = parse_qs(urlparse(full).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    if url.startswith("//"):
        return "https:" + url
    return url


def _looks_like_html(content_type: str, raw: str) -> bool:
    return "html" in (content_type or "").lower() or raw.lstrip().startswith("<")


def _html_to_text(raw_html: str, fmt: str) -> str:
    """Convert HTML to the requested format using html2text or plain-text stripping."""
    if fmt == "html":
        return raw_html
    if not _looks_like_html("text/html", raw_html):
        return raw_html
    if fmt == "text":
        text = _STRIP_BLOCK_RE.sub("", raw_html)
        text = _COMMENT_RE.sub("", text)
        text = _TAG_RE.sub(" ", text)
        text = html_lib.unescape(text)
        text = _MULTI_SP.sub(" ", text)
        text = _MULTI_NL.sub("\n\n", text)
        return text.strip()
    # markdown (default)
    return str(_get_h2t().handle(raw_html).strip())


# ═══════════════════════════════════════════════════════════════════════════
# Content Extraction — Readability (regex-based, ~10x faster than HTMLParser)
# ═══════════════════════════════════════════════════════════════════════════


def _extract_main_content(html: str) -> str:
    """Extract the main content block from a web page using regex heuristics."""
    # Strip non-content blocks
    html = _STRIP_BLOCK_RE.sub("", html)
    html = _COMMENT_RE.sub("", html)

    # Try <article> or <main> first — highest semantic weight
    m = _READABILITY_ARTICLE_RE.search(html)
    if m:
        return m.group(1)

    # Split into large blocks, score by text density
    blocks = _READABILITY_SECTION_RE.findall(html)
    if not blocks:
        return html

    best_text = ""
    best_score = 0.0

    for block in blocks:
        text = _TAG_RE.sub(" ", block)
        text = html_lib.unescape(text)
        text = _MULTI_SP.sub(" ", text).strip()
        if len(text) < 50:
            continue

        link_text = " ".join(_LINK_DENSITY_RE.findall(block))
        link_text = _TAG_RE.sub("", link_text).strip()

        text_len = len(text)
        link_ratio = len(link_text) / text_len if text_len > 0 else 1.0

        # Score: prefer long text blocks with low link density
        score = min(text_len / 80, 10.0)
        if link_ratio > 0.5:
            score *= 0.2
        elif link_ratio > 0.3:
            score *= 0.5

        if score > best_score:
            best_score = score
            best_text = text

    return best_text if best_text else html


# ═══════════════════════════════════════════════════════════════════════════
# Metadata Extraction
# ═══════════════════════════════════════════════════════════════════════════


def _extract_metadata(html: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    m = _TITLE_RE.search(html)
    if m:
        meta["title"] = _strip_tags(m.group(1))
    for m in _META_OG_RE.finditer(html):
        meta[f"og:{m.group(1)}"] = m.group(2)
    for m in _META_TWITTER_RE.finditer(html):
        meta[f"twitter:{m.group(1)}"] = m.group(2)
    for m in _META_NAME_RE.finditer(html):
        name = m.group(1).lower()
        if name not in meta:
            meta[name] = m.group(2)
    for m in _JSONLD_RE.findall(html):
        try:
            ld = json.loads(m)
            if isinstance(ld, dict):
                meta["jsonld_type"] = ld.get("@type", "")
                if "name" in ld:
                    meta.setdefault("title", str(ld["name"]))
                if "description" in ld:
                    meta.setdefault("description", str(ld["description"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# PDF Text Extraction
# ═══════════════════════════════════════════════════════════════════════════


def _extract_pdf_text(content: bytes) -> Optional[str]:
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        pages: List[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages) if pages else None
    except ImportError:
        logger.debug("pypdf not installed; install with: pip install pypdf")
        return None
    except Exception as e:
        logger.debug(f"PDF extraction failed: {e}")
        return None


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


# ═══════════════════════════════════════════════════════════════════════════
# HTTP Request (shared session, SSRF via resolver, redirect-safe)
# ═══════════════════════════════════════════════════════════════════════════


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
    """Issue an HTTP request with SSRF protection via a shared session pool.

    Redirects are handled manually so a public→private redirect cannot
    bypass SSRF validation. The shared session provides connection pooling.
    """
    # Resolve effective allow_local (env var or parameter)
    allow_local = _allow_local(allow_local)
    session = await _get_session(allow_local)
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


async def _safe_request_cf(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    resp = await _safe_request(method, url, headers=headers, **kwargs)
    if resp is None:
        return None
    if not _is_cloudflare_block(resp.get("status", 0), resp.get("headers", {})):
        return resp

    fallback_headers = dict(headers or _HEADERS_CHROME)
    fallback_headers["User-Agent"] = _TRANSPARENT_UA
    logger.info(f"CF block detected for {url}; retrying with transparent UA")
    return await _safe_request(method, url, headers=fallback_headers, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# Fetch Page Content
# ═══════════════════════════════════════════════════════════════════════════


async def _fetch_page_text(
    url: str,
    max_length: int,
    fmt: str = "markdown",
    extract_main: bool = False,
) -> Optional[str]:
    cache_key = _cache_key("page", url, fmt, str(extract_main))
    cached = _get_cached(cache_key)
    if cached is not None:
        return str(cached)[:max_length]

    resp = await _safe_request_cf("GET", url, timeout_s=15.0)
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
            from coderAI.system.config import config_manager

            ttl = config_manager.load().page_cache_ttl_seconds
        except Exception:
            ttl = _DEFAULT_PAGE_TTL
        _set_cached(cache_key, text, ttl)

    return text if len(text) > 0 else None


# ═══════════════════════════════════════════════════════════════════════════
# Search Result Dataclass
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


# ═══════════════════════════════════════════════════════════════════════════
# Search Backend Base
# ═══════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════
# Tavily Backend
# ═══════════════════════════════════════════════════════════════════════════


class _TavilyBackend(_SearchBackend):
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

        resp = await _safe_request("POST", self.ENDPOINT, json_body=body, timeout_s=20.0)
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


# ═══════════════════════════════════════════════════════════════════════════
# Exa Backend
# ═══════════════════════════════════════════════════════════════════════════


class _ExaBackend(_SearchBackend):
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


# ═══════════════════════════════════════════════════════════════════════════
# DuckDuckGo Backend (reduced retries: 4 → 2, exponential backoff stays)
# ═══════════════════════════════════════════════════════════════════════════


class _DDGBackend(_SearchBackend):
    name = "ddg"
    DDG_URL = "https://html.duckduckgo.com/lite/"

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        wanted = num_results
        if allowed_domains or blocked_domains:
            wanted = min(20, num_results * 3)

        header_sets = [_HEADERS_CHROME, _HEADERS_FIREFOX]
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
                if resp is None or (resp["status"] != 200 and resp["status"] != 303):
                    status = resp["status"] if resp else "blocked"
                    last_error = f"HTTP {status}"
                    logger.debug(f"DDG attempt {attempt + 1}: {last_error}")
                elif results := _parse_ddg_results(resp["text"], wanted):
                    return results
                elif results := _parse_ddg_results_v2(resp["text"], wanted):
                    return results
                else:
                    last_error = "Empty results returned"
            except Exception as e:
                last_error = str(e)
                logger.debug(f"DDG attempt {attempt + 1}: {e}")

            if attempt < len(header_sets) - 1:
                await asyncio.sleep(2**attempt)

        raise RuntimeError(
            f"Search failed after {len(header_sets)} attempts. Last error: {last_error}"
        )


def _parse_ddg_results(html_text: str, max_results: int) -> List[_SearchResult]:
    results: List[_SearchResult] = []
    for m in _DDG_RESULT_RE.finditer(html_text):
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


def _parse_ddg_results_v2(html_text: str, max_results: int) -> List[_SearchResult]:
    """Fallback parser for DDG results when regex pattern fails."""
    from html.parser import HTMLParser

    class _DDGParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.results: List[_SearchResult] = []
            self._in_link = False
            self._in_snippet = False
            self._current_url: Optional[str] = None
            self._current_title: Optional[str] = None
            self._current_snippet: List[str] = []
            self._row_urls: List[str] = []

        def handle_starttag(self, tag, attrs):
            attrs_d = dict(attrs)
            if tag == "a":
                href = attrs_d.get("href", "")
                cls = attrs_d.get("class", "")
                if "result-link" in cls and href:
                    self._in_link = True
                    self._current_url = href
                    self._current_title = None
                elif "result-snippet" in cls:
                    self._in_snippet = True
                    self._current_snippet = []
                elif href.startswith("http"):
                    self._row_urls.append(href)
            if tag == "td" and "result-snippet" in attrs_d.get("class", ""):
                self._in_snippet = True
                self._current_snippet = []

        def handle_endtag(self, tag):
            if tag == "a" and self._in_link:
                self._in_link = False
            if tag in ("td", "a") and self._in_snippet:
                self._in_snippet = False
                snippet = _strip_tags(" ".join(self._current_snippet))
                if self._current_url and snippet:
                    url = _resolve_ddg_url(html_lib.unescape(self._current_url))
                    if url.startswith("http"):
                        self.results.append(
                            _SearchResult(
                                title=self._current_title or "",
                                url=url,
                                snippet=snippet,
                            )
                        )
                    self._current_url = None
                    self._current_title = None

        def handle_data(self, data):
            stripped = data.strip()
            if not stripped:
                return
            if self._in_link and self._current_title is None:
                self._current_title = stripped
            elif self._in_link:
                self._current_title = (self._current_title or "") + " " + stripped
            if self._in_snippet:
                self._current_snippet.append(stripped)

    parser = _DDGParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        logger.debug("DDG v2 parse error", exc_info=True)
        pass

    results: List[_SearchResult] = []
    for r in parser.results:
        if len(results) >= max_results:
            break
        if r.url.startswith("http"):
            results.append(r)

    if not results and parser._row_urls:
        for url in parser._row_urls:
            if len(results) >= max_results:
                break
            real_url = _resolve_ddg_url(url)
            if real_url.startswith("http"):
                results.append(_SearchResult(title=real_url, url=real_url, snippet=""))

    return results


# ═══════════════════════════════════════════════════════════════════════════
# SearXNG Backend (concurrent instance probing)
# ═══════════════════════════════════════════════════════════════════════════


class _SearXNGBackend(_SearchBackend):
    name = "searxng"

    async def search(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[_SearchResult]:
        async def _try_instance(instance: str) -> Optional[List[_SearchResult]]:
            try:
                search_url = (
                    f"{instance}/search?q={quote_plus(query)}&format=html&categories=general"
                )
                resp = await _safe_request(
                    "GET",
                    search_url,
                    headers=_HEADERS_CHROME,
                    timeout_s=15.0,
                )
                if resp is None or resp["status"] != 200:
                    logger.debug(
                        f"SearXNG {instance}: HTTP {resp['status'] if resp else 'blocked'}"
                    )
                    return None

                results = _parse_searxng_results(resp["text"], num_results)
                if results:
                    logger.info(f"SearXNG {instance}: {len(results)} results")
                return results
            except Exception as e:
                logger.debug(f"SearXNG {instance}: {e}")
                return None

        # Try all instances concurrently
        gathered = await asyncio.gather(*[_try_instance(inst) for inst in _SEARXNG_INSTANCES])
        for batch in gathered:
            if batch is not None and len(batch) > 0:
                return batch

        raise RuntimeError("All SearXNG instances failed")


def _parse_searxng_results(html_text: str, max_results: int) -> List[_SearchResult]:
    results: List[_SearchResult] = []
    for block_m in _SEARXNG_BLOCK_RE.finditer(html_text):
        if len(results) >= max_results:
            break
        block = block_m.group(1)

        link_m = _SEARXNG_LINK_RE.search(block)
        if not link_m:
            continue
        url = html_lib.unescape(link_m.group(1))
        title = _strip_tags(link_m.group(2))
        if not url.startswith("http"):
            continue

        snippet = ""
        sm = _SEARXNG_SNIPPET_RE.search(block)
        if sm:
            snippet = _strip_tags(sm.group(1))
        else:
            for pm in _SEARXNG_ANY_P_RE.finditer(block):
                candidate = _strip_tags(pm.group(1))
                if len(candidate) > len(snippet):
                    snippet = candidate

        results.append(_SearchResult(title=title, url=url, snippet=snippet))
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Backend Selection
# ═══════════════════════════════════════════════════════════════════════════


def _concurrent_search_enabled() -> bool:
    try:
        from coderAI.system.config import config_manager

        return bool(config_manager.load().concurrent_search)
    except Exception:
        return os.getenv("CODERAI_CONCURRENT_SEARCH", "true").strip().lower() in (
            "true",
            "1",
            "yes",
            "on",
        )


def _select_search_backend() -> _SearchBackend:
    from coderAI.system.config import config_manager

    cfg = config_manager.load()

    explicit = os.getenv("CODERAI_SEARCH_BACKEND")
    if not explicit and cfg.search_backend:
        explicit = cfg.search_backend
    if not explicit:
        explicit = "auto"
    explicit = explicit.lower().strip()

    tavily_key = os.getenv("TAVILY_API_KEY") or cfg.tavily_api_key
    exa_key = os.getenv("EXA_API_KEY") or cfg.exa_api_key

    if explicit == "tavily":
        if tavily_key:
            return _TavilyBackend(tavily_key)
        logger.warning(
            "CODERAI_SEARCH_BACKEND=tavily but TAVILY_API_KEY unset; falling back to DDG"
        )
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


def _select_free_backends() -> List[_SearchBackend]:
    return [_DDGBackend(), _SearXNGBackend()]


# ═══════════════════════════════════════════════════════════════════════════
# Domain Filtering
# ═══════════════════════════════════════════════════════════════════════════


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _matches_domain(domain: str, patterns: List[str]) -> bool:
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


# ═══════════════════════════════════════════════════════════════════════════
# WebSearchTool
# ═══════════════════════════════════════════════════════════════════════════


class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query string")
    num_results: int = Field(5, description="Number of results to return (default 5, max 10)")
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
    extract_main_content: bool = Field(
        False,
        description="When fetch_content=true, extract main article content (readability).",
    )


class WebSearchTool(Tool):
    """Search the web (Tavily / Exa / DDG / SearXNG) and optionally read page content."""

    name = "web_search"
    description = (
        "Search the web for information. Returns titles, URLs, and snippets. "
        "Backend is chosen automatically: Tavily if TAVILY_API_KEY is set, "
        "else Exa if EXA_API_KEY is set, else DuckDuckGo HTML scrape + SearXNG "
        "public instances (run in parallel for better results). "
        "Override with CODERAI_SEARCH_BACKEND=tavily|exa|ddg|searxng. "
        "Set fetch_content=true to also read the full text of the top 3 results. "
        "Set extract_main_content=true to get only the article text (not nav/ads). "
        "Use allowed_domains/blocked_domains to constrain results."
    )
    is_read_only = True
    parameters_model = WebSearchParams

    async def execute(  # type: ignore[override]
        self,
        query: str,
        num_results: int = 5,
        fetch_content: bool = False,
        max_content_length: int = 8000,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
        extract_main_content: bool = False,
    ) -> Dict[str, Any]:
        if not query.strip():
            return {"success": False, "error": "Query must not be empty"}
        num_results = max(1, min(num_results, 10))

        cache_params = (
            f"{query}|{num_results}|"
            f"{','.join(allowed_domains or [])}|{','.join(blocked_domains or [])}"
        )
        cache_key = _cache_key("search", cache_params)
        if not fetch_content:
            cached = _get_cached(cache_key)
            if cached is not None:
                return {
                    "success": True,
                    "results": cached,
                    "query": query,
                    "backend": "cache",
                    "result_count": len(cached),
                    "from_cache": True,
                }

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
                    "note": "No results found. Try rephrasing the query.",
                    "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
                }

            if fetch_content:
                results = await self._fetch_top_results(
                    results,
                    min(3, len(results)),
                    max_content_length,
                    extract_main=extract_main_content,
                )

            try:
                from coderAI.system.config import config_manager

                ttl = config_manager.load().search_cache_ttl_seconds
            except Exception:
                ttl = _DEFAULT_SEARCH_TTL
            _set_cached(cache_key, results, ttl)

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
        results: List[Dict[str, Any]],
        count: int,
        max_length: int,
        extract_main: bool = False,
    ) -> List[Dict[str, Any]]:
        async def _fetch_one(result: Dict[str, Any]) -> Dict[str, Any]:
            url = result.get("url", "")
            try:
                text = await _fetch_page_text(url, max_length, extract_main=extract_main)
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
        backend = backend or _select_search_backend()
        from coderAI.system.config import config_manager

        cfg = config_manager.load()
        tavily_key = os.getenv("TAVILY_API_KEY") or cfg.tavily_api_key
        exa_key = os.getenv("EXA_API_KEY") or cfg.exa_api_key
        explicit = os.getenv("CODERAI_SEARCH_BACKEND") or cfg.search_backend or "auto"
        explicit = explicit.lower().strip()

        if (
            not tavily_key
            and not exa_key
            and explicit not in ("tavily", "exa")
            and _concurrent_search_enabled()
        ):
            return await self._search_concurrent(
                query, num_results, allowed_domains, blocked_domains
            )

        raw = await backend.search(query, num_results, allowed_domains, blocked_domains)
        filtered = _filter_by_domain(raw, allowed_domains, blocked_domains)
        return [r.to_dict() for r in filtered[:num_results]]

    async def _search_concurrent(
        self,
        query: str,
        num_results: int,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        backends = _select_free_backends()

        async def _run(be: _SearchBackend) -> Optional[List[_SearchResult]]:
            try:
                return await be.search(query, num_results * 2, allowed_domains, blocked_domains)
            except Exception as e:
                logger.debug(f"Backend {be.name} failed: {e}")
                return None

        all_results_raw = await asyncio.gather(*[_run(b) for b in backends])

        succeeded = [r for r in all_results_raw if r is not None]
        if not succeeded:
            raise RuntimeError(
                f"All search backends failed ({', '.join(b.name for b in backends)}). "
                "Check your network connection."
            )

        seen_urls: set = set()
        merged: List[_SearchResult] = []
        for batch in succeeded:
            for r in batch:
                normalized = r.url.rstrip("/").lower()
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    merged.append(r)

        filtered = _filter_by_domain(merged, allowed_domains, blocked_domains)
        return [r.to_dict() for r in filtered[:num_results]]


# ═══════════════════════════════════════════════════════════════════════════
# ReadURLTool
# ═══════════════════════════════════════════════════════════════════════════


class ReadURLParams(BaseModel):
    url: str = Field(..., description="URL to fetch and read")
    max_length: int = Field(8000, description="Maximum characters of page text to return")
    format: str = Field(
        "markdown",
        description="Output format: 'markdown', 'text', or 'html'",
    )
    extract_main: bool = Field(
        False,
        description="Extract only the main content using readability heuristics.",
    )
    extract_metadata: bool = Field(
        False,
        description="Also extract Open Graph, Twitter Card, JSON-LD metadata.",
    )


class ReadURLTool(Tool):
    """Fetch a web page and return its text content."""

    name = "read_url"
    description = (
        "Fetch a web page URL and return its content. Choose the output "
        "format with format='markdown'|'text'|'html'. Set extract_main=true "
        "to get only the article content (strips nav, ads, sidebars). "
        "Set extract_metadata=true to also get title, description, OG tags. "
        "Supports PDF extraction if pypdf is installed. "
        "Responses larger than 5MB are reported as oversize and truncated."
    )
    is_read_only = True
    parameters_model = ReadURLParams

    async def execute(  # type: ignore[override]
        self,
        url: str,
        max_length: int = 8000,
        format: str = "markdown",
        extract_main: bool = False,
        extract_metadata: bool = False,
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
                "error": f"SSRF Protection triggered for {url}.",
            }
        if resp["status"] != 200:
            return {
                "success": False,
                "error": f"HTTP {resp['status']} for {url}",
                "oversize": resp.get("oversize", False),
            }

        content_type = resp.get("content_type", "")
        raw = resp["text"]
        is_pdf = "pdf" in content_type.lower() or url.lower().endswith(".pdf")

        if is_pdf and "pdf" in content_type.lower():
            pdf_text = _extract_pdf_text(resp.get("content", b""))
            if pdf_text:
                text = pdf_text
                content_type = "text/plain"
            else:
                return {
                    "success": False,
                    "error": (
                        "PDF content detected but text extraction failed. "
                        "Install pypdf: pip install pypdf"
                    ),
                }
        elif extract_main and _looks_like_html(content_type, raw):
            raw = _extract_main_content(raw)
            text = _html_to_text(raw, fmt)
        else:
            text = _html_to_text(raw, fmt)

        truncated = False
        if len(text) > max_length:
            text = text[:max_length]
            truncated = True

        result: Dict[str, Any] = {
            "success": True,
            "url": resp["url"],
            "format": fmt,
            "content": text,
            "length": len(text),
            "truncated": truncated,
            "oversize": resp.get("oversize", False),
            "content_type": content_type,
        }

        if extract_metadata and _looks_like_html(content_type, resp["text"]):
            result["metadata"] = _extract_metadata(resp["text"])

        return result


# ═══════════════════════════════════════════════════════════════════════════
# DownloadFileTool
# ═══════════════════════════════════════════════════════════════════════════


class DownloadFileParams(BaseModel):
    url: str = Field(..., description="URL of the file to download")
    destination_path: str = Field(..., description="Absolute path where the file should be saved.")


class DownloadFileTool(Tool):
    """Download a file from a URL to the local filesystem."""

    name = "download_file"
    description = (
        "Download a file (like a ZIP, image, or raw code snippet) from a given URL to a "
        "local destination. Returns the absolute path to the downloaded file."
    )
    is_read_only = False
    requires_confirmation = True
    parameters_model = DownloadFileParams
    timeout = 300.0

    async def execute(self, url: str, destination_path: str) -> Dict[str, Any]:  # type: ignore[override]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            resp = await _safe_request_cf("GET", url, timeout_s=60.0, max_bytes=50 * 1024 * 1024)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout downloading {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to download {url}: {e}"}

        if resp is None:
            return {"success": False, "error": f"SSRF Protection blocked {url}."}
        if resp["status"] != 200:
            return {"success": False, "error": f"HTTP {resp['status']} for {url}"}
        content = resp["content"]

        try:
            dest = Path(destination_path).expanduser().resolve()
            if _is_path_protected(dest):
                return {
                    "success": False,
                    "error": f"Refusing to download to protected path: {dest}",
                }
            scope_err = _enforce_project_scope(dest, "download_file")
            if scope_err is not None:
                return scope_err

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
        except Exception as e:
            return {"success": False, "error": f"Failed to download {url}: {e}"}


# ═══════════════════════════════════════════════════════════════════════════
# HTTPRequestTool
# ═══════════════════════════════════════════════════════════════════════════


class HTTPRequestParams(BaseModel):
    url: str = Field(..., description="Full URL to send the request to")
    method: str = Field("GET", description="HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD")
    headers: Optional[Dict[str, str]] = Field(None, description="Optional HTTP headers")
    json_body: Optional[Dict[str, Any]] = Field(None, description="Request body as a JSON object")
    body: Optional[str] = Field(None, description="Raw request body string")
    timeout: int = Field(30, description="Request timeout in seconds (default: 30)")
    max_response_length: int = Field(
        16000, description="Maximum characters of response body to return"
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
    requires_confirmation = True
    parameters_model = HTTPRequestParams

    async def execute(  # type: ignore[override]
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
            return {
                "success": False,
                "error": f"Method '{method}' not allowed. Use one of: {allowed_methods}",
            }

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
            return {"success": False, "error": f"SSRF Protection blocked {url}."}

        status = resp["status"]
        resp_headers = resp["headers"]
        content_type = resp["content_type"]
        raw = resp["text"]

        truncated = False
        if len(raw) > max_response_length:
            raw = raw[:max_response_length]
            truncated = True

        response_body = raw
        if _looks_like_html(content_type, raw):
            response_body = _html_to_text(raw, "markdown")

        return {
            "success": True,
            "status": status,
            "headers": resp_headers,
            "content_type": content_type,
            "response": response_body,
            "raw_response": raw if raw != response_body else None,
            "response_length": len(response_body),
            "truncated": truncated,
            "oversize": resp.get("oversize", False),
        }


# ═══════════════════════════════════════════════════════════════════════════
# WikipediaSearchTool
# ═══════════════════════════════════════════════════════════════════════════


class WikipediaSearchParams(BaseModel):
    query: str = Field(..., description="Search query for Wikipedia")
    num_results: int = Field(5, description="Number of results (default 5, max 10)")
    language: str = Field("en", description="Wikipedia language code: en, de, fr, es, ja, zh, etc.")
    fetch_content: bool = Field(
        False, description="Fetch and return the full page text of the top result."
    )
    max_content_length: int = Field(8000, description="Max characters when fetch_content=true")


class WikipediaSearchTool(Tool):
    """Search Wikipedia directly — free, no API key required."""

    name = "wikipedia_search"
    description = (
        "Search Wikipedia for articles. Free, no API key needed. "
        "Returns titles, URLs, and text snippets from Wikipedia search. "
        "Set fetch_content=true to also read the full article text of the top result. "
        "Use language= to search other language editions (e.g., 'de' for German)."
    )
    is_read_only = True
    parameters_model = WikipediaSearchParams

    async def execute(  # type: ignore[override]
        self,
        query: str,
        num_results: int = 5,
        language: str = "en",
        fetch_content: bool = False,
        max_content_length: int = 8000,
    ) -> Dict[str, Any]:
        num_results = max(1, min(num_results, 10))
        lang = language.strip().lower()[:2] or "en"

        base_url = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": str(num_results),
            "format": "json",
        }
        qs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
        api_url = f"{base_url}?{qs}"

        try:
            resp = await _safe_request_cf("GET", api_url, timeout_s=15.0)
        except Exception as e:
            return {"success": False, "error": f"Wikipedia request failed: {e}"}

        if resp is None:
            return {"success": False, "error": "Wikipedia request blocked by SSRF guard"}
        if resp["status"] != 200:
            return {"success": False, "error": f"Wikipedia HTTP {resp['status']}"}

        try:
            data = json.loads(resp["text"])
        except json.JSONDecodeError:
            return {"success": False, "error": "Wikipedia returned non-JSON response"}

        results = []
        for r in data.get("query", {}).get("search", []):
            title = r.get("title", "")
            results.append(
                {
                    "title": title,
                    "url": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                    "snippet": _strip_tags(r.get("snippet", "")),
                    "page_id": r.get("pageid"),
                    "word_count": r.get("wordcount"),
                }
            )

        if fetch_content and results:
            page_title = results[0]["title"]
            content_params = {
                "action": "query",
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "titles": page_title,
                "format": "json",
            }
            cqs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in content_params.items())
            content_url = f"{base_url}?{cqs}"

            try:
                c_resp = await _safe_request_cf("GET", content_url, timeout_s=15.0)
                if c_resp and c_resp["status"] == 200:
                    c_data = json.loads(c_resp["text"])
                    pages = c_data.get("query", {}).get("pages", {})
                    for pid, page in pages.items():
                        extract = page.get("extract", "")
                        if len(extract) > max_content_length:
                            extract = extract[:max_content_length] + "\n\n[...truncated...]"
                        results[0]["page_content"] = extract
                        results[0]["page_content_length"] = len(extract)
                        break
            except Exception as e:
                logger.debug(f"Wikipedia content fetch failed: {e}")

        return {
            "success": True,
            "results": results,
            "query": query,
            "language": lang,
            "result_count": len(results),
        }


# ═══════════════════════════════════════════════════════════════════════════
# FeedReaderTool (RSS / Atom)
# ═══════════════════════════════════════════════════════════════════════════


class ReadFeedParams(BaseModel):
    url: str = Field(..., description="URL of the RSS or Atom feed")
    max_entries: int = Field(10, description="Maximum feed entries to return (default 10, max 50)")
    fetch_content: bool = Field(
        False, description="If true, also fetch linked page content for each entry (up to 5)."
    )
    max_content_length: int = Field(
        4000, description="Max characters per entry content when fetch_content=true"
    )


class ReadFeedTool(Tool):
    """Read RSS and Atom feeds — no API key needed."""

    name = "read_feed"
    description = (
        "Read and parse an RSS or Atom feed. Returns feed metadata and entries "
        "with title, link, published date, and summary. Set fetch_content=true "
        "to also read the linked article content for the top entries. "
        "Useful for monitoring blog posts, changelogs, release notes, and news."
    )
    is_read_only = True
    parameters_model = ReadFeedParams

    async def execute(  # type: ignore[override]
        self,
        url: str,
        max_entries: int = 10,
        fetch_content: bool = False,
        max_content_length: int = 4000,
    ) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        max_entries = max(1, min(max_entries, 50))

        try:
            resp = await _safe_request_cf("GET", url, timeout_s=20.0)
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout fetching feed: {url}"}
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch feed: {e}"}

        if resp is None:
            return {"success": False, "error": f"SSRF Protection blocked {url}"}
        if resp["status"] != 200:
            return {"success": False, "error": f"HTTP {resp['status']} for {url}"}

        raw = resp["text"]
        entries = _parse_feed(raw, max_entries)

        if not entries:
            return {"success": False, "error": "Could not parse feed — may not be valid RSS/Atom"}

        if fetch_content:
            entries = await self._fetch_entry_content(
                entries, min(5, len(entries)), max_content_length
            )

        return {
            "success": True,
            "feed_url": url,
            "feed_title": _extract_feed_metadata(raw).get("title", ""),
            "entries": entries,
            "entry_count": len(entries),
        }

    async def _fetch_entry_content(
        self, entries: List[Dict[str, Any]], count: int, max_length: int
    ) -> List[Dict[str, Any]]:
        async def _fetch_one(entry: Dict[str, Any]) -> Dict[str, Any]:
            link = entry.get("link", "")
            if not link:
                return entry
            try:
                text = await _fetch_page_text(link, max_length, extract_main=True)
                if text:
                    entry["page_content"] = text
            except Exception as e:
                logger.debug(f"Feed content fetch failed for {link}: {e}")
            return entry

        fetched = await asyncio.gather(*[_fetch_one(e) for e in entries[:count]])
        return list(fetched) + entries[count:]


def _parse_feed(raw: str, max_entries: int) -> List[Dict[str, Any]]:
    from xml.etree import ElementTree as ET

    entries: List[Dict[str, Any]] = []
    try:
        start = raw.find("<")
        if start > 0:
            raw = raw[start:]
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.debug(f"XML parse error: {e}")
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }

    for item in root.iter("item"):
        if len(entries) >= max_entries:
            break
        entries.append(_parse_rss_item(item, ns))

    for entry_elem in root.iter("{http://www.w3.org/2005/Atom}entry"):
        if len(entries) >= max_entries:
            break
        entries.append(_parse_atom_entry(entry_elem, ns))

    return entries


def _parse_rss_item(item, ns: Dict[str, str]) -> Dict[str, Any]:
    def _text(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    link = _text("link")
    if not link:
        guid = item.find("guid")
        if guid is not None and guid.text:
            link = guid.text.strip()

    return {
        "title": _text("title"),
        "link": link,
        "published": _text("pubDate") or _text("{http://purl.org/dc/elements/1.1/}date"),
        "summary": _text("description")
        or _text("{http://purl.org/rss/1.0/modules/content/}encoded"),
        "author": _text("author") or _text("{http://purl.org/dc/elements/1.1/}creator"),
    }


def _parse_atom_entry(entry, ns: Dict[str, str]) -> Dict[str, Any]:
    def _text(tag: str) -> str:
        el = entry.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    link = ""
    for link_el in entry.findall("{http://www.w3.org/2005/Atom}link"):
        rel = link_el.get("rel", "alternate")
        href = link_el.get("href", "")
        if rel == "alternate" and href:
            link = href
            break
    if not link:
        for link_el in entry.findall("{http://www.w3.org/2005/Atom}link"):
            href = link_el.get("href", "")
            if href:
                link = href
                break

    return {
        "title": _text("{http://www.w3.org/2005/Atom}title"),
        "link": link,
        "published": _text("{http://www.w3.org/2005/Atom}updated")
        or _text("{http://www.w3.org/2005/Atom}published"),
        "summary": _text("{http://www.w3.org/2005/Atom}summary")
        or _text("{http://www.w3.org/2005/Atom}content"),
        "author": "",
    }


def _extract_feed_metadata(raw: str) -> Dict[str, str]:
    from xml.etree import ElementTree as ET

    try:
        start = raw.find("<")
        if start > 0:
            raw = raw[start:]
        root = ET.fromstring(raw)

        channel = root.find("channel")
        if channel is not None:
            title_el = channel.find("title")
            desc_el = channel.find("description")
            return {
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "description": (desc_el.text or "").strip() if desc_el is not None else "",
            }

        title_el = root.find("{http://www.w3.org/2005/Atom}title")
        return {
            "title": (title_el.text or "").strip() if title_el is not None else "",
        }
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# SitemapDiscoverTool
# ═══════════════════════════════════════════════════════════════════════════


class SitemapDiscoverParams(BaseModel):
    url: str = Field(
        ..., description="Base URL of the website. Auto-discovers sitemap.xml from robots.txt."
    )
    sitemap_url: Optional[str] = Field(None, description="Direct URL to a sitemap.xml if known.")
    max_urls: int = Field(50, description="Maximum number of URLs to return (default 50, max 200)")
    filter_path: Optional[str] = Field(
        None, description="Only return URLs whose path contains this string"
    )


class SitemapDiscoverTool(Tool):
    """Discover pages on a website via sitemap.xml and robots.txt."""

    name = "sitemap_discover"
    description = (
        "Discover pages on a website by parsing its sitemap.xml (auto-discovered "
        "from robots.txt if not explicitly provided). Returns a list of URLs. "
        "Use filter_path to narrow results (e.g., '/docs/' or '/api/'). "
        "Useful for understanding a site's structure or finding documentation pages."
    )
    is_read_only = True
    parameters_model = SitemapDiscoverParams

    async def execute(  # type: ignore[override]
        self,
        url: str,
        sitemap_url: Optional[str] = None,
        max_urls: int = 50,
        filter_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        max_urls = max(1, min(max_urls, 200))

        discovered: List[str] = []

        if not sitemap_url:
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            sitemap_url = await _discover_sitemap_from_robots(robots_url)
            if not sitemap_url:
                for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]:
                    candidate = f"{parsed.scheme}://{parsed.netloc}{path}"
                    if await _url_exists(candidate):
                        sitemap_url = candidate
                        break

        if sitemap_url:
            discovered = await _fetch_sitemap_urls(sitemap_url, max_urls)
        else:
            return {
                "success": False,
                "error": f"Could not discover sitemap for {url}. Provide sitemap_url manually.",
            }

        if filter_path:
            discovered = [u for u in discovered if filter_path in u]

        return {
            "success": True,
            "site_url": url,
            "sitemap_url": sitemap_url,
            "urls": discovered[:max_urls],
            "url_count": min(len(discovered), max_urls),
            "total_discovered": len(discovered),
        }


async def _discover_sitemap_from_robots(robots_url: str) -> Optional[str]:
    try:
        resp = await _safe_request_cf("GET", robots_url, timeout_s=10.0)
        if resp and resp["status"] == 200:
            for line in resp["text"].splitlines():
                line_lower = line.strip().lower()
                if line_lower.startswith("sitemap:"):
                    return str(line.strip().split(":", 1)[1].strip())
    except Exception as e:
        logger.debug(f"robots.txt fetch failed: {e}")
    return None


async def _url_exists(url: str) -> bool:
    try:
        resp = await _safe_request_cf("HEAD", url, timeout_s=10.0)
        return resp is not None and resp["status"] == 200
    except Exception:
        return False


async def _fetch_sitemap_urls(sitemap_url: str, max_urls: int) -> List[str]:
    try:
        resp = await _safe_request_cf("GET", sitemap_url, timeout_s=20.0)
        if not resp or resp["status"] != 200:
            return []
    except Exception as e:
        logger.debug(f"Sitemap fetch failed: {e}")
        return []
    return _parse_sitemap(resp["text"], max_urls)


def _parse_sitemap(xml_text: str, max_urls: int) -> List[str]:
    from xml.etree import ElementTree as ET

    try:
        start = xml_text.find("<")
        if start > 0:
            xml_text = xml_text[start:]
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    urls: List[str] = []
    ns_url = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

    for url_elem in root.iter(f"{ns_url}url"):
        if len(urls) >= max_urls:
            break
        loc = url_elem.find(f"{ns_url}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())

    if not urls:
        for sm_elem in root.iter(f"{ns_url}sitemap"):
            loc = sm_elem.find(f"{ns_url}loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    if not urls:
        for url_elem in root.iter("url"):
            if len(urls) >= max_urls:
                break
            loc = url_elem.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    if not urls:
        for sm_elem in root.iter("sitemap"):
            loc = sm_elem.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    return urls

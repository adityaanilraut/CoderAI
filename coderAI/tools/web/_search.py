"""Search backends (Tavily / Exa / DDG / SearXNG), parsers, and selection."""

import asyncio
import html as html_lib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, unquote, urlparse

# Calls to _safe_request go through the package namespace so tests can patch
# coderAI.tools.web._safe_request as a single point, exactly as they could
# when everything lived in one module.
import coderAI.tools.web as _web
from coderAI.tools.web._constants import (
    _HEADERS_CHROME,
    _HEADERS_FIREFOX,
    _SEARXNG_INSTANCES,
    _TRANSPARENT_UA,
)
from coderAI.tools.web._html import _strip_tags

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Precompiled Regex Patterns
# ═══════════════════════════════════════════════════════════════════════════

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

        resp = await _web._safe_request("POST", self.ENDPOINT, json_body=body, timeout_s=20.0)
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
        resp = await _web._safe_request(
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
                resp = await _web._safe_request(
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
                # Retry with the next header set; final failure raises RuntimeError below.
                last_error = str(e)
                logger.debug(f"DDG attempt {attempt + 1}: {e}", exc_info=True)

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
        # Keep whatever results were parsed before the failure.
        logger.debug("DDG v2 parse error", exc_info=True)

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
                resp = await _web._safe_request(
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
                # Public SearXNG instances flake routinely; a None return just
                # drops this instance from the race — others may still succeed.
                logger.debug(f"SearXNG {instance}: {e}", exc_info=True)
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
        # Config unavailable → fall back to env var / default (concurrent on).
        logger.debug("concurrent_search config unavailable, using fallback", exc_info=True)
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
        # urlparse raises only on pathologically malformed URLs; an empty
        # domain simply matches no allow/block pattern downstream.
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

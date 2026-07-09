"""The web Tool classes: search, read_url, download, and HTTP."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel, Field

# Patchable seams (_safe_request_cf, _fetch_page_text, _get_cached, _set_cached,
# _select_search_backend, _select_free_backends, _concurrent_search_enabled) are
# resolved through the package namespace so tests can patch
# coderAI.tools.web.<name> as a single point, exactly as they could when
# everything lived in one module.
import coderAI.tools.web as _web
from coderAI.core.provenance import Provenance
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool
from coderAI.tools.filesystem import _enforce_project_scope, _is_path_protected
from coderAI.tools.web._cache import _cache_key, _DEFAULT_SEARCH_TTL
from coderAI.tools.web._constants import _HEADERS_CHROME, _VALID_FORMATS
from coderAI.tools.web._html import (
    _extract_main_content,
    _extract_metadata,
    _extract_pdf_text,
    _html_to_text,
    _looks_like_html,
)
from coderAI.tools.web._search import _filter_by_domain, _SearchBackend

logger = logging.getLogger(__name__)


# Destination extensions and response content-types that indicate an executable
# or script. ``download_file`` refuses these: writing attacker-controlled
# executable content to disk is a code-execution / supply-chain vector that the
# path + size + SSRF guards do not cover. (Fetch the text with ``read_url`` and
# write it via ``write_file`` — a confirmed, previewed edit — if truly needed.)
_EXECUTABLE_DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".sh", ".bash", ".zsh", ".ksh", ".csh", ".command", ".fish",
        ".exe", ".bat", ".cmd", ".com", ".msi", ".ps1", ".psm1", ".scr",
        ".vbs", ".vbe", ".jse", ".wsf", ".wsh", ".hta",
        ".so", ".dylib", ".dll", ".scpt", ".desktop",
    }
)  # fmt: skip
_EXECUTABLE_CONTENT_TYPES = frozenset(
    {
        "application/x-sh",
        "application/x-shellscript",
        "text/x-shellscript",
        "application/x-executable",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-mach-binary",
        "application/x-elf",
        "application/vnd.microsoft.portable-executable",
    }
)


def _download_type_blocked(dest: Path, content_type: str) -> Optional[str]:
    """Return an error string if *dest*/*content_type* names executable content."""
    suffix = dest.suffix.lower()
    if suffix in _EXECUTABLE_DOWNLOAD_EXTENSIONS:
        return (
            f"Refusing to download to an executable/script destination "
            f"('{suffix}'). download_file does not fetch runnable code; use "
            "read_url then write_file if you have reviewed the content."
        )
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _EXECUTABLE_CONTENT_TYPES:
        return (
            f"Refusing to download executable content (Content-Type '{mime}'). "
            "download_file does not fetch runnable code."
        )
    return None


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
    is_egress = True
    # Transient network failures (429/5xx/resets) are worth one more try.
    retryable = True
    # Removed from the main agent when web_tools_in_main is False (Phase 4.2).
    network_gate = True
    category = "web"
    result_provenance = Provenance.UNTRUSTED_EXTERNAL

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
            cached = _web._get_cached(cache_key)
            if cached is not None:
                return {
                    "success": True,
                    "results": cached,
                    "query": query,
                    "backend": "cache",
                    "result_count": len(cached),
                    "from_cache": True,
                }

        backend = _web._select_search_backend()
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
                from coderAI.core.services import get_services

                ttl = get_services().config.search_cache_ttl_seconds
            except Exception:
                # Config unavailable → default TTL; caching must not break search.
                logger.debug("search_cache_ttl config unavailable, using default", exc_info=True)
                ttl = _DEFAULT_SEARCH_TTL
            _web._set_cached(cache_key, results, ttl)

            return {
                "success": True,
                "results": results,
                "query": query,
                "backend": backend.name,
                "result_count": len(results),
            }

        except Exception as e:
            logger.error(f"Web search error ({backend.name}): {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
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
                text = await _web._fetch_page_text(url, max_length, extract_main=extract_main)
                if text:
                    result["page_content"] = text
                    result["page_content_length"] = len(text)
            except Exception as e:
                # Best-effort enrichment: surface the error on the result and
                # return the search hit without page content.
                logger.debug(f"Auto-fetch failed for {url}: {e}", exc_info=True)
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
        backend = backend or _web._select_search_backend()
        from coderAI.core.services import get_services

        cfg = get_services().config
        tavily_key = os.getenv("TAVILY_API_KEY") or cfg.tavily_api_key
        exa_key = os.getenv("EXA_API_KEY") or cfg.exa_api_key
        explicit = os.getenv("CODERAI_SEARCH_BACKEND") or cfg.search_backend or "auto"
        explicit = explicit.lower().strip()

        if (
            not tavily_key
            and not exa_key
            and explicit not in ("tavily", "exa")
            and _web._concurrent_search_enabled()
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
        backends = _web._select_free_backends()

        async def _run(be: _SearchBackend) -> Optional[List[Any]]:
            try:
                return await be.search(query, num_results * 2, allowed_domains, blocked_domains)
            except Exception as e:
                # A failing backend drops out of the race; if every backend
                # fails, the all-None check below raises RuntimeError.
                logger.debug(f"Backend {be.name} failed: {e}", exc_info=True)
                return None

        all_results_raw = await asyncio.gather(*[_run(b) for b in backends])

        succeeded = [r for r in all_results_raw if r is not None]
        if not succeeded:
            raise RuntimeError(
                f"All search backends failed ({', '.join(b.name for b in backends)}). "
                "Check your network connection."
            )

        seen_urls: set = set()
        merged: List[Any] = []
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
    is_egress = True
    # Transient network failures (429/5xx/resets) are worth one more try.
    retryable = True
    # Removed from the main agent when web_tools_in_main is False (Phase 4.2).
    network_gate = True
    category = "web"

    result_provenance = Provenance.UNTRUSTED_EXTERNAL
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
            resp = await _web._safe_request_cf("GET", url, timeout_s=25.0)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Timeout fetching {url}",
                "error_code": ToolErrorCode.TIMEOUT,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch {url}: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

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
    is_egress = True
    # Removed from the main agent when web_tools_in_main is False (Phase 4.2).
    network_gate = True
    requires_confirmation = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    category = "web"
    parameters_model = DownloadFileParams
    timeout = 300.0

    async def execute(self, url: str, destination_path: str) -> Dict[str, Any]:  # type: ignore[override]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            resp = await _web._safe_request_cf(
                "GET", url, timeout_s=60.0, max_bytes=50 * 1024 * 1024
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Timeout downloading {url}",
                "error_code": ToolErrorCode.TIMEOUT,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to download {url}: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

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
            # Refuse executable/script destinations and payloads (the path, size
            # and SSRF guards don't cover "download runnable code and execute it").
            type_err = _download_type_blocked(dest, resp.get("content_type", ""))
            if type_err is not None:
                return {
                    "success": False,
                    "error": type_err,
                    "error_code": ToolErrorCode.PERMISSION_DENIED,
                }

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
            return {
                "success": False,
                "error": f"Failed to download {url}: {e}",
                "error_code": ToolErrorCode.IO,
            }


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
    is_egress = True
    # Removed from the main agent when web_tools_in_main is False (Phase 4.2).
    network_gate = True
    requires_confirmation = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
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
            resp = await _web._safe_request_cf(
                method,
                url,
                headers=req_headers,
                json_body=json_body,
                body=body,
                timeout_s=float(timeout),
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Request timed out after {timeout}s: {url}",
                "error_code": ToolErrorCode.TIMEOUT,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Request failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

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

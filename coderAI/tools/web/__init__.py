# ruff: noqa: F401
"""Web and URL tools for search and content fetching.

This package replaces the former ``coderAI/tools/web.py`` monolith. The full
public *and* private surface of that module is re-exported here so existing
imports (``from coderAI.tools.web import _is_ip_public``) and test patch
targets (``patch("coderAI.tools.web._safe_request")``) keep working.

Submodules call patchable seams (``_safe_request``, ``_safe_request_cf``,
``_get_cached``, ``_set_cached``, ``_concurrent_search_enabled``, …) through
this package namespace, so patching ``coderAI.tools.web.<name>`` affects every
caller — the same single-patch-point semantics the monolith had.
"""

import asyncio  # re-exported: tests patch coderAI.tools.web.asyncio.sleep

from coderAI.tools.web._cache import (
    _CACHE_DIR,
    _cache_dir,
    _cache_key,
    _cache_path,
    _DEFAULT_PAGE_TTL,
    _DEFAULT_SEARCH_TTL,
    _get_cached,
    _maybe_prune,
    _prune_cache,
    _set_cached,
)
from coderAI.tools.web._constants import (
    _CF_BLOCK_HEADERS,
    _HEADERS_CHROME,
    _HEADERS_FIREFOX,
    _MAX_REDIRECTS,
    _MAX_RESPONSE_BYTES,
    _SEARXNG_INSTANCES,
    _TRANSPARENT_UA,
    _VALID_FORMATS,
)
from coderAI.tools.web._ratelimit import (
    _get_rate_limit_delay,
    _last_request,
    _rate_limit_async,
)
from coderAI.tools.web._html import (
    _extract_main_content,
    _extract_metadata,
    _extract_pdf_text,
    _get_h2t,
    _html_to_text,
    _looks_like_html,
    _strip_tags,
)
from coderAI.tools.web._http import (
    _allow_local,
    _fetch_page_text,
    _get_session,
    _get_ssl_ctx,
    _is_cloudflare_block,
    _is_ip_public,
    _safe_request,
    _safe_request_cf,
    _SSRFResolver,
)
from coderAI.tools.web._search import (
    _concurrent_search_enabled,
    _DDGBackend,
    _domain_of,
    _ExaBackend,
    _filter_by_domain,
    _matches_domain,
    _parse_ddg_results,
    _parse_ddg_results_v2,
    _parse_searxng_results,
    _resolve_ddg_url,
    _SearchBackend,
    _SearchResult,
    _SearXNGBackend,
    _select_free_backends,
    _select_search_backend,
    _searxng_instances,
    _TavilyBackend,
)
from coderAI.tools.web.tools import (
    DownloadFileParams,
    DownloadFileTool,
    HTTPRequestParams,
    HTTPRequestTool,
    ReadURLParams,
    ReadURLTool,
    WebSearchParams,
    WebSearchTool,
)

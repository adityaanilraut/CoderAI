"""CoderAI Network & Web Access Subsystem."""

from coderai.network.cache import ResponseCache, get_fetch_cache, get_search_cache
from coderai.utils.aiohttp import HttpClient, HttpResponse, get_http_client
from coderai.tools.web.fetch import (
    ExtractedWebPage,
    extract_and_sanitize_html,
    sanitize_prompt_injection,
    slice_payload,
)
from coderai.network.security import (
    NetworkPolicy,
    NetworkSecurityError,
    check_outbound_url,
    validate_outbound_url,
)

__all__ = [
    "ExtractedWebPage",
    "HttpClient",
    "HttpResponse",
    "NetworkPolicy",
    "NetworkSecurityError",
    "ResponseCache",
    "check_outbound_url",
    "extract_and_sanitize_html",
    "get_fetch_cache",
    "get_http_client",
    "get_search_cache",
    "sanitize_prompt_injection",
    "slice_payload",
    "validate_outbound_url",
]

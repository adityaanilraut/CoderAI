"""CoderAI Network & Web Access Subsystem."""

from coderai.core.network.cache import ResponseCache, get_fetch_cache, get_search_cache
from coderai.core.network.client import HttpClient, HttpResponse, get_http_client
from coderai.core.network.sanitizer import (
    ExtractedWebPage,
    extract_and_sanitize_html,
    sanitize_prompt_injection,
    slice_payload,
)
from coderai.core.network.security import (
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

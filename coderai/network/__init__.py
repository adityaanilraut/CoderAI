"""CoderAI Network & Web Access Subsystem."""

from coderai.network.cache import (
    ResponseCache,
    build_search_key,
    get_fetch_cache,
    get_search_cache,
    normalize_search_query,
)
from coderai.utils.aiohttp import HttpClient, HttpResponse, get_http_client
from coderai.network.security import (
    NetworkPolicy,
    NetworkSecurityError,
    check_outbound_url,
    validate_outbound_url,
)

__all__ = [
    "HttpClient",
    "HttpResponse",
    "NetworkPolicy",
    "NetworkSecurityError",
    "ResponseCache",
    "build_search_key",
    "check_outbound_url",
    "get_fetch_cache",
    "get_http_client",
    "get_search_cache",
    "normalize_search_query",
    "validate_outbound_url",
]

"""Shared constants for the web tools package."""

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

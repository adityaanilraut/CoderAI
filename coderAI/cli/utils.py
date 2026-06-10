"""Shared CLI utilities."""

from typing import Set


def valid_models() -> Set[str]:
    """Return the set of valid default-model values accepted by setup()."""
    from coderAI.llm.factory import get_all_model_ids

    return get_all_model_ids()


def valid_endpoint(url: str) -> bool:
    """Loose URL check: must start with http:// or https:// and have a host."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)

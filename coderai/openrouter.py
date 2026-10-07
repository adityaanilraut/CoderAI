"""Dynamic OpenRouter model catalog with local caching.

Fetches https://openrouter.ai/api/v1/models (public, no auth required),
filters to free models, and caches the result under ~/.coderai/ so the
/model picker stays fast and works offline.

Free = id ends with ":free" OR all pricing fields are zero. The ":free"
suffix check is the primary signal since OpenRouter keeps adding/removing
free models; pricing-zero is a fallback for entries missing the suffix.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_FILENAME = "openrouter_models.json"
CACHE_TTL_SECONDS = 24 * 3600
OPENROUTER_APP_URL = "https://github.com/adityaanilraut/CoderAI"


def attribution_headers(custom_headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Identify CoderAI honestly; preserve explicit, case-insensitive overrides.

    These are app attribution headers, not a guarantee of gated model access.
    """
    headers = dict(custom_headers or {})
    names = {name.lower() for name in headers}
    defaults = {
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-OpenRouter-Title": "CoderAI",
        "X-OpenRouter-Categories": "cli-agent",
    }
    for name, value in defaults.items():
        if name.lower() in names:
            continue
        if name == "X-OpenRouter-Title" and "x-title" in names:
            continue
        headers[name] = value
    return headers


# Where the most recent fetch_openrouter_models() result came from:
# "live" (just pinged provider) | "cache" (fresh cache, no ping) |
# "stale" (ping failed, showing expired cache) | "fallback" (no cache at all).
_last_source = "unknown"
# Short reason for the last failed ping (None after any success/cache hit).
_last_error: str | None = None

# Static fallback used when network + cache both miss (offline first run).
# These rot over time (providers retire :free slugs); the status line always
# says when the fallback is in use so it is never mistaken for live data.
FALLBACK_FREE_IDS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "deepseek/deepseek-r1:free",
]


def _cache_path() -> Path:
    base = os.getenv("CODERAI_SHARE_DIR") or str(Path.home() / ".coderai")
    return Path(base).expanduser() / CACHE_FILENAME


def _ssl_context() -> Any:
    """SSL context for the catalog ping.

    Stock macOS framework Pythons ship without CA certificates, so stdlib
    HTTPS fails with CERTIFICATE_VERIFY_FAILED while httpx-based chat calls
    (which bundle certifi) work fine. Prefer certifi's bundle when present;
    otherwise fall back to the platform default. Verification is never
    disabled.
    """
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _read_cache(max_age: float = CACHE_TTL_SECONDS) -> list[dict] | None:
    try:
        path = _cache_path()
        if not path.is_file():
            return None
        if max_age >= 0 and time.time() - path.stat().st_mtime > max_age:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("data") if isinstance(data, dict) else data
        if isinstance(entries, list):
            return [e for e in entries if isinstance(e, dict)]
        return None
    except (OSError, ValueError):
        return None


def _write_cache(entries: list[dict]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"data": entries}), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _is_free(entry: dict) -> bool:
    model_id = str(entry.get("id") or "")
    if model_id.endswith(":free"):
        return True
    pricing = entry.get("pricing") or {}
    if isinstance(pricing, dict) and pricing:
        try:
            return all(float(pricing.get(k) or 0) == 0 for k in ("prompt", "completion"))
        except (TypeError, ValueError):
            return False
    return False


def get_openrouter_model_metadata(model: str) -> dict[str, Any] | None:
    """Read model capabilities from the local catalog, without network I/O.

    Expired metadata remains useful for mandatory reasoning. Availability checks
    below require a fresh catalog so an outage cannot hide configured models.
    """
    raw_id = model.strip().removeprefix("openrouter/")
    return next((e for e in _read_cache(max_age=-1) or [] if e.get("id") == raw_id), None)


def openrouter_model_available(model: str) -> bool | None:
    """Catalog membership, or None when no fresh catalog is available."""
    entries = _read_cache()
    if entries is None:
        return None
    raw_id = model.strip().removeprefix("openrouter/")
    return any(e.get("id") == raw_id for e in entries)


def fetch_openrouter_models(
    *,
    force_refresh: bool = False,
    timeout: float = 8.0,
    allow_network: bool = True,
) -> list[dict]:
    """Return raw OpenRouter model entries, cached for CACHE_TTL_SECONDS."""
    global _last_source, _last_error
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            _last_source = "cache"
            _last_error = None
            return cached
    if not allow_network:
        stale = _read_cache(max_age=-1)
        _last_source = "cache" if stale is not None else "fallback"
        return stale if stale is not None else []
    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={
                "Accept": "application/json",
                # Default "Python-urllib/3.x" UA gets challenged/blocked by
                # some frontends while real clients pass; identify honestly.
                "User-Agent": "CoderAI/1.0 (+https://openrouter.ai)",
                **attribution_headers(),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        entries = payload.get("data", payload)
        if not isinstance(entries, list):
            _last_source = "stale"
            _last_error = "unexpected payload shape"
            return _read_cache(max_age=-1) or []
        entries = [e for e in entries if isinstance(e, dict) and e.get("id")]
        _write_cache(entries)
        _last_source = "live"
        _last_error = None
        return entries
    except Exception as exc:
        stale = _read_cache(max_age=-1)
        _last_source = "stale" if stale is not None else "fallback"
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        _last_error = f"{type(exc).__name__}: {detail[:140]}"
        return stale if stale is not None else []


def get_openrouter_free_models(
    *,
    force_refresh: bool = False,
    timeout: float = 8.0,
    allow_network: bool = True,
) -> list[tuple[str, str, str]]:
    """Return [(picker_name, description, category)] for free OpenRouter models.

    picker_name is prefixed with "openrouter/" so routing is unambiguous
    (e.g. "openrouter/deepseek/deepseek-r1:free" -> wire id strips prefix).
    """
    entries = fetch_openrouter_models(
        force_refresh=force_refresh, timeout=timeout, allow_network=allow_network
    )
    free = [e for e in entries if _is_free(e)]
    if not free and not entries:
        free = [{"id": mid, "name": mid} for mid in FALLBACK_FREE_IDS]
    out: list[tuple[str, str, str]] = []
    for e in free:
        raw_id = str(e.get("id") or "").strip()
        if not raw_id:
            continue
        name = f"openrouter/{raw_id}"
        label = str(e.get("name") or raw_id)
        ctx = e.get("context_length")
        desc = label if len(label) <= 90 else label[:87] + "..."
        if ctx:
            try:
                desc = f"{desc} ({int(ctx) // 1000}k ctx)"
            except (TypeError, ValueError):
                pass
        if isinstance(e.get("reasoning"), dict) and e["reasoning"].get("mandatory") is True:
            desc += " • reasoning required"
        out.append((name, desc, "OpenRouter :free"))
    out.sort(key=lambda t: t[0].lower())
    return out


def _format_age(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown age"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def catalog_status(free_count: int) -> str:
    """One-line human summary of where the picker list came from."""
    age: float | None = None
    try:
        age = time.time() - _cache_path().stat().st_mtime
    except OSError:
        age = None
    if _last_source == "live":
        return f"OpenRouter catalog: live ping • {free_count} free models"
    if _last_source == "cache":
        return (
            f"OpenRouter catalog: cached {_format_age(age)} • "
            f"{free_count} free models • `/models refresh` to re-ping provider"
        )
    reason = f" • ping failed ({_last_error})" if _last_error else ""
    if _last_source == "stale":
        return (
            f"OpenRouter catalog: provider unreachable, showing cached "
            f"{_format_age(age)} list • {free_count} free models{reason}"
        )
    return (
        f"OpenRouter catalog: offline fallback • {free_count} free models{reason} • "
        f"`/models refresh` to ping provider"
    )


def refresh_openrouter_catalog(*, timeout: float = 12.0) -> tuple[int, str]:
    """Force a live re-ping of the provider; returns (free_count, status_line)."""
    models = get_openrouter_free_models(force_refresh=True, timeout=timeout)
    return len(models), catalog_status(len(models))


def is_openrouter_model(model: str, base_url: str | None = None) -> bool:
    """True when this model id resolves to the OpenRouter endpoint."""
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith("openrouter/") or m.endswith(":free"):
        return True
    if base_url and "openrouter.ai" in base_url:
        return True
    if "/" not in m:
        return False
    try:
        from coderai.llm import resolve_model_provider_routing

        url, _ = resolve_model_provider_routing(model)
        return url is not None and "openrouter.ai" in url
    except Exception:
        return False

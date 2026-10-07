"""Pluggable web retrieval providers using the policy-enforcing HTTP transport."""

from __future__ import annotations

import abc
import asyncio
import base64
import copy
import html
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from coderai.network.security import NetworkSecurityError
from coderai.tools.web.common import safe_web_url
from coderai.tools.web.options import SearchFilters
from coderai.utils.aiohttp import HttpClient, get_http_client

USER_AGENT = "CoderAI/1.0"


@dataclass
class WebSearchSource:
    title: str
    url: str
    snippet: str | None = None
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"title": self.title, "url": self.url}
        if self.snippet:
            result["snippet"] = self.snippet
        if self.published_at:
            result["publishedAt"] = self.published_at
        return result


@dataclass
class WebSearchResult:
    query: str
    sources: list[WebSearchSource] = field(default_factory=list)
    content: str | None = None
    error: str | None = None
    fetched_at: float = 0.0
    from_cache: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "query": self.query,
            "sources": [s.to_dict() for s in self.sources],
        }
        if self.content:
            result["content"] = self.content
        if self.error:
            result["error"] = self.error
        result["fetchedAt"] = self.fetched_at
        result["fromCache"] = self.from_cache
        result["truncated"] = self.truncated
        return result


class WebSearchProvider(abc.ABC):
    client: HttpClient | None = None
    filters = SearchFilters()

    @property
    @abc.abstractmethod
    def id(self) -> str: ...

    @abc.abstractmethod
    def available(self) -> bool: ...

    @abc.abstractmethod
    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 15.0
    ) -> WebSearchResult: ...

    def _client(self) -> HttpClient:
        return self.client or get_http_client()

    def _get(self, url: str, **kwargs: Any) -> Any:
        response = self._client().get(url, **kwargs)
        if response.security_blocked:
            raise NetworkSecurityError(response.error or "Request blocked by policy.")
        return response

    async def _get_async(self, url: str, **kwargs: Any) -> Any:
        response = await self._client().get_async(url, **kwargs)
        if response.security_blocked:
            raise NetworkSecurityError(response.error or "Request blocked by policy.")
        return response

    def cache_scope(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "base_url": getattr(self, "base_url", ""),
            "model": getattr(self, "model", ""),
            "script": getattr(self, "script_path", ""),
            "filters": self.filters.to_dict(),
            "policy": vars(self._client().policy),
            "search_type": getattr(self, "search_type", ""),
            "credential_scope": hashlib.sha256(getattr(self, "api_key", "").encode()).hexdigest()[
                :16
            ],
        }


class CustomScriptSearchProvider(WebSearchProvider):
    id = "custom"

    def __init__(self, script_path: str) -> None:
        self.script_path = script_path

    def available(self) -> bool:
        return bool(
            self.script_path
            and (os.path.isfile(self.script_path) or shutil.which(self.script_path))
        )

    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 15.0
    ) -> WebSearchResult:
        from coderai.utils.subprocess_env import scrub_subprocess_env

        if self.filters.to_dict():
            return WebSearchResult(
                query=query, error="Custom scripts do not support structured search filters."
            )
        try:
            proc = subprocess.run(
                [self.script_path, query],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=scrub_subprocess_env(dict(os.environ)),
            )
            stdout = proc.stdout.strip()[:100_000]
            if proc.returncode != 0 or not stdout:
                return WebSearchResult(
                    query=query,
                    error=f"Custom search exited with code {proc.returncode}: {(proc.stderr or stdout)[:1000]}",
                )
            # Structured output supplies usable citations; plain scripts remain compatible.
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict) and isinstance(parsed.get("sources"), list):
                    return WebSearchResult(
                        query=query,
                        content=parsed.get("content"),
                        sources=_map_sources(parsed["sources"])[:max_results],
                    )
            except ValueError:
                pass
            return WebSearchResult(query=query, content=stdout)
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Custom search tool error: {exc}")


class _KeyedSearchProvider(WebSearchProvider):
    key_env = ""

    def __init__(self, api_key: str | None, base_url: str) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")

    @property
    def api_key(self) -> str:
        return self._api_key if self._api_key is not None else os.environ.get(self.key_env, "")

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            raise ValueError(f"{self.id.title()} API key not configured (set {self.key_env}).")
        response = self._client().post(
            f"{self.base_url}/{path}",
            json_data=payload,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        if response.security_blocked:
            raise NetworkSecurityError(response.error or "Request blocked by policy.")
        if not response.ok:
            raise ValueError(
                f"{self.id} search failed: {response.error or f'HTTP {response.status_code}'}"
            )
        parsed = json.loads(response.text)
        if not isinstance(parsed, dict):
            raise ValueError("Provider returned a non-object JSON response.")
        return parsed


def _map_sources(items: list[Any]) -> list[WebSearchSource]:
    sources = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = safe_web_url(item.get("url") or "")
        if not url:
            continue
        sources.append(
            WebSearchSource(
                title=item.get("title") or url,
                url=url,
                snippet=item.get("snippet") or item.get("text"),
                published_at=item.get("publishedDate") or item.get("date") or item.get("page_age"),
            )
        )
    return sources


class ExaSearchProvider(_KeyedSearchProvider):
    id = "exa"
    key_env = "EXA_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.exa.ai",
        search_type: str = "auto",
    ) -> None:
        super().__init__(api_key, base_url)
        self.search_type = search_type

    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 15.0
    ) -> WebSearchResult:
        try:
            if self.filters.language:
                raise ValueError("Language filtering requires the Perplexity provider.")
            payload: dict[str, Any] = {
                "query": query,
                "numResults": max_results,
                "type": self.search_type,
                "contents": {"highlights": True},
            }
            for name, value in (
                ("includeDomains", self.filters.include_domains),
                ("excludeDomains", self.filters.exclude_domains),
                ("startPublishedDate", self.filters.start_date),
                ("endPublishedDate", self.filters.end_date),
                ("userLocation", self.filters.region),
            ):
                if value:
                    payload[name] = value
            data = self._post("search", payload, timeout_seconds, {"x-api-key": self.api_key})
            items = data.get("results") or []
            for item in items:
                if isinstance(item, dict) and item.get("highlights"):
                    item["snippet"] = " ... ".join(item["highlights"])
            return WebSearchResult(query=query, sources=_map_sources(items)[:max_results])
        except NetworkSecurityError:
            raise
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Exa search error: {exc}")


class PerplexitySearchProvider(_KeyedSearchProvider):
    id = "perplexity"
    key_env = "PERPLEXITY_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.perplexity.ai",
        model: str = "sonar",
    ) -> None:
        super().__init__(api_key, base_url)
        self.model = model

    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 15.0
    ) -> WebSearchResult:
        try:
            if self.filters.to_dict():
                payload: dict[str, Any] = {
                    "query": query,
                    "max_results": min(max_results, 20),
                    "max_tokens_per_page": 512,
                }
                # Perplexity accepts include OR exclude; exclusions are enforced locally as well.
                domains = list(self.filters.include_domains) or [
                    "-" + d for d in self.filters.exclude_domains
                ]
                if domains:
                    payload["search_domain_filter"] = [d.removeprefix("*.") for d in domains]
                if self.filters.language:
                    payload["search_language_filter"] = [self.filters.language]
                if self.filters.region:
                    payload["country"] = self.filters.region
                for name, value in (
                    ("search_after_date_filter", self.filters.start_date),
                    ("search_before_date_filter", self.filters.end_date),
                ):
                    if value:
                        payload[name] = datetime.fromisoformat(value).strftime("%m/%d/%Y")
                data = self._post("search", payload, timeout_seconds)
                return WebSearchResult(
                    query=query, sources=_map_sources(data.get("results") or [])[:max_results]
                )
            data = self._post(
                "chat/completions",
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": query}],
                    "max_tokens": 1024,
                },
                timeout_seconds,
            )
            choices = data.get("choices") or [{}]
            answer = choices[0].get("message", {}).get("content") or None
            sources = _map_sources(data.get("search_results") or [])
            if not sources:
                sources = [
                    WebSearchSource(title=url, url=url)
                    for url in data.get("citations") or []
                    if safe_web_url(url)
                ]
            return WebSearchResult(query=query, content=answer, sources=sources[:max_results])
        except NetworkSecurityError:
            raise
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Perplexity search error: {exc}")


class DeepSeekSearchProvider(_KeyedSearchProvider):
    """Native search is a Messages model turn, not a dedicated /search endpoint."""

    id = "deepseek"
    key_env = "DEEPSEEK_API_KEY"

    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-flash"
    ) -> None:
        super().__init__(
            api_key,
            base_url
            or os.environ.get("DEEPSEEK_SEARCH_BASE_URL", "https://api.deepseek.com/anthropic/v1"),
        )
        self.model = model

    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 15.0
    ) -> WebSearchResult:
        try:
            if self.filters.to_dict():
                raise ValueError("Structured filters require Exa or Perplexity.")
            data = self._post(
                "messages",
                {
                    "model": self.model,
                    "max_tokens": 4096,
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": f"Search the web for: {query}"}],
                        }
                    ],
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
                },
                timeout_seconds,
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            )
            blocks = data.get("content") or []
            snippets: dict[str, str] = {}
            answer: list[str] = []
            items: list[Any] = []
            found_search = False
            for block in blocks:
                if block.get("type") == "text":
                    answer.append(block.get("text") or "")
                    for citation in block.get("citations") or []:
                        if citation.get("url") and citation.get("cited_text"):
                            snippets.setdefault(citation["url"], citation["cited_text"])
                elif block.get("type") == "web_search_tool_result":
                    found_search = True
                    content = block.get("content") or []
                    if isinstance(content, dict):
                        raise ValueError(
                            f"Native search failed: {content.get('error_code', 'unknown error')}"
                        )
                    items.extend(
                        item for item in content if item.get("type") == "web_search_result"
                    )
            if not found_search:
                raise ValueError("DeepSeek returned no native search result blocks.")
            sources = _map_sources(items)
            for source in sources:
                source.snippet = snippets.get(source.url) or source.snippet
            return WebSearchResult(
                query=query,
                sources=sources[:max_results],
                content="\n\n".join(answer).strip() or None,
            )
        except NetworkSecurityError:
            raise
        except Exception as exc:
            return WebSearchResult(query=query, error=f"DeepSeek search error: {exc}")


class HttpSearchProvider(WebSearchProvider):
    """Free HTTP search. Date/language filters require a structured search API."""

    id = "http"

    def available(self) -> bool:
        return True

    def _query(self, query: str) -> str:
        if self.filters.start_date or self.filters.end_date or self.filters.language:
            raise ValueError("Date and language filters require Exa (dates) or Perplexity.")
        if self.filters.include_domains:
            query += (
                " ("
                + " OR ".join("site:" + d.removeprefix("*.") for d in self.filters.include_domains)
                + ")"
            )
        query += "".join(" -site:" + d.removeprefix("*.") for d in self.filters.exclude_domains)
        return query

    @staticmethod
    def _instant(query: str, text: str, max_results: int) -> WebSearchResult | None:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        sources: list[WebSearchSource] = []
        if data.get("AbstractURL"):
            sources.append(
                WebSearchSource(
                    title=data.get("Heading") or query,
                    url=data["AbstractURL"],
                    snippet=data.get("AbstractText"),
                )
            )

        def topics(items: list[Any]) -> None:
            for item in items:
                if isinstance(item, dict):
                    if item.get("FirstURL"):
                        sources.append(
                            WebSearchSource(
                                title=item.get("Text") or "Topic",
                                url=item["FirstURL"],
                                snippet=item.get("Text"),
                            )
                        )
                    topics(item.get("Topics") or [])

        topics(data.get("RelatedTopics") or [])
        if sources or data.get("AbstractText"):
            return WebSearchResult(
                query=query, content=data.get("AbstractText") or None, sources=sources[:max_results]
            )
        return None

    def search(
        self, query: str, max_results: int = 8, timeout_seconds: float = 10.0
    ) -> WebSearchResult:
        effective = self._query(query)
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": effective, "format": "json", "no_html": 1, "skip_disambig": 1}
        )
        response = self._get(url, timeout=timeout_seconds, use_cache=False)
        result = (
            self._instant(query, response.text, max_results)
            if response.ok and not self.filters.region
            else None
        )
        if not result:
            result = self._search_fallback(effective, max_results, timeout_seconds)
        result.query = query
        return result

    async def search_async(
        self, query: str, max_results: int = 8, timeout_seconds: float = 10.0
    ) -> WebSearchResult:
        effective = self._query(query)
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": effective, "format": "json", "no_html": 1, "skip_disambig": 1}
        )
        response = await self._get_async(url, timeout=timeout_seconds, use_cache=False)
        result = (
            self._instant(query, response.text, max_results)
            if response.ok and not self.filters.region
            else None
        )
        if result:
            return result
        result = await asyncio.to_thread(
            self._search_fallback, effective, max_results, timeout_seconds
        )
        result.query = query
        return result

    def _search_fallback(
        self,
        query: str,
        max_results: int,
        timeout_seconds: float,
    ) -> WebSearchResult:
        """Search HTML endpoints when the primary DuckDuckGo request has no results."""
        sources: list[WebSearchSource] = []
        seen_urls: set[str] = set()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        # Yahoo is the first HTML fallback. Country-specific requests use Bing.
        try:
            url = f"https://search.yahoo.com/search?p={urllib.parse.quote(query)}"
            resp = (
                None
                if self.filters.region
                else self._get(url, headers=headers, timeout=timeout_seconds, use_cache=False)
            )
            if resp is not None and resp.status_code == 200:
                items = re.findall(r"<li><div class=\"[^\"]*dd [^\"]*\"[\s\S]*?</li>", resp.text)
                for it in items:
                    link_m = re.search(r"href=\"(https?://[^\"]+)\"", it)
                    title_m = re.search(r"<h3[^>]*>([\s\S]*?)</h3>", it) or re.search(
                        r"<h4[^>]*>([\s\S]*?)</h4>", it
                    )
                    snippet_m = re.search(
                        r"<div class=\"compText[^\"]*\"[^>]*>[\s\S]*?<p[^>]*>([\s\S]*?)</p>",
                        it,
                    ) or re.search(r"<p[^>]*>([\s\S]*?)</p>", it)
                    if link_m and title_m:
                        raw_url = link_m.group(1)
                        target_url = raw_url
                        if "r.search.yahoo.com" in raw_url:
                            m = re.search(r"/RU=([^/]+)/", raw_url)
                            if m:
                                target_url = urllib.parse.unquote(m.group(1))
                        target_url = safe_web_url(html.unescape(target_url))
                        host = urllib.parse.urlsplit(target_url).hostname or ""
                        if (
                            not target_url
                            or host == "yahoo.com"
                            or host.endswith(".yahoo.com")
                            or target_url in seen_urls
                            or not self.filters.allows_url(target_url)
                        ):
                            continue
                        title = html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1)).strip())
                        snippet = (
                            html.unescape(re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip())
                            if snippet_m
                            else ""
                        )
                        snippet = re.sub(r"^[\w\s,0-9]+·\s*", "", snippet).strip()
                        if title and target_url:
                            seen_urls.add(target_url)
                            sources.append(
                                WebSearchSource(
                                    title=title, url=target_url, snippet=snippet or None
                                )
                            )
                            if len(sources) >= max_results:
                                break
        except NetworkSecurityError:
            raise
        except Exception:
            pass

        # Fill remaining results from Bing.
        if len(sources) < max_results:
            try:
                b_url = "https://www.bing.com/search?" + urllib.parse.urlencode(
                    {
                        "q": query,
                        **({"cc": self.filters.region.lower()} if self.filters.region else {}),
                    }
                )
                resp = self._get(b_url, headers=headers, timeout=timeout_seconds, use_cache=False)
                if resp.status_code == 200:
                    matches = re.findall(r"<li class=\"b_algo\"[\s\S]*?</li>", resp.text)
                    for m in matches:
                        h2_match = re.search(
                            r"<h2[^>]*>[\s\S]*?<a[^>]+href=\"([^\"]+)\"[^>]*>([\s\S]*?)</a>", m
                        )
                        snippet_match = re.search(
                            r"<div class=\"b_caption\"[\s\S]*?<p[^>]*>([\s\S]*?)</p>", m
                        ) or re.search(r"<p[^>]*>([\s\S]*?)</p>", m)
                        if h2_match:
                            raw_url = html.unescape(h2_match.group(1))
                            target_url = raw_url
                            if "bing.com/ck/a?" in raw_url:
                                u_match = re.search(r"[?&]u=([a-zA-Z0-9_-]+)", raw_url)
                                if u_match:
                                    u_val = u_match.group(1)
                                    if u_val.startswith("a1"):
                                        b64 = u_val[2:]
                                        b64 += "=" * ((4 - len(b64) % 4) % 4)
                                        try:
                                            target_url = base64.urlsafe_b64decode(b64).decode(
                                                "utf-8", errors="ignore"
                                            )
                                        except Exception:
                                            pass
                            target_url = safe_web_url(target_url)
                            host = urllib.parse.urlsplit(target_url).hostname or ""
                            if (
                                not target_url
                                or host == "bing.com"
                                or host.endswith(".bing.com")
                                or target_url in seen_urls
                                or not self.filters.allows_url(target_url)
                            ):
                                continue
                            title = html.unescape(re.sub(r"<[^>]+>", "", h2_match.group(2)).strip())
                            snippet = (
                                html.unescape(
                                    re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                                )
                                if snippet_match
                                else ""
                            )
                            snippet = re.sub(r"^[\w\s,0-9]+·\s*", "", snippet).strip()
                            if title and target_url:
                                seen_urls.add(target_url)
                                sources.append(
                                    WebSearchSource(
                                        title=title, url=target_url, snippet=snippet or None
                                    )
                                )
                                if len(sources) >= max_results:
                                    break
            except NetworkSecurityError:
                raise
            except Exception:
                pass

        if sources:
            return WebSearchResult(query=query, sources=sources[:max_results])
        return WebSearchResult(
            query=query,
            error="Search engines returned no readable results; they may be unavailable or blocking automated access.",
        )


_PROVIDERS: dict[str, WebSearchProvider] = {
    "exa": ExaSearchProvider(),
    "perplexity": PerplexitySearchProvider(),
    "deepseek": DeepSeekSearchProvider(),
    "http": HttpSearchProvider(),
}


def register_web_search_provider(provider: WebSearchProvider) -> None:
    _PROVIDERS[provider.id.lower()] = provider


def list_web_search_providers() -> list[str]:
    return sorted(_PROVIDERS)


def resolve_web_search_provider(
    name: str | None = None,
    *,
    settings: dict[str, Any] | None = None,
    filters: SearchFilters | None = None,
    client: HttpClient | None = None,
) -> WebSearchProvider:
    settings = settings or {}
    env = {**(settings.get("env") or {}), **os.environ}
    custom_tool = env.get("CODERAI_WEB_SEARCH_TOOL") or settings.get("webSearchTool")
    preferred = name or env.get("CODERAI_WEB_SEARCH_PROVIDER") or settings.get("webSearchProvider")

    def configured(provider_id: str) -> WebSearchProvider:
        if provider_id == "custom":
            if not custom_tool:
                raise ValueError("Custom search executable is not configured.")
            provider: WebSearchProvider = CustomScriptSearchProvider(custom_tool)
        elif provider_id in _PROVIDERS:
            provider = copy.copy(_PROVIDERS[provider_id])
            if isinstance(provider, _KeyedSearchProvider):
                provider._api_key = (
                    provider.api_key
                    if provider._api_key is not None
                    else env.get(provider.key_env, "")
                )
            if isinstance(provider, DeepSeekSearchProvider) and env.get("DEEPSEEK_SEARCH_BASE_URL"):
                provider.base_url = env["DEEPSEEK_SEARCH_BASE_URL"].rstrip("/")
        else:
            raise ValueError(f"Unknown web search provider: {provider_id}")
        if isinstance(provider, WebSearchProvider):
            provider.filters = filters or SearchFilters()
            provider.client = client
        elif filters and filters.to_dict():
            raise ValueError(f"Provider {provider_id} does not support structured search filters.")
        if not provider.available():
            raise ValueError(f"Web search provider '{provider_id}' is not configured or available.")
        return provider

    if preferred:
        return configured(preferred.lower().strip())
    if custom_tool:
        return configured("custom")
    order = (
        ("perplexity", "exa", "deepseek")
        if filters and filters.language
        else ("exa", "perplexity", "deepseek")
    )
    for provider_id in order:
        try:
            return configured(provider_id)
        except ValueError:
            continue
    return configured("http")

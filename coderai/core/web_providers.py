"""Pluggable Web Search & Fetch Providers.

Implements modular providers for Exa, Perplexity, DeepSeek, Custom scripts, and generic HTTP search/fetch
following DeepSeek Harness dsh-web specifications.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from typing import Any
import requests

from coderai.core.network.cache import get_search_cache
from coderai.core.network.client import get_http_client

logger = logging.getLogger(__name__)

USER_AGENT = "CoderAI/1.0"


@dataclass
class WebSearchSource:
    """A single source result returned by a search provider."""

    title: str
    url: str
    snippet: str | None = None
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title, "url": self.url}
        if self.snippet:
            d["snippet"] = self.snippet
        if self.published_at:
            d["publishedAt"] = self.published_at
        return d


@dataclass
class WebSearchResult:
    """Aggregated output from a web search query."""

    query: str
    sources: list[WebSearchSource] = field(default_factory=list)
    content: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "query": self.query,
            "sources": [s.to_dict() for s in self.sources],
        }
        if self.content:
            d["content"] = self.content
        if self.error:
            d["error"] = self.error
        return d


class WebSearchProvider(abc.ABC):
    """Abstract base class for all web search providers."""

    @property
    @abc.abstractmethod
    def id(self) -> str:
        """Provider identifier (e.g. 'exa', 'perplexity', 'deepseek', 'http', 'custom')."""
        ...

    @abc.abstractmethod
    def available(self) -> bool:
        """Return True if required API keys or endpoints are configured."""
        ...

    @abc.abstractmethod
    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 15.0,
    ) -> WebSearchResult:
        """Execute a web search query and return normalized results."""
        ...


class CustomScriptSearchProvider(WebSearchProvider):
    """Custom search script provider executing an external script specified by path or CODERAI_WEB_SEARCH_TOOL."""

    def __init__(self, script_path: str) -> None:
        self.script_path = script_path

    @property
    def id(self) -> str:
        return "custom"

    def available(self) -> bool:
        return bool(
            self.script_path
            and (os.path.isfile(self.script_path) or shutil.which(self.script_path))
        )

    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 15.0,
    ) -> WebSearchResult:
        try:
            proc = subprocess.run(
                [self.script_path, query],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            stdout = proc.stdout.strip()
            if proc.returncode == 0 and stdout:
                return WebSearchResult(
                    query=query,
                    content=stdout,
                    sources=[WebSearchSource(title="Custom Search Output", url="", snippet=stdout)],
                )
            return WebSearchResult(
                query=query,
                error=f"Custom search tool exited with code {proc.returncode}: {proc.stderr or stdout}",
            )
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Custom search tool error: {exc}")


class ExaSearchProvider(WebSearchProvider):
    """Exa neural & keyword web search provider."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.exa.ai",
        search_type: str = "auto",
    ) -> None:
        self.api_key = api_key or os.environ.get("EXA_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.search_type = search_type

    @property
    def id(self) -> str:
        return "exa"

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 15.0,
    ) -> WebSearchResult:
        if not self.available():
            return WebSearchResult(query=query, error="Exa API key not configured (set EXA_API_KEY).")

        url = f"{self.base_url}/search"
        headers = {
            "x-api-key": self.api_key,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "numResults": max_results,
            "type": self.search_type,
            "contents": {"highlights": {"numSentences": 2}},
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
            if resp.status_code != 200:
                return WebSearchResult(
                    query=query, error=f"Exa search failed with status {resp.status_code}: {resp.text}"
                )
            data = resp.json()
            results = data.get("results") or []
            sources: list[WebSearchSource] = []
            for r in results:
                highlights = r.get("highlights") or []
                snippet = " ... ".join(highlights) if highlights else r.get("text")
                sources.append(
                    WebSearchSource(
                        title=r.get("title") or r.get("url") or "Untitled",
                        url=r.get("url", ""),
                        snippet=snippet,
                        published_at=r.get("publishedDate"),
                    )
                )
            return WebSearchResult(query=query, sources=sources[:max_results])
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Exa search error: {exc}")


class PerplexitySearchProvider(WebSearchProvider):
    """Perplexity AI web search provider with generated answers and citations."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.perplexity.ai",
        model: str = "sonar",
    ) -> None:
        self.api_key = api_key or os.environ.get("PERPLEXITY_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model

    @property
    def id(self) -> str:
        return "perplexity"

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 15.0,
    ) -> WebSearchResult:
        if not self.available():
            return WebSearchResult(
                query=query, error="Perplexity API key not configured (set PERPLEXITY_API_KEY)."
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 1024,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
            if resp.status_code != 200:
                return WebSearchResult(
                    query=query,
                    error=f"Perplexity search failed with status {resp.status_code}: {resp.text}",
                )
            data = resp.json()
            choices = data.get("choices") or [{}]
            answer = choices[0].get("message", {}).get("content", "")
            citations = data.get("citations") or []
            search_results = data.get("search_results") or []

            sources: list[WebSearchSource] = []
            if search_results:
                for sr in search_results:
                    sources.append(
                        WebSearchSource(
                            title=sr.get("title") or sr.get("url") or "Source",
                            url=sr.get("url", ""),
                            snippet=sr.get("snippet"),
                        )
                    )
            elif citations:
                for cite in citations:
                    sources.append(
                        WebSearchSource(
                            title=cite,
                            url=cite,
                            snippet=None,
                        )
                    )

            return WebSearchResult(
                query=query,
                content=answer.strip() if answer else None,
                sources=sources[:max_results],
            )
        except Exception as exc:
            return WebSearchResult(query=query, error=f"Perplexity search error: {exc}")


class DeepSeekSearchProvider(WebSearchProvider):
    """DeepSeek native search API provider."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    @property
    def id(self) -> str:
        return "deepseek"

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 15.0,
    ) -> WebSearchResult:
        if not self.available():
            return WebSearchResult(
                query=query, error="DeepSeek API key not configured (set DEEPSEEK_API_KEY)."
            )

        url = f"{self.base_url}/search"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                url,
                json={"query": query, "limit": max_results},
                headers=headers,
                timeout=timeout_seconds,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results") or []
                sources = [
                    WebSearchSource(
                        title=r.get("title", "Untitled"),
                        url=r.get("url", ""),
                        snippet=r.get("snippet"),
                    )
                    for r in results
                ]
                return WebSearchResult(query=query, sources=sources[:max_results])
            return WebSearchResult(
                query=query, error=f"DeepSeek search API returned status {resp.status_code}"
            )
        except Exception as exc:
            return WebSearchResult(query=query, error=f"DeepSeek search error: {exc}")


class HttpSearchProvider(WebSearchProvider):
    """Generic HTTP multi-engine web search provider (Yahoo + Bing + DuckDuckGo) with caching."""

    @property
    def id(self) -> str:
        return "http"

    def available(self) -> bool:
        return True

    def search(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 10.0,
    ) -> WebSearchResult:
        cache = get_search_cache()
        cached = cache.get(f"search:{query}")
        if cached:
            return cached

        # 1. DuckDuckGo Instant Answer / Mock Client Check
        try:
            client = get_http_client()
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
            resp = client.get(ddg_url, timeout=timeout_seconds)
            if resp.ok and resp.text:
                try:
                    data = json.loads(resp.text)
                    abstract = data.get("AbstractText", "")
                    ddg_sources: list[WebSearchSource] = []
                    for topic in data.get("RelatedTopics", []):
                        if isinstance(topic, dict) and "FirstURL" in topic:
                            u = topic.get("FirstURL", "")
                            if u:
                                ddg_sources.append(
                                    WebSearchSource(
                                        title=topic.get("Text", "Topic"),
                                        url=u,
                                        snippet=topic.get("Text"),
                                    )
                                )
                    if abstract or ddg_sources:
                        res = WebSearchResult(
                            query=query,
                            content=abstract or None,
                            sources=ddg_sources[:max_results],
                        )
                        cache.set(f"search:{query}", res)
                        return res
                except Exception:
                    pass
        except Exception:
            pass

    async def search_async(
        self,
        query: str,
        max_results: int = 8,
        timeout_seconds: float = 10.0,
    ) -> WebSearchResult:
        cache = get_search_cache()
        cached = cache.get(f"search:{query}")
        if cached:
            return cached

        try:
            client = get_http_client()
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
            resp = await client.get_async(ddg_url, timeout=timeout_seconds)
            if resp.ok and resp.text:
                try:
                    data = json.loads(resp.text)
                    abstract = data.get("AbstractText", "")
                    ddg_sources: list[WebSearchSource] = []
                    for topic in data.get("RelatedTopics", []):
                        if isinstance(topic, dict) and "FirstURL" in topic:
                            u = topic.get("FirstURL", "")
                            if u:
                                ddg_sources.append(
                                    WebSearchSource(
                                        title=topic.get("Text", "Topic"),
                                        url=u,
                                        snippet=topic.get("Text"),
                                    )
                                )
                    if abstract or ddg_sources:
                        res = WebSearchResult(
                            query=query,
                            content=abstract or None,
                            sources=ddg_sources[:max_results],
                        )
                        cache.set(f"search:{query}", res)
                        return res
                except Exception:
                    pass
        except Exception:
            pass

        return await asyncio.to_thread(self.search, query, max_results, timeout_seconds)

        sources: list[WebSearchSource] = []
        seen_urls: set[str] = set()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        # 2. Engine 1: Yahoo Web Search
        try:
            url = f"https://search.yahoo.com/search?p={urllib.parse.quote(query)}"
            resp = requests.get(url, headers=headers, timeout=timeout_seconds)
            if resp.status_code == 200:
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
                        if "yahoo.com" in target_url or target_url in seen_urls:
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
        except Exception:
            pass

        # 2. Engine 2: Bing Web Search
        if len(sources) < max_results:
            try:
                b_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
                resp = requests.get(b_url, headers=headers, timeout=timeout_seconds)
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
                            if (
                                "r.bing.com" in target_url
                                or "bing.com" in target_url
                                or target_url in seen_urls
                            ):
                                continue
                            title = html.unescape(
                                re.sub(r"<[^>]+>", "", h2_match.group(2)).strip()
                            )
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
            except Exception:
                pass

        # 3. Engine 3: DuckDuckGo instant answer / abstract fallback
        if not sources:
            try:
                ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
                resp = requests.get(ddg_url, headers=headers, timeout=timeout_seconds)
                if resp.status_code == 200:
                    data = resp.json()
                    abstract = data.get("AbstractText", "")
                    for topic in data.get("RelatedTopics", []):
                        if isinstance(topic, dict) and "FirstURL" in topic:
                            u = topic.get("FirstURL", "")
                            if u and u not in seen_urls:
                                seen_urls.add(u)
                                sources.append(
                                    WebSearchSource(
                                        title=topic.get("Text", "Topic"),
                                        url=u,
                                        snippet=topic.get("Text"),
                                    )
                                )
                    if abstract or sources:
                        res = WebSearchResult(
                            query=query,
                            content=abstract or None,
                            sources=sources[:max_results],
                        )
                        cache.set(f"search:{query}", res)
                        return res
            except Exception:
                pass

        if sources:
            res = WebSearchResult(query=query, sources=sources[:max_results])
            cache.set(f"search:{query}", res)
            return res

        return WebSearchResult(query=query, sources=[], error="No search results found.")


# Registry Management
_PROVIDERS: dict[str, WebSearchProvider] = {
    "exa": ExaSearchProvider(),
    "perplexity": PerplexitySearchProvider(),
    "deepseek": DeepSeekSearchProvider(),
    "http": HttpSearchProvider(),
}


def register_web_search_provider(provider: WebSearchProvider) -> None:
    _PROVIDERS[provider.id.lower()] = provider


def list_web_search_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def resolve_web_search_provider(name: str | None = None) -> WebSearchProvider:
    """Resolve the active WebSearchProvider based on preferences, env vars, or availability."""
    if name and name.lower() in _PROVIDERS:
        return _PROVIDERS[name.lower()]

    custom_tool = os.environ.get("CODERAI_WEB_SEARCH_TOOL")
    if custom_tool and (os.path.isfile(custom_tool) or shutil.which(custom_tool)):
        return CustomScriptSearchProvider(custom_tool)

    preferred = os.environ.get("CODERAI_WEB_SEARCH_PROVIDER")
    if preferred and preferred.lower() in _PROVIDERS:
        p = _PROVIDERS[preferred.lower()]
        if p.available():
            return p

    # Fallback to first available provider
    for p_id in ("exa", "perplexity", "deepseek"):
        p = _PROVIDERS[p_id]
        if p.available():
            return p

    # Default fallback
    return _PROVIDERS["http"]

"""Web search tool using DuckDuckGo."""

import logging
import re
from typing import Any, Dict
from urllib.parse import quote_plus

import aiohttp
from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)

# Module-level session for reuse across requests (#8)
_web_session: aiohttp.ClientSession | None = None


async def _get_web_session() -> aiohttp.ClientSession:
    """Get or create a shared aiohttp session."""
    global _web_session
    if _web_session is None or _web_session.closed:
        _web_session = aiohttp.ClientSession()
    return _web_session


class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query string")
    num_results: int = Field(5, description="Number of results to return (default: 5, max: 10)")


class WebSearchTool(Tool):
    """Tool for searching the web using DuckDuckGo."""

    name = "web_search"
    description = "Search the web for information using DuckDuckGo"

    # DuckDuckGo Instant Answer API (light JSON endpoint)
    DDG_API_URL = "https://api.duckduckgo.com/"
    # DuckDuckGo HTML search (more comprehensive but needs parsing)
    DDG_HTML_URL = "https://html.duckduckgo.com/html/"
    parameters_model = WebSearchParams

    async def execute(self, query: str, num_results: int = 5) -> Dict[str, Any]:
        """Execute web search.

        Uses DuckDuckGo Instant Answer API first, falls back to HTML search.
        """
        num_results = min(num_results, 10)

        try:
            # Try Instant Answer API first (structured JSON, very reliable)
            instant_result = await self._search_instant_answer(query)
            if instant_result and instant_result.get("results"):
                return instant_result

            # Fall back to HTML search (broader but less structured)
            html_result = await self._search_html(query, num_results)
            if html_result and html_result.get("results"):
                return html_result

            # If both failed, return a fallback
            return {
                "success": True,
                "results": [],
                "query": query,
                "note": "No results found. Try a different query.",
                "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
            }

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return {
                "success": False,
                "error": str(e),
                "hint": "Web search failed. You can still help the user based on your training data.",
                "search_url": f"https://duckduckgo.com/?q={quote_plus(query)}",
            }

    async def _search_instant_answer(self, query: str) -> Dict[str, Any]:
        """Search using DuckDuckGo Instant Answer API (JSON, reliable)."""
        try:
            params = {
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.DDG_API_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "CoderAI/0.1"},
                ) as response:
                    if response.status != 200:
                        return None

                    data = await response.json(content_type=None)

            results = []

            # Abstract (Wikipedia-style summary)
            if data.get("Abstract"):
                results.append({
                    "title": data.get("Heading", "Summary"),
                    "snippet": data["Abstract"],
                    "url": data.get("AbstractURL", ""),
                    "source": data.get("AbstractSource", ""),
                })

            # Answer (calculator, definitions, etc.)
            if data.get("Answer"):
                results.append({
                    "title": "Answer",
                    "snippet": str(data["Answer"]),
                    "url": "",
                    "source": "DuckDuckGo",
                })

            # Related topics
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title": topic.get("Text", "")[:80],
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                        "source": "DuckDuckGo",
                    })

            if results:
                return {
                    "success": True,
                    "results": results,
                    "query": query,
                    "source": "DuckDuckGo Instant Answers",
                }

            return None

        except Exception as e:
            logger.debug(f"Instant answer search failed: {e}")
            return None

    async def _search_html(self, query: str, num_results: int) -> Dict[str, Any]:
        """Search using DuckDuckGo HTML endpoint.

        Parses the HTML response to extract search results.
        This is more fragile than the API but provides broader results.
        """
        try:
            data = {"q": query}

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.DDG_HTML_URL,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                ) as response:
                    if response.status != 200:
                        return None

                    html = await response.text()

            results = self._parse_html_results(html, num_results)

            if results:
                return {
                    "success": True,
                    "results": results,
                    "query": query,
                    "source": "DuckDuckGo HTML",
                }

            return None

        except Exception as e:
            logger.debug(f"HTML search failed: {e}")
            return None

    def _parse_html_results(self, html: str, max_results: int) -> list:
        """Parse DuckDuckGo HTML search results.

        Uses simple string parsing with a regex fallback.
        """
        try:
            results = self._parse_html_split(html, max_results)
            if results:
                return results
        except Exception as e:
            logger.debug(f"Primary HTML parser failed: {e}")

        # Fallback: regex-based extraction
        try:
            return self._parse_html_regex(html, max_results)
        except Exception as e:
            logger.warning(f"All HTML parsers failed: {e}")
            return []

    def _parse_html_split(self, html: str, max_results: int) -> list:
        """Primary parser using string splitting."""
        results = []

        # Look for result blocks: class="result__body"
        parts = html.split('class="result__body"')

        for part in parts[1 : max_results + 1]:
            try:
                result = {}

                # Extract URL
                if 'class="result__url"' in part:
                    url_start = part.index('href="') + 6
                    url_end = part.index('"', url_start)
                    url = part[url_start:url_end]
                    if url.startswith("//"):
                        url = "https:" + url
                    result["url"] = url

                # Extract title
                if 'class="result__a"' in part:
                    title_start = part.index('class="result__a"')
                    title_tag_end = part.index(">", title_start) + 1
                    title_end = part.index("<", title_tag_end)
                    result["title"] = part[title_tag_end:title_end].strip()

                # Extract snippet
                if 'class="result__snippet"' in part:
                    snip_start = part.index('class="result__snippet"')
                    snip_tag_end = part.index(">", snip_start) + 1
                    snip_end = part.index("</", snip_tag_end)
                    snippet = part[snip_tag_end:snip_end].strip()
                    # Remove HTML tags from snippet
                    snippet = re.sub(r"<[^>]+>", "", snippet)
                    result["snippet"] = snippet

                if result.get("title") or result.get("snippet"):
                    results.append(result)

            except (ValueError, IndexError):
                continue

        return results

    def _parse_html_regex(self, html: str, max_results: int) -> list:
        """Fallback regex-based parser for when the split approach fails."""
        results = []

        # Match title + URL from anchor tags in result blocks
        link_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</(?:td|div|span)',
            re.DOTALL,
        )

        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (url, title) in enumerate(links[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            url = ("https:" + url) if url.startswith("//") else url
            result = {"url": url, "title": title_clean}
            if i < len(snippets):
                result["snippet"] = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            results.append(result)

        return results

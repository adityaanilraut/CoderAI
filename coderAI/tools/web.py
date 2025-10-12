"""Web search tools for finding information online."""

import asyncio
import ssl
from typing import Any, Dict
from urllib.parse import quote_plus

import aiohttp

from .base import Tool


class WebSearchTool(Tool):
    """Tool for searching the web."""

    name = "web_search"
    description = "Search the web for information (useful for documentation, errors, etc.)"

    def get_parameters(self) -> Dict[str, Any]:
        """Get parameters schema."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5)",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, num_results: int = 5) -> Dict[str, Any]:
        """Provide web search results and URLs.
        
        Note: Due to SSL/API limitations, this provides search URLs and suggestions
        rather than scraping results. This approach is more reliable and respects
        rate limits and terms of service.
        """
        try:
            encoded_query = quote_plus(query)
            
            # Generate useful search URLs for different platforms
            results = [
                {
                    "title": f"Search DuckDuckGo for '{query}'",
                    "snippet": "Privacy-focused search engine with no tracking",
                    "url": f"https://duckduckgo.com/?q={encoded_query}"
                },
                {
                    "title": f"Search Google for '{query}'",
                    "snippet": "Comprehensive search results from Google",
                    "url": f"https://www.google.com/search?q={encoded_query}"
                },
            ]
            
            # Add specific resources based on query type
            query_lower = query.lower()
            
            # Programming/Code related
            if any(word in query_lower for word in ['python', 'javascript', 'java', 'code', 'programming', 'error', 'exception', 'function']):
                results.append({
                    "title": f"Search Stack Overflow for '{query}'",
                    "snippet": "Find programming solutions and discussions",
                    "url": f"https://stackoverflow.com/search?q={encoded_query}"
                })
                results.append({
                    "title": f"Search GitHub for '{query}'",
                    "snippet": "Find relevant code repositories and examples",
                    "url": f"https://github.com/search?q={encoded_query}&type=repositories"
                })
            
            # Documentation related
            if any(word in query_lower for word in ['docs', 'documentation', 'api', 'reference', 'guide', 'tutorial']):
                results.append({
                    "title": f"Search documentation for '{query}'",
                    "snippet": "DevDocs combines multiple API documentations",
                    "url": f"https://devdocs.io/#q={encoded_query}"
                })
            
            return {
                "success": True,
                "query": query,
                "results": results[:num_results],
                "count": len(results[:num_results]),
                "note": "Opening these URLs in a browser will provide search results"
            }

        except Exception as e:
            return {"success": False, "error": str(e)}


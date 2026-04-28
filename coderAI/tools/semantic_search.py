"""Semantic code search tool.

Lets the agent search the codebase with natural-language queries like
"where is authentication middleware?" or "function that parses JSON config files".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .base import Tool

logger = logging.getLogger(__name__)


class SemanticSearchParams(BaseModel):
    query: str = Field(
        ...,
        description="Natural-language description of what you are looking for. "
        "Be specific — e.g. 'the function that validates JWT tokens' rather than 'auth'.",
    )
    top_k: int = Field(
        10,
        description="Maximum number of results to return (default: 10, max: 20).",
    )
    file_filter: Optional[str] = Field(
        None,
        description="Optional glob pattern to restrict results, e.g. '*.py' or 'src/**/*.ts'.",
    )


class SemanticSearchTool(Tool):
    """Search the codebase using meaning, not exact text.

    Requires the project to be indexed first (``coderAI index``).
    """

    name = "semantic_search"
    description = (
        "Find code by meaning, not exact text. Use this when you need to locate "
        "functions, classes, or modules by what they DO rather than what they're "
        "CALLED. For example: 'where is the rate-limiting logic?', 'function that "
        "serializes API responses', 'authentication middleware'. "
        "Results include file paths, line ranges, and a relevance score. "
        "The project must be indexed first — if no results come back, suggest "
        "running `coderAI index`."
    )
    parameters_model = SemanticSearchParams
    is_read_only = True
    category = "search"  # type: ignore[assignment]

    async def execute(
        self,
        query: str,
        top_k: int = 10,
        file_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        top_k = min(top_k, 20)

        try:
            from ..code_indexer import CodeIndexer
            from ..config import config_manager
            from ..embeddings.factory import create_embedding_provider

            config = config_manager.load()
            project_root = str(Path(config.project_root).resolve())

            provider = create_embedding_provider(config)
            if provider is None:
                return {
                    "success": False,
                    "error": (
                        "No embedding provider available. Set an OpenAI API key "
                        "via `coderAI config set openai_api_key <key>` or the "
                        "OPENAI_API_KEY environment variable."
                    ),
                }

            indexer = CodeIndexer(project_root, provider)

            # Check if index has any data (stats() lazily connects)
            stats = indexer.stats()
            if stats.get("chunks", 0) == 0:
                return {
                    "success": False,
                    "error": (
                        "The codebase index is empty. Run `coderAI index` first "
                        "to build the semantic search index."
                    ),
                }

            results = await indexer.search(
                query=query,
                top_k=top_k,
                file_filter=file_filter,
            )

            return {
                "success": True,
                "query": query,
                "results": results,
                "count": len(results),
                "hint": (
                    "Use read_file with the file_path and start_line/end_line "
                    "from these results to inspect the full code."
                ),
            }

        except ImportError as e:
            return {
                "success": False,
                "error": (
                    f"Missing dependency: {e}. Install chromadb: "
                    "`pip install chromadb`"
                ),
            }
        except Exception as e:
            logger.warning(f"semantic_search failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

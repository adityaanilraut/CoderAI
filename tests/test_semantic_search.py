"""Tests for the semantic_search tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from coderAI.tools.semantic_search import SemanticSearchParams, SemanticSearchTool


class TestSemanticSearchParams:
    def test_valid_minimal_params(self):
        params = SemanticSearchParams(query="find auth middleware")
        assert params.query == "find auth middleware"
        assert params.top_k == 10
        assert params.file_filter is None

    def test_valid_full_params(self):
        params = SemanticSearchParams(
            query="rate limiter",
            top_k=5,
            file_filter="*.py",
        )
        assert params.query == "rate limiter"
        assert params.top_k == 5
        assert params.file_filter == "*.py"

    def test_missing_query_raises_validation_error(self):
        with pytest.raises(ValidationError):
            SemanticSearchParams()

    def test_top_k_default_is_10(self):
        params = SemanticSearchParams(query="test")
        assert params.top_k == 10


class TestSemanticSearchToolSchema:
    def test_schema_has_correct_name(self):
        tool = SemanticSearchTool()
        schema = tool.get_schema()
        assert schema["function"]["name"] == "semantic_search"

    def test_schema_has_description(self):
        tool = SemanticSearchTool()
        schema = tool.get_schema()
        assert "Find code by meaning" in schema["function"]["description"]

    def test_schema_includes_query_parameter(self):
        tool = SemanticSearchTool()
        schema = tool.get_schema()
        params = schema["function"]["parameters"]
        assert "query" in params["properties"]
        assert params["required"] == ["query"]

    def test_schema_includes_top_k_parameter(self):
        tool = SemanticSearchTool()
        schema = tool.get_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "top_k" in props
        assert props["top_k"]["type"] == "integer"
        assert props["top_k"]["default"] == 10

    def test_schema_includes_file_filter_parameter(self):
        tool = SemanticSearchTool()
        schema = tool.get_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "file_filter" in props
        assert "anyOf" in props["file_filter"] or "type" in props["file_filter"]

    def test_tool_is_read_only(self):
        tool = SemanticSearchTool()
        assert tool.is_read_only is True

    def test_tool_category_is_search(self):
        tool = SemanticSearchTool()
        assert tool.category == "search"


class TestSemanticSearchToolExecute:
    def _make_tool(self):
        return SemanticSearchTool()

    @pytest.mark.asyncio
    async def test_execute_with_mocked_embedding_provider(self):
        tool = self._make_tool()
        results = [
            {
                "file_path": "src/auth.py",
                "start_line": 10,
                "end_line": 25,
                "score": 0.95,
                "text": "def validate_token(token): ...",
            }
        ]
        mock_indexer = MagicMock()
        mock_indexer.stats.return_value = {"chunks": 42}
        mock_indexer.search = AsyncMock(return_value=results)

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    result = await tool.execute(
                        query="where is auth middleware",
                        top_k=5,
                    )

        assert result["success"] is True
        assert result["query"] == "where is auth middleware"
        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"] == "src/auth.py"
        assert result["results"][0]["score"] == 0.95
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_execute_caps_top_k_at_20(self):
        tool = self._make_tool()
        mock_indexer = MagicMock()
        mock_indexer.stats.return_value = {"chunks": 5}
        mock_indexer.search = AsyncMock(return_value=[])

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    await tool.execute(query="test", top_k=50)

        mock_indexer.search.assert_awaited_once()
        called_top_k = mock_indexer.search.await_args.kwargs["top_k"]
        assert called_top_k == 20

    @pytest.mark.asyncio
    async def test_execute_no_embedding_provider(self):
        tool = self._make_tool()

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=None,
        ):
            with patch("coderAI.system.config.config_manager") as mock_cm:
                from coderAI.system.config import Config

                mock_cm.load.return_value = Config()

                result = await tool.execute(query="find something")

        assert result["success"] is False
        assert "No embedding provider" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_empty_index(self):
        tool = self._make_tool()
        mock_indexer = MagicMock()
        mock_indexer.stats.return_value = {"chunks": 0}

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    result = await tool.execute(query="anything")

        assert result["success"] is False
        assert "empty" in result["error"] or "index" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_with_file_filter(self):
        tool = self._make_tool()
        mock_indexer = MagicMock()
        mock_indexer.stats.return_value = {"chunks": 10}
        mock_indexer.search = AsyncMock(return_value=[])

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    await tool.execute(
                        query="rate limiter",
                        file_filter="*.py",
                    )

        mock_indexer.search.assert_awaited_once()
        kwargs = mock_indexer.search.await_args.kwargs
        assert kwargs["file_filter"] == "*.py"

    @pytest.mark.asyncio
    async def test_execute_handles_missing_chromadb_dependency(self):
        tool = self._make_tool()

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            side_effect=ImportError("No module named 'chromadb'"),
        ):
            result = await tool.execute(query="test")

        assert result["success"] is False
        assert "chromadb" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_handles_generic_exception(self):
        tool = self._make_tool()
        mock_indexer = MagicMock()
        mock_indexer.stats.side_effect = RuntimeError("database corrupted")

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    result = await tool.execute(query="test")

        assert result["success"] is False
        assert "database corrupted" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_results_include_hint(self):
        tool = self._make_tool()
        mock_indexer = MagicMock()
        mock_indexer.stats.return_value = {"chunks": 1}
        mock_indexer.search = AsyncMock(
            return_value=[
                {"file_path": "x.py", "start_line": 1, "end_line": 10, "score": 1.0, "text": "x"}
            ]
        )

        with patch(
            "coderAI.embeddings.openai.create_embedding_provider",
            return_value=MagicMock(),
        ):
            with patch(
                "coderAI.context.code_indexer.CodeIndexer",
                return_value=mock_indexer,
            ):
                with patch("coderAI.system.config.config_manager") as mock_cm:
                    from coderAI.system.config import Config

                    mock_cm.load.return_value = Config()

                    result = await tool.execute(query="find x")

        assert "hint" in result
        assert "read_file" in result["hint"]

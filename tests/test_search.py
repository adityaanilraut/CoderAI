"""Tests for TextSearchTool and GrepTool."""

import asyncio
import pytest

from coderAI.tools.search import TextSearchTool, GrepTool


@pytest.fixture
def search_tree(tmp_path):
    """Create a small directory tree with known content for search tests."""
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("def foo():\n    pass\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("hello from subdir\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("should be ignored\n")
    return tmp_path


class TestTextSearchTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = TextSearchTool()

    def test_finds_match(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="hello", base_path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] >= 1
        files = [r["file"] for r in result["results"]]
        assert any("a.py" in f or "c.txt" in f for f in files)

    def test_no_match(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="ZZZNOMATCH999", base_path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] == 0

    def test_file_pattern_filter(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="def", base_path=str(search_tree), file_pattern="*.py")
        )
        assert result["success"]
        for r in result["results"]:
            assert r["file"].endswith(".py")

    def test_node_modules_ignored(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="ignored", base_path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] == 0

    def test_max_results_respected(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="def", base_path=str(search_tree), max_results=1)
        )
        assert result["success"]
        assert result["count"] <= 1

    def test_invalid_path(self):
        result = asyncio.run(
            self.tool.execute(query="anything", base_path="/nonexistent/path/xyz")
        )
        assert not result["success"]

    def test_case_insensitive_search(self, search_tree):
        result = asyncio.run(
            self.tool.execute(query="HELLO", base_path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] >= 1


class TestGrepTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GrepTool()

    def test_finds_literal_match(self, search_tree):
        result = asyncio.run(
            self.tool.execute(pattern="hello", path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] >= 1

    def test_regex_pattern(self, search_tree):
        # BRE-compatible pattern (grep default; no -E flag used in GrepTool)
        result = asyncio.run(
            self.tool.execute(pattern=r"def [a-z]", path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] >= 1

    def test_case_insensitive(self, search_tree):
        result = asyncio.run(
            self.tool.execute(pattern="HELLO", path=str(search_tree), case_insensitive=True)
        )
        assert result["success"]
        assert result["count"] >= 1

    def test_single_file(self, search_tree):
        target = str(search_tree / "a.py")
        result = asyncio.run(
            self.tool.execute(pattern="hello", path=target)
        )
        assert result["success"]
        assert result["count"] >= 1

    def test_no_match_returns_empty(self, search_tree):
        result = asyncio.run(
            self.tool.execute(pattern="ZZZNOMATCH999", path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] == 0

    def test_max_results_capped(self, search_tree):
        result = asyncio.run(
            self.tool.execute(pattern=".", path=str(search_tree), max_results=2)
        )
        assert result["success"]
        assert result["count"] <= 2

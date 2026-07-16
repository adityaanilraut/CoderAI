"""Tests for GrepTool and SymbolSearchTool."""

import asyncio
import pytest

from coderAI.tools.search import GrepTool, SymbolSearchTool


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


class TestGrepTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = GrepTool()

    def test_finds_literal_match(self, search_tree):
        result = asyncio.run(self.tool.execute(pattern="hello", path=str(search_tree)))
        assert result["success"]
        assert result["count"] >= 1

    def test_regex_pattern(self, search_tree):
        # BRE-compatible pattern (grep default; no -E flag used in GrepTool)
        result = asyncio.run(self.tool.execute(pattern=r"def [a-z]", path=str(search_tree)))
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
        result = asyncio.run(self.tool.execute(pattern="hello", path=target))
        assert result["success"]
        assert result["count"] >= 1

    def test_no_match_returns_empty(self, search_tree):
        result = asyncio.run(self.tool.execute(pattern="ZZZNOMATCH999", path=str(search_tree)))
        assert result["success"]
        assert result["count"] == 0

    def test_max_results_capped(self, search_tree):
        result = asyncio.run(self.tool.execute(pattern=".", path=str(search_tree), max_results=2))
        assert result["success"]
        assert result["count"] <= 2
        assert result["was_truncated"] is True


class TestSymbolSearchTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = SymbolSearchTool()

    def test_finds_python_class(self, search_tree):
        result = asyncio.run(
            self.tool.execute(symbol="hello", kind="function", path=str(search_tree))
        )
        assert result["success"]
        assert result["count"] >= 1

    def test_finds_typescript_symbol(self, tmp_path):
        target = tmp_path / "sample.ts"
        target.write_text("export class Agent {}\nconst helper = () => 1\n", encoding="utf-8")
        result = asyncio.run(self.tool.execute(symbol="Agent", kind="class", path=str(tmp_path)))
        assert result["success"]
        assert result["count"] == 1
        assert result["results"][0]["kind"] == "class"

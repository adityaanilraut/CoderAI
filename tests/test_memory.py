"""Tests for SaveMemoryTool, RecallMemoryTool, and MemoryStore."""

import asyncio
import json
import pytest

from coderAI.tools.memory import MemoryStore, SaveMemoryTool, RecallMemoryTool


@pytest.fixture
def store(tmp_path):
    """Return a fresh MemoryStore backed by a temp directory."""
    s = MemoryStore.__new__(MemoryStore)
    s.memory_dir = tmp_path / "memory"
    s.memory_dir.mkdir()
    s.memory_file = s.memory_dir / "memories.json"
    s._memories = {}
    return s


class TestMemoryStore:
    def test_add_and_get(self, store):
        store.add("key1", "value1")
        assert store.get("key1") == "value1"

    def test_get_missing_key(self, store):
        assert store.get("missing") is None

    def test_overwrite(self, store):
        store.add("k", "old")
        store.add("k", "new")
        assert store.get("k") == "new"

    def test_delete_existing(self, store):
        store.add("k", "v")
        assert store.delete("k") is True
        assert store.get("k") is None

    def test_delete_missing(self, store):
        assert store.delete("nonexistent") is False

    def test_list_all(self, store):
        store.add("a", 1)
        store.add("b", 2)
        all_mem = store.list_all()
        assert all_mem == {"a": 1, "b": 2}

    def test_search_by_key(self, store):
        store.add("project_name", "CoderAI")
        results = store.search("project")
        assert any(r["key"] == "project_name" for r in results)

    def test_search_by_value(self, store):
        store.add("info", "this is about CoderAI")
        results = store.search("CoderAI")
        assert len(results) >= 1

    def test_search_no_match(self, store):
        store.add("k", "v")
        results = store.search("ZZZNOMATCH")
        assert results == []

    def test_persistence(self, tmp_path):
        s1 = MemoryStore.__new__(MemoryStore)
        s1.memory_dir = tmp_path / "mem"
        s1.memory_dir.mkdir()
        s1.memory_file = s1.memory_dir / "memories.json"
        s1._memories = {}
        s1.add("persistent_key", "persistent_value")

        s2 = MemoryStore.__new__(MemoryStore)
        s2.memory_dir = s1.memory_dir
        s2.memory_file = s1.memory_file
        s2._memories = {}
        s2.load()
        assert s2.get("persistent_key") == "persistent_value"


class TestSaveMemoryTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        store = MemoryStore.__new__(MemoryStore)
        store.memory_dir = tmp_path / "memory"
        store.memory_dir.mkdir()
        store.memory_file = store.memory_dir / "memories.json"
        store._memories = {}
        import coderAI.tools.memory as mem_mod
        monkeypatch.setattr(mem_mod, "_memory_store", store)
        self.tool = SaveMemoryTool()

    def test_save_returns_success(self):
        result = asyncio.run(self.tool.execute(key="test_key", value="test_value"))
        assert result["success"]
        assert result["key"] == "test_key"

    def test_save_message_present(self):
        result = asyncio.run(self.tool.execute(key="k", value="v"))
        assert "message" in result

    def test_save_overwrites(self):
        asyncio.run(self.tool.execute(key="k", value="old"))
        asyncio.run(self.tool.execute(key="k", value="new"))
        import coderAI.tools.memory as mem_mod
        assert mem_mod._memory_store.get("k") == "new"


class TestRecallMemoryTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        store = MemoryStore.__new__(MemoryStore)
        store.memory_dir = tmp_path / "memory"
        store.memory_dir.mkdir()
        store.memory_file = store.memory_dir / "memories.json"
        store._memories = {"existing": "found_it"}
        import coderAI.tools.memory as mem_mod
        monkeypatch.setattr(mem_mod, "_memory_store", store)
        self.tool = RecallMemoryTool()

    def test_recall_by_key(self):
        result = asyncio.run(self.tool.execute(key="existing"))
        assert result["success"]
        assert result["value"] == "found_it"

    def test_recall_missing_key(self):
        result = asyncio.run(self.tool.execute(key="missing_key"))
        assert not result["success"]

    def test_recall_by_query(self):
        result = asyncio.run(self.tool.execute(query="existing"))
        assert result["success"]
        assert result["count"] >= 1

    def test_recall_all(self):
        result = asyncio.run(self.tool.execute())
        assert result["success"]
        assert "memories" in result
        assert result["count"] >= 1

"""Phase 5.2: incremental mtime-based reindex for CodeIndexer.

The fast-path tests drive ``_discover_changed_files`` directly (no ChromaDB).
The benchmark exercises the full ``index()`` against a mocked collection and is
marked ``slow`` so it stays out of the default run / CI.
"""

import time

import pytest

from coderAI.context import code_indexer as ci_mod
from coderAI.context.code_indexer import CodeIndexer, _entry_meta, _file_hash


class _FakeEmbed:
    """Embedding provider returning a fixed small vector per text."""

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return [[0.0, 0.1, 0.2] for _ in texts]


def _make_indexer(root):
    return CodeIndexer(str(root), _FakeEmbed())


def _entry_for(path):
    """Build the manifest entry a prior successful index would have stored."""
    st = path.stat()
    return {"hash": _file_hash(path), "mtime": st.st_mtime, "size": st.st_size}


def _write(root, name, content):
    p = root / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _entry_meta coercion
# ---------------------------------------------------------------------------


class TestEntryMeta:
    def test_none(self):
        assert _entry_meta(None) is None

    def test_legacy_string_hash(self):
        assert _entry_meta("abc123") == {"hash": "abc123", "mtime": None, "size": None}

    def test_dict_passthrough(self):
        entry = {"hash": "h", "mtime": 1.0, "size": 5}
        assert _entry_meta(entry) == entry


# ---------------------------------------------------------------------------
# Fast-path discovery
# ---------------------------------------------------------------------------


class TestIncrementalDiscovery:
    @pytest.mark.asyncio
    async def test_unchanged_files_skip_the_hash(self, tmp_path, monkeypatch):
        a = _write(tmp_path, "a.py", "def a():\n    return 1\n")
        b = _write(tmp_path, "b.py", "def b():\n    return 2\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {"a.py": _entry_for(a), "b.py": _entry_for(b)}

        # Spy: the fast path must not read file bytes for unchanged files.
        calls = {"n": 0}
        real_hash = ci_mod._file_hash
        monkeypatch.setattr(
            ci_mod,
            "_file_hash",
            lambda p: (calls.__setitem__("n", calls["n"] + 1), real_hash(p))[1],
        )

        files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            None, True
        )

        assert to_index == []
        assert (added, updated, unchanged) == (0, 0, 2)
        assert calls["n"] == 0  # no hashing happened

    @pytest.mark.asyncio
    async def test_modified_file_is_reindexed(self, tmp_path):
        a = _write(tmp_path, "a.py", "def a():\n    return 1\n")
        b = _write(tmp_path, "b.py", "def b():\n    return 2\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {"a.py": _entry_for(a), "b.py": _entry_for(b)}

        a.write_text("def a():\n    return 999\n", encoding="utf-8")

        _files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            None, True
        )

        assert [p.name for p in to_index] == ["a.py"]
        assert (added, updated, unchanged) == (0, 1, 1)

    @pytest.mark.asyncio
    async def test_new_file_counted_as_added(self, tmp_path):
        a = _write(tmp_path, "a.py", "def a():\n    return 1\n")
        _write(tmp_path, "b.py", "def b():\n    return 2\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {"a.py": _entry_for(a)}  # b.py unknown

        _files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            None, True
        )

        assert [p.name for p in to_index] == ["b.py"]
        assert (added, updated, unchanged) == (1, 0, 1)

    @pytest.mark.asyncio
    async def test_legacy_string_manifest_is_upgraded(self, tmp_path):
        a = _write(tmp_path, "a.py", "def a():\n    return 1\n")
        indexer = _make_indexer(tmp_path)
        # Legacy format: bare hash string, no stat metadata.
        indexer._manifest = {"a.py": _file_hash(a)}

        _files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            None, True
        )

        assert to_index == []
        assert unchanged == 1
        # Entry upgraded to the dict form and flagged for persistence.
        assert isinstance(indexer._manifest["a.py"], dict)
        assert indexer._manifest["a.py"]["mtime"] == a.stat().st_mtime
        assert indexer._manifest_dirty is True

    @pytest.mark.asyncio
    async def test_touch_without_content_change_refreshes_mtime(self, tmp_path):
        a = _write(tmp_path, "a.py", "def a():\n    return 1\n")
        indexer = _make_indexer(tmp_path)
        entry = _entry_for(a)
        # Pretend the file was last indexed with an older mtime.
        entry["mtime"] = entry["mtime"] - 100
        indexer._manifest = {"a.py": dict(entry)}

        _files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            None, True
        )

        # Same content, so no re-embed, but the stored mtime is refreshed.
        assert to_index == []
        assert unchanged == 1
        assert indexer._manifest["a.py"]["mtime"] == a.stat().st_mtime
        assert indexer._manifest_dirty is True


# ---------------------------------------------------------------------------
# Benchmark (opt-in: pytest -m slow)
# ---------------------------------------------------------------------------


class _FakeCollection:
    def get(self, *a, **k):
        return {"ids": []}

    def delete(self, *a, **k):
        pass

    def add(self, *a, **k):
        pass

    def count(self):
        return 0

    def query(self, *a, **k):
        return {"ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]}


@pytest.mark.slow
@pytest.mark.asyncio
async def test_warm_reindex_is_incremental(tmp_path, monkeypatch):
    """A synthetic tree: the warm reindex must skip all hashing and be faster."""
    n_files = 1500
    for i in range(n_files):
        sub = tmp_path / f"pkg{i % 25}"
        sub.mkdir(exist_ok=True)
        (sub / f"mod{i}.py").write_text(f"def f{i}(x):\n    return x + {i}\n", encoding="utf-8")

    indexer = _make_indexer(tmp_path)
    monkeypatch.setattr(
        indexer,
        "_recreate_collection",
        lambda skip: setattr(indexer, "_collection", _FakeCollection()),
    )

    cold_start = time.perf_counter()
    cold = await indexer.index(skip_if_unchanged=True)
    cold_elapsed = time.perf_counter() - cold_start
    assert cold["added"] == n_files

    # Warm run: count hashes to prove the fast path skips reading file bytes.
    calls = {"n": 0}
    real_hash = ci_mod._file_hash
    monkeypatch.setattr(
        ci_mod,
        "_file_hash",
        lambda p: (calls.__setitem__("n", calls["n"] + 1), real_hash(p))[1],
    )

    warm_start = time.perf_counter()
    warm = await indexer.index(skip_if_unchanged=True)
    warm_elapsed = time.perf_counter() - warm_start

    assert warm["unchanged"] == n_files
    assert warm["added"] == 0
    assert calls["n"] == 0  # fast path: zero file reads on the warm pass
    assert warm_elapsed < cold_elapsed

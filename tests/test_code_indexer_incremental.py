"""Phase 5.2: incremental mtime-based reindex for CodeIndexer.

The fast-path tests drive ``_discover_changed_files`` directly (no ChromaDB).
The benchmark exercises the full ``index()`` against a mocked collection and is
marked ``slow`` so it stays out of the default run / CI.
"""

import time
from types import SimpleNamespace

import pytest

from coderAI.context import code_indexer as ci_mod
from coderAI.context.code_indexer import (
    CodeIndexer,
    EmbeddingIndexMismatchError,
    _entry_meta,
    _file_hash,
)


class _FakeEmbed:
    """Embedding provider returning a fixed small vector per text."""

    def __init__(self, backend="test", model="fixed", dimension=3):
        self.calls = 0
        self.backend = backend
        self.model = model
        self._dimension = dimension

    async def embed(self, texts):
        self.calls += 1
        return [[float(self.calls)] * self._dimension for _ in texts]

    def dimension(self):
        return self._dimension


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


class _RecordingCollection:
    def __init__(self):
        self.deleted = []
        self.added = []

    def delete(self, **kwargs):
        self.deleted.append(kwargs)

    def add(self, **kwargs):
        self.added.append(kwargs)


class TestScopedIndexing:
    @pytest.mark.asyncio
    async def test_directory_scope_expands_only_that_subtree(self, tmp_path):
        package = tmp_path / "package"
        package.mkdir()
        nested = package / "nested"
        nested.mkdir()
        wanted = _write(nested, "wanted.py", "def wanted():\n    pass\n")
        _write(tmp_path, "outside.py", "def outside():\n    pass\n")

        indexer = _make_indexer(tmp_path)
        files, to_index, added, updated, unchanged = await indexer._discover_changed_files(
            ["package"], True
        )

        assert files == [wanted]
        assert to_index == [wanted]
        assert (added, updated, unchanged) == (1, 0, 0)

    def test_scoped_cleanup_deletes_only_missing_vectors_in_scope(self, tmp_path):
        package = tmp_path / "package"
        package.mkdir()
        keep = _write(package, "keep.py", "keep = True\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {
            "package/keep.py": {"hash": "keep"},
            "package/removed.py": {"hash": "removed"},
            "other/removed.py": {"hash": "unrelated"},
        }
        collection = _RecordingCollection()
        indexer._collection = collection

        removed = indexer._cleanup_removed_files([keep], ["package"])

        assert removed == 1
        assert set(indexer._manifest) == {"package/keep.py", "other/removed.py"}
        assert collection.deleted == [{"where": {"file_path": {"$in": ["package/removed.py"]}}}]

    def test_full_cleanup_deletes_all_missing_vectors(self, tmp_path):
        keep = _write(tmp_path, "keep.py", "keep = True\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {
            "keep.py": {"hash": "keep"},
            "first.py": {"hash": "first"},
            "nested/second.py": {"hash": "second"},
        }
        collection = _RecordingCollection()
        indexer._collection = collection

        removed = indexer._cleanup_removed_files([keep])

        assert removed == 2
        assert set(indexer._manifest) == {"keep.py"}
        assert collection.deleted == [
            {"where": {"file_path": {"$in": ["first.py", "nested/second.py"]}}}
        ]

    def test_scoped_force_does_not_recreate_collection(self, tmp_path, monkeypatch):
        class _Client:
            def __init__(self):
                self.deleted = []
                self.collection = _RecordingCollection()

            def delete_collection(self, name):
                self.deleted.append(name)

            def get_or_create_collection(self, **_kwargs):
                return self.collection

        client = _Client()
        monkeypatch.setattr(
            ci_mod,
            "_import_chromadb",
            lambda: SimpleNamespace(PersistentClient=lambda **_kwargs: client),
        )
        indexer = _make_indexer(tmp_path)

        indexer._recreate_collection(False, scoped=True)
        assert client.deleted == []

        indexer._recreate_collection(False, scoped=False)
        assert client.deleted == ["codebase"]

    @pytest.mark.asyncio
    async def test_scoped_force_replaces_only_scoped_vectors(self, tmp_path, monkeypatch):
        package = tmp_path / "package"
        package.mkdir()
        target = _write(package, "target.py", "def target():\n    return True\n")
        unrelated = _write(tmp_path, "unrelated.py", "def unrelated():\n    return True\n")
        indexer = _make_indexer(tmp_path)
        indexer._manifest = {
            "package/target.py": _entry_for(target),
            "unrelated.py": _entry_for(unrelated),
        }
        indexer._save_manifest()
        indexer._save_embedding_fingerprint()
        collection = _RecordingCollection()

        def use_existing_collection(skip_if_unchanged, *, scoped=False):
            assert skip_if_unchanged is False
            assert scoped is True
            indexer._collection = collection

        monkeypatch.setattr(indexer, "_recreate_collection", use_existing_collection)

        result = await indexer.index(skip_if_unchanged=False, paths=["package"])

        assert result == {"added": 1, "removed": 0, "updated": 0, "unchanged": 0}
        assert "unrelated.py" in indexer._manifest
        assert collection.deleted == [{"where": {"file_path": {"$in": ["package/target.py"]}}}]
        assert len(collection.added) == 1


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


class _FingerprintCollection(_FakeCollection):
    def __init__(self):
        self.rows = []

    def add(self, **kwargs):
        self.rows.extend(kwargs["embeddings"])

    def count(self):
        return len(self.rows)


class _FingerprintClient:
    def __init__(self):
        self.collection = None
        self.delete_calls = 0

    def get_or_create_collection(self, **_kwargs):
        if self.collection is None:
            self.collection = _FingerprintCollection()
        return self.collection

    def get_collection(self, _name):
        if self.collection is None:
            raise RuntimeError("missing collection")
        return self.collection

    def delete_collection(self, _name):
        self.delete_calls += 1
        if self.collection is None:
            raise RuntimeError("missing collection")
        self.collection = None


class TestEmbeddingFingerprint:
    @staticmethod
    def _install_fake_chroma(monkeypatch):
        client = _FingerprintClient()
        monkeypatch.setattr(
            ci_mod,
            "_import_chromadb",
            lambda: SimpleNamespace(PersistentClient=lambda **_kwargs: client),
        )
        return client

    @pytest.mark.asyncio
    async def test_backend_switch_forces_complete_reindex(self, tmp_path, monkeypatch):
        _write(tmp_path, "a.py", "def a():\n    return 1\n")
        client = self._install_fake_chroma(monkeypatch)

        first = CodeIndexer(str(tmp_path), _FakeEmbed("openai", "remote", 3))
        await first.index()
        assert client.collection.count() == 1

        second = CodeIndexer(str(tmp_path), _FakeEmbed("local", "offline", 4))
        result = await second.index()

        assert client.delete_calls == 1
        assert result == {"added": 1, "removed": 0, "updated": 0, "unchanged": 0}
        assert client.collection.count() == 1
        assert len(client.collection.rows[0]) == 4
        assert second.stats()["embedding"] == {
            "backend": "local",
            "model": "offline",
            "dimension": 4,
        }

    @pytest.mark.asyncio
    async def test_scoped_index_rejects_backend_switch(self, tmp_path, monkeypatch):
        _write(tmp_path, "a.py", "def a():\n    return 1\n")
        self._install_fake_chroma(monkeypatch)
        await CodeIndexer(str(tmp_path), _FakeEmbed("openai", "remote", 3)).index()

        switched = CodeIndexer(str(tmp_path), _FakeEmbed("local", "offline", 4))
        with pytest.raises(EmbeddingIndexMismatchError, match="without --paths"):
            await switched.index(paths=["a.py"])

    @pytest.mark.asyncio
    async def test_search_rejects_backend_switch(self, tmp_path, monkeypatch):
        _write(tmp_path, "a.py", "def a():\n    return 1\n")
        self._install_fake_chroma(monkeypatch)
        await CodeIndexer(str(tmp_path), _FakeEmbed("openai", "remote", 3)).index()

        switched = CodeIndexer(str(tmp_path), _FakeEmbed("local", "offline", 4))
        with pytest.raises(EmbeddingIndexMismatchError, match="index --force"):
            await switched.search("find a")


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
        lambda skip, **_kwargs: setattr(indexer, "_collection", _FakeCollection()),
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

"""Project-wide semantic code index backed by ChromaDB.

The indexer walks the project tree, splits files into semantic chunks,
generates embeddings, and stores them so the agent can search the codebase
with natural-language queries.

Index state lives under ``.coderAI/index/``:
  vectordb/          ChromaDB persistent store
  manifest.json      {file_path: sha256} for incremental updates
  embedding.json     embedding backend/model/dimension fingerprint
"""

from __future__ import annotations

import json
import logging
import os
import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from coderAI.embeddings import (
    EmbeddingFingerprint,
    EmbeddingProvider,
    embedding_fingerprint,
)
from coderAI.system.events import event_emitter
from coderAI.system.fsperms import atomic_write_json

logger = logging.getLogger(__name__)

_BATCH_SIZE = 32  # how many chunks to embed in one API call

_COLLECTION_NAME = "codebase"

_CHROMADB_INSTALL_HINT = (
    "Semantic code search needs ChromaDB, an optional dependency. "
    "Install it with: pip install 'coderAI[semantic]'"
)


class EmbeddingIndexMismatchError(RuntimeError):
    """Raised when the configured provider cannot safely use an existing index."""


def _import_chromadb() -> Any:
    """Import ``chromadb`` or raise an ImportError pointing at the optional extra."""
    try:
        import chromadb
    except ImportError as e:
        raise ImportError(_CHROMADB_INSTALL_HINT) from e
    return chromadb


def _on_walk_error(error: OSError) -> None:
    """Handle os.walk permission errors gracefully during indexing."""
    logger.warning("Skipping unreadable directory during indexing: %s", error)


class CodeIndexer:
    """Manages the semantic search index for a project.

    Typical usage::

        indexer = CodeIndexer(project_root, embedding_provider)
        added, removed, updated = await indexer.index(skip_if_unchanged=True)
        results = await indexer.search("where is authentication middleware?")
        stats = indexer.stats()
    """

    def __init__(
        self,
        project_root: str,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._embed = embedding_provider
        self._index_dir = self._root / ".coderAI" / "index"
        self._manifest_path = self._index_dir / "manifest.json"
        self._fingerprint_path = self._index_dir / "embedding.json"
        self._index_dir.mkdir(parents=True, exist_ok=True)

        # Manifest values are per-file entries: {"hash", "mtime", "size"}.
        # Legacy manifests stored a bare hash string; those are coerced on read
        # and upgraded to the dict form on the next index pass.
        self._manifest: Dict[str, Any] = {}
        self._manifest_dirty = False
        self._collection: Optional[Any] = None
        self._client: Optional[Any] = None

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    async def index(
        self,
        skip_if_unchanged: bool = True,
        paths: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Build or update the index.

        Args:
            skip_if_unchanged: Skip files whose hash hasn't changed since last index.
            paths: Optional list of file or directory scopes (relative to project root)
                   to index instead of the whole project.

        Returns:
            Dict with ``added``, ``removed``, ``updated``, ``unchanged`` counts.
        """
        self._load_manifest()
        scoped = bool(paths)
        skip_if_unchanged = self._prepare_embedding_fingerprint(
            skip_if_unchanged,
            scoped=scoped,
        )
        rebuilding_all = not skip_if_unchanged and not scoped
        if rebuilding_all:
            # Invalidate identity before deleting vectors. A failed rebuild can
            # then never leave a partially rebuilt collection marked compatible.
            self._fingerprint_path.unlink(missing_ok=True)
            self._manifest = {}
            self._save_manifest()

        self._recreate_collection(skip_if_unchanged, scoped=scoped)
        files, to_index, added, updated, unchanged = await self._discover_changed_files(
            paths, skip_if_unchanged
        )
        removed = self._cleanup_removed_files(files, paths)

        if not to_index:
            # Discovery may have refreshed stored mtimes for touched-but-
            # unchanged files; persist them so the fast path holds next run.
            if self._manifest_dirty:
                self._save_manifest()
            self._save_embedding_fingerprint()
            return {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}

        ids, docs, metadatas, embeddings, file_entries = await self._chunk_and_embed(to_index)
        changed_rels = [
            fp.relative_to(self._root).as_posix()
            for fp in to_index
            if fp.relative_to(self._root).as_posix() in self._manifest
        ]
        self._delete_vectors(changed_rels)
        await self._upsert_batch(ids, docs, metadatas, embeddings)
        self._update_manifest(file_entries)
        self._save_embedding_fingerprint()

        return {
            "added": added,
            "removed": removed,
            "updated": updated,
            "unchanged": unchanged,
        }

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------

    def _recreate_collection(self, skip_if_unchanged: bool, *, scoped: bool = False) -> None:
        """Initialize or re-create the ChromaDB collection."""
        chromadb = _import_chromadb()

        if self._client is None:
            chroma_path = str(self._index_dir / "vectordb")
            self._client = chromadb.PersistentClient(path=chroma_path)

        if not skip_if_unchanged and not scoped:
            try:
                self._client.delete_collection(_COLLECTION_NAME)
            except Exception as delete_error:
                # Chroma raises when the collection does not exist. Distinguish
                # that harmless case from a failed deletion of live vectors.
                try:
                    self._client.get_collection(_COLLECTION_NAME)
                except Exception:
                    pass
                else:
                    raise RuntimeError(
                        "Could not safely clear the existing semantic index"
                    ) from delete_error

        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def _scan_files_sync(self) -> List[Path]:
        """Synchronously scan directory for files to index (run in a thread pool)."""
        return self._scan_scopes_sync([self._root])

    def _scan_scopes_sync(self, scopes: List[Path]) -> List[Path]:
        """Expand scoped files and directories into indexable files."""
        from coderAI.context.code_chunker import is_skip_dir, should_index

        files: dict[Path, None] = {}
        for scope in scopes:
            if scope.is_file():
                if should_index(scope):
                    files[scope] = None
                continue
            if not scope.is_dir() or (scope != self._root and is_skip_dir(scope.name)):
                continue
            for dirpath, dirnames, filenames in os.walk(scope, onerror=_on_walk_error):
                dirnames[:] = [d for d in dirnames if not is_skip_dir(d)]
                for fname in filenames:
                    fp = Path(dirpath) / fname
                    if should_index(fp):
                        files[fp] = None
        return list(files)

    def _resolve_scopes(self, paths: List[str]) -> List[Path]:
        """Resolve explicit scopes and discard paths outside the project."""
        scopes: dict[Path, None] = {}
        for value in paths:
            path = Path(value).expanduser()
            resolved = (path if path.is_absolute() else self._root / path).resolve()
            try:
                resolved.relative_to(self._root)
            except ValueError:
                continue
            scopes[resolved] = None
        return list(scopes)

    async def _discover_changed_files(
        self,
        paths: Optional[List[str]],
        skip_if_unchanged: bool,
    ) -> tuple:
        """Walk project, gather files, and separate changed from unchanged.

        Returns (files, to_index, added, updated, unchanged).
        """

        if paths:
            scopes = self._resolve_scopes(paths)
            files = await asyncio.to_thread(self._scan_scopes_sync, scopes)
        else:
            files = await asyncio.to_thread(self._scan_files_sync)

        to_index: list[Path] = []
        unchanged = 0
        added = 0
        updated = 0
        for fp in files:
            rel = fp.relative_to(self._root).as_posix()
            try:
                st = fp.stat()
            except OSError:
                continue
            mtime, size = st.st_mtime, st.st_size
            existing = _entry_meta(self._manifest.get(rel))

            # Fast path: when mtime AND size are unchanged since the last index,
            # assume the content is unchanged and skip the full-file sha256 read.
            if (
                skip_if_unchanged
                and existing is not None
                and existing.get("mtime") == mtime
                and existing.get("size") == size
            ):
                unchanged += 1
                continue

            fhash = _file_hash(fp)
            if fhash is None:
                continue

            if skip_if_unchanged and existing is not None and existing.get("hash") == fhash:
                # Content identical despite an mtime/size touch — refresh the
                # stored stat so the next run fast-paths, but don't re-embed.
                self._manifest[rel] = {"hash": fhash, "mtime": mtime, "size": size}
                self._manifest_dirty = True
                unchanged += 1
                continue

            if skip_if_unchanged:
                if existing is None:
                    added += 1
                else:
                    updated += 1
            to_index.append(fp)

        if not skip_if_unchanged:
            added = len(to_index)

        logger.info(
            "Indexing %d files (%d unchanged, %d total scanned)",
            len(to_index),
            unchanged,
            len(files),
        )

        return files, to_index, added, updated, unchanged

    def _cleanup_removed_files(self, files: list[Path], paths: Optional[List[str]] = None) -> int:
        """Remove missing entries and vectors within the scanned scope.

        Returns count of removed entries.
        """
        known = {f.relative_to(self._root).as_posix() for f in files}
        candidates = list(self._manifest)
        if paths:
            scope_rels = [scope.relative_to(self._root) for scope in self._resolve_scopes(paths)]
            candidates = [
                rel
                for rel in candidates
                if any(Path(rel) == scope or scope in Path(rel).parents for scope in scope_rels)
            ]

        removed_rels = [rel for rel in candidates if rel not in known]
        if removed_rels:
            self._delete_vectors(removed_rels)
            for rel in removed_rels:
                del self._manifest[rel]
            self._save_manifest()
        return len(removed_rels)

    def _delete_vectors(self, file_paths: List[str]) -> None:
        """Delete all indexed chunks belonging to the supplied relative paths."""
        if self._collection is None or not file_paths:
            return
        self._collection.delete(where={"file_path": {"$in": file_paths}})

    async def _chunk_and_embed(self, to_index: list[Path]) -> tuple:
        """Chunk files and generate embeddings.

        Returns (ids, docs, metadatas, embeddings, file_entries) where
        file_entries maps rel-path -> {"hash", "mtime", "size"}.
        """
        from coderAI.context.code_chunker import chunk_file

        all_chunks: list = []
        file_entries: Dict[str, dict] = {}
        for fp in to_index:
            rel = fp.relative_to(self._root).as_posix()
            result = await asyncio.to_thread(chunk_file, fp, self._root)
            if result.chunks:
                all_chunks.extend(result.chunks)
                try:
                    st = fp.stat()
                    file_entries[rel] = {
                        "hash": result.file_hash,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                    }
                except OSError:
                    file_entries[rel] = {"hash": result.file_hash, "mtime": None, "size": None}

        ids: list[str] = []
        docs: list[str] = []
        metadatas: list[dict] = []
        embeddings: list[list[float]] = []

        total_chunks = len(all_chunks)
        for batch_start in range(0, total_chunks, _BATCH_SIZE):
            batch = all_chunks[batch_start : batch_start + _BATCH_SIZE]
            texts = [c.text for c in batch]
            vecs = await self._embed.embed(texts)
            expected_dimension = self._current_embedding_fingerprint().dimension
            if len(vecs) != len(texts):
                raise ValueError(
                    f"Embedding provider returned {len(vecs)} vectors for {len(texts)} texts"
                )
            if any(len(vec) != expected_dimension for vec in vecs):
                raise ValueError(
                    "Embedding provider returned a vector that does not match its "
                    f"declared dimension {expected_dimension}"
                )

            # Progress event so the UI can show embedding advance on large trees.
            event_emitter.emit(
                "index_progress",
                phase="embed",
                done=min(batch_start + len(batch), total_chunks),
                total=total_chunks,
                files=len(file_entries),
            )

            for chunk, vec in zip(batch, vecs):
                cid = f"{chunk.file_path}:{chunk.start_line}"
                ids.append(cid)
                docs.append(chunk.text)
                metadatas.append(
                    {
                        "file_path": chunk.file_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "language": chunk.language,
                        "chunk_type": chunk.chunk_type,
                    }
                )
                embeddings.append(vec)

        return ids, docs, metadatas, embeddings, file_entries

    async def _upsert_batch(
        self,
        ids: list[str],
        docs: list[str],
        metadatas: list[dict],
        embeddings: list[list[float]],
    ) -> None:
        """Insert chunk batches into the ChromaDB collection."""
        if not ids:
            return
        assert self._collection is not None
        await asyncio.to_thread(
            self._collection.add,
            ids=ids,
            documents=docs,
            metadatas=metadatas,
            embeddings=embeddings,
        )

    def _update_manifest(self, file_entries: Dict[str, dict]) -> None:
        """Persist updated file entries to the manifest after a successful write."""
        for rel, entry in file_entries.items():
            self._manifest[rel] = entry
        self._save_manifest()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 10,
        file_filter: Optional[str] = None,
    ) -> List[dict]:
        """Find code chunks semantically similar to *query*.

        Args:
            query: Natural-language search query.
            top_k: Maximum number of results.
            file_filter: Optional glob pattern to restrict results (e.g. ``*.py``).

        Returns:
            List of result dicts with keys: file_path, start_line, end_line,
            language, chunk_type, text (truncated to 500 chars), and score.
        """
        if self._collection is None:
            self._connect()

        self._require_compatible_fingerprint()

        assert self._collection is not None
        query_vec = await self._embed.embed([query])
        # ChromaDB doesn't support glob filters natively, so when file_filter is
        # set we overfetch and filter client-side below via _match_glob.
        results = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=query_vec,
            n_results=min(top_k * 2 if file_filter else top_k, 50),
        )

        out: list[dict] = []
        if not results or not results["ids"] or not results["ids"][0]:
            return out

        for i, cid in enumerate(results["ids"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            doc = results["documents"][0][i] if results["documents"] else ""
            dist = results["distances"][0][i] if results["distances"] else 0.0

            fp = meta.get("file_path", "")
            if file_filter and not _match_glob(fp, file_filter):
                continue

            out.append(
                {
                    "file_path": fp,
                    "start_line": meta.get("start_line", 1),
                    "end_line": meta.get("end_line", 1),
                    "language": meta.get("language", ""),
                    "chunk_type": meta.get("chunk_type", ""),
                    "text": doc[:500],
                    "score": round(1.0 - dist, 4) if dist else 1.0,
                }
            )
            if len(out) >= top_k:
                break

        return out

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return index statistics."""
        if self._collection is None:
            self._connect()
        assert self._collection is not None
        try:
            count = self._collection.count()
        except Exception:
            count = 0
        if count:
            self._require_compatible_fingerprint()
        stored_fingerprint = self._load_embedding_fingerprint()
        return {
            "indexed_files": len(self._manifest),
            "chunks": count,
            "index_dir": str(self._index_dir),
            "embedding": stored_fingerprint.to_dict() if stored_fingerprint else None,
        }

    def clear(self) -> None:
        """Delete the entire index."""
        import shutil

        if self._client is not None:
            try:
                self._client.delete_collection(_COLLECTION_NAME)
            except Exception:
                logger.debug("Failed to delete ChromaDB collection during clear", exc_info=True)
                pass
        if self._index_dir.exists():
            shutil.rmtree(str(self._index_dir))
        self._manifest = {}
        self._collection = None
        self._client = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        chromadb = _import_chromadb()

        if self._client is None:
            chroma_path = str(self._index_dir / "vectordb")
            self._client = chromadb.PersistentClient(path=chroma_path)
        try:
            self._collection = self._client.get_collection(_COLLECTION_NAME)
        except Exception:
            self._collection = self._client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )

    def _load_manifest(self) -> None:
        self._manifest_dirty = False
        if self._manifest_path.is_file():
            try:
                self._manifest = json.loads(self._manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._manifest = {}

    def _current_embedding_fingerprint(self) -> EmbeddingFingerprint:
        return embedding_fingerprint(self._embed)

    def _load_embedding_fingerprint(self) -> Optional[EmbeddingFingerprint]:
        if not self._fingerprint_path.is_file():
            return None
        try:
            data = json.loads(self._fingerprint_path.read_text(encoding="utf-8"))
            backend = data["backend"]
            model = data["model"]
            dimension = data["dimension"]
            if not isinstance(backend, str) or not isinstance(model, str):
                return None
            if not isinstance(dimension, int) or dimension <= 0:
                return None
            return EmbeddingFingerprint(backend=backend, model=model, dimension=dimension)
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            return None

    def _save_embedding_fingerprint(self) -> None:
        atomic_write_json(
            self._fingerprint_path,
            self._current_embedding_fingerprint().to_dict(),
        )

    def _has_existing_vectors(self) -> bool:
        if not (self._index_dir / "vectordb").exists():
            return False
        try:
            if self._collection is None:
                self._connect()
            assert self._collection is not None
            return bool(self._collection.count())
        except Exception:
            # An unreadable existing store is not safe to append to.
            return True

    @staticmethod
    def _fingerprint_description(fingerprint: Optional[EmbeddingFingerprint]) -> str:
        if fingerprint is None:
            return "unknown legacy embedding configuration"
        return f"{fingerprint.backend}/{fingerprint.model} ({fingerprint.dimension} dimensions)"

    def _mismatch_message(
        self,
        stored: Optional[EmbeddingFingerprint],
        current: EmbeddingFingerprint,
    ) -> str:
        return (
            "Semantic index embedding mismatch: index uses "
            f"{self._fingerprint_description(stored)}, but configuration selects "
            f"{self._fingerprint_description(current)}. Run `coderAI index --force` "
            "without --paths to rebuild the complete index."
        )

    def _prepare_embedding_fingerprint(
        self,
        skip_if_unchanged: bool,
        *,
        scoped: bool,
    ) -> bool:
        current = self._current_embedding_fingerprint()
        stored = self._load_embedding_fingerprint()
        if stored == current:
            return skip_if_unchanged

        has_existing_index = (
            self._fingerprint_path.exists() or bool(self._manifest) or self._has_existing_vectors()
        )
        if not has_existing_index:
            return skip_if_unchanged
        if scoped:
            raise EmbeddingIndexMismatchError(self._mismatch_message(stored, current))

        logger.warning(
            "%s Rebuilding the complete index automatically.",
            self._mismatch_message(stored, current),
        )
        return False

    def _require_compatible_fingerprint(self) -> None:
        current = self._current_embedding_fingerprint()
        stored = self._load_embedding_fingerprint()
        if stored != current:
            raise EmbeddingIndexMismatchError(self._mismatch_message(stored, current))

    def _save_manifest(self) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._manifest_path.parent,
                prefix=f".{self._manifest_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_name = temp_file.name
                json.dump(self._manifest, temp_file, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, self._manifest_path)
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Failed to remove manifest temporary file", exc_info=True)
        self._manifest_dirty = False


def _entry_meta(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a manifest value into ``{"hash", "mtime", "size"}`` form.

    Accepts the current dict form, the legacy bare-hash string (no stat — so
    the fast path is skipped until the entry is rewritten), or None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return {"hash": value, "mtime": None, "size": None}
    if isinstance(value, dict):
        return value
    return None


def _file_hash(path: Path) -> Optional[str]:
    """SHA-256 of file contents, or None on failure."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _match_glob(file_path: str, pattern: str) -> bool:
    """Simple glob match — supports ``*.py`` and ``**/*.py``."""
    import fnmatch

    return fnmatch.fnmatch(file_path, pattern)

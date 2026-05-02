"""Project-wide semantic code index backed by ChromaDB.

The indexer walks the project tree, splits files into semantic chunks,
generates embeddings, and stores them so the agent can search the codebase
with natural-language queries.

Index state lives under ``.coderAI/index/``:
  vectordb/          ChromaDB persistent store
  manifest.json      {file_path: sha256} for incremental updates
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BATCH_SIZE = 32  # how many chunks to embed in one API call

_COLLECTION_NAME = "codebase"


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
        embedding_provider,  # EmbeddingProvider
    ) -> None:
        self._root = Path(project_root).resolve()
        self._embed = embedding_provider
        self._index_dir = self._root / ".coderAI" / "index"
        self._manifest_path = self._index_dir / "manifest.json"
        self._index_dir.mkdir(parents=True, exist_ok=True)

        self._manifest: Dict[str, str] = {}
        self._collection = None

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
            paths: Optional list of specific file paths (relative to project root)
                   to index instead of the whole project.

        Returns:
            Dict with ``added``, ``removed``, ``updated``, ``unchanged`` counts.
        """
        import chromadb

        self._load_manifest()

        chroma_path = str(self._index_dir / "vectordb")
        client = chromadb.PersistentClient(path=chroma_path)

        # Only delete and recreate collection for full re-index.
        if not skip_if_unchanged:
            try:
                client.delete_collection(_COLLECTION_NAME)
            except Exception:
                pass

        self._collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        from .code_chunker import chunk_file, is_skip_dir, should_index

        # Gather files
        if paths:
            files = [
                (self._root / p).resolve()
                for p in paths
                if (self._root / p).resolve().is_file()
            ]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(self._root):
                dirnames[:] = [d for d in dirnames if not is_skip_dir(d)]
                for fname in filenames:
                    fp = Path(dirpath) / fname
                    if should_index(fp):
                        files.append(fp)

        # Filter unchanged; separate genuinely new files from hash-changed ones
        # so the returned stats are no longer misleading.
        to_index: list[Path] = []
        unchanged = 0
        added = 0
        updated = 0
        for fp in files:
            rel = str(fp.relative_to(self._root))
            fhash = _file_hash(fp)
            if skip_if_unchanged:
                existing = self._manifest.get(rel)
                if existing == fhash:
                    unchanged += 1
                    continue
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

        # Remove files no longer on disk
        known = {str(f.relative_to(self._root)) for f in files}
        removed = 0
        for rel in list(self._manifest.keys()):
            if rel not in known:
                del self._manifest[rel]
                removed += 1

        if removed:
            self._save_manifest()

        if not to_index:
            return {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}

        # Chunk
        all_chunks: list = []
        file_hashes: Dict[str, str] = {}
        for fp in to_index:
            rel = str(fp.relative_to(self._root))
            result = chunk_file(fp, self._root)
            if result.chunks:
                all_chunks.extend(result.chunks)
                file_hashes[rel] = result.file_hash

        if not all_chunks:
            return {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}

        # Embed in batches
        ids: list[str] = []
        docs: list[str] = []
        metadatas: list[dict] = []
        embeddings: list[list[float]] = []

        for batch_start in range(0, len(all_chunks), _BATCH_SIZE):
            batch = all_chunks[batch_start : batch_start + _BATCH_SIZE]
            texts = [c.text for c in batch]
            vecs = await self._embed.embed(texts)

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

        # Upsert
        if ids:
            self._collection.add(
                ids=ids,
                documents=docs,
                metadatas=metadatas,
                embeddings=embeddings,
            )

        # Update manifest
        for rel, fhash in file_hashes.items():
            self._manifest[rel] = fhash
        self._save_manifest()

        return {
            "added": added,
            "removed": removed,
            "updated": updated,
            "unchanged": unchanged,
        }

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

        query_vec = await self._embed.embed([query])
        # ChromaDB doesn't support glob filters natively, so when file_filter is
        # set we overfetch and filter client-side below via _match_glob.
        results = self._collection.query(
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
        try:
            count = self._collection.count()
        except Exception:
            count = 0
        return {
            "indexed_files": len(self._manifest),
            "chunks": count,
            "index_dir": str(self._index_dir),
        }

    def clear(self) -> None:
        """Delete the entire index."""
        import chromadb
        import shutil

        chroma_path = str(self._index_dir / "vectordb")
        client = chromadb.PersistentClient(path=chroma_path)
        try:
            client.delete_collection(_COLLECTION_NAME)
        except Exception:
            pass
        if self._index_dir.exists():
            shutil.rmtree(str(self._index_dir))
        self._manifest = {}
        self._collection = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        import chromadb

        chroma_path = str(self._index_dir / "vectordb")
        client = chromadb.PersistentClient(path=chroma_path)
        try:
            self._collection = client.get_collection(_COLLECTION_NAME)
        except Exception:
            self._collection = client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )

    def _load_manifest(self) -> None:
        if self._manifest_path.is_file():
            try:
                self._manifest = json.loads(self._manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._manifest = {}

    def _save_manifest(self) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(self._manifest, indent=2, sort_keys=True)
        )


def _file_hash(path: Path) -> str:
    """SHA-256 of file contents, or empty string on failure."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _match_glob(file_path: str, pattern: str) -> bool:
    """Simple glob match — supports ``*.py`` and ``**/*.py``."""
    import re

    # Convert ** to a catch-all
    regex = re.escape(pattern).replace(r"\*\*", "___DOUBLESTAR___").replace(r"\*", "[^/]*").replace("___DOUBLESTAR___", ".*")
    return bool(re.match(regex, file_path))

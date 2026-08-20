"""Session Full-Text Search (FTS) and Historical Query Indexer for CoderAI."""

from __future__ import annotations

import json
import logging
import math
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionSearchResult:
    """Represents a matched message or tool result from session query."""

    session_id: str
    message_id: str
    role: str
    content_snippet: str
    score: float
    timestamp: float = 0.0
    tool_name: str | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "role": self.role,
            "content_snippet": self.content_snippet,
            "score": round(self.score, 4),
            "timestamp": self.timestamp,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
        }


@dataclass
class IndexedMessage:
    """Normalized message representation for full-text search."""

    session_id: str
    message_id: str
    role: str
    content: str
    timestamp: float
    tool_name: str | None = None
    tool_call_id: str | None = None
    tokens: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens."""
    return re.findall(r"\b\w+\b", text.lower())


class SessionIndex:
    """In-memory BM25-based Full-Text Search Index over session history."""

    def __init__(self, project_root: str) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.documents: list[IndexedMessage] = []
        self._doc_lens: list[int] = []
        self._avg_doc_len: float = 0.0
        self._inverted_index: dict[str, list[int]] = {}
        self._last_indexed: float = 0.0

    def index_messages(self, messages: list[dict[str, Any]], session_id: str) -> None:
        """Directly index a list of message dicts for a session."""
        for msg in messages:
            msg_id = msg.get("id") or msg.get("message_id") or f"msg_{len(self.documents)}"
            role = msg.get("role", "user")
            content = str(msg.get("content") or "")
            ts = float(msg.get("timestamp") or msg.get("created_at") or time.time())
            tool_name = msg.get("tool_name") or msg.get("name")
            tool_call_id = msg.get("tool_call_id") or msg.get("toolCallId")

            tokens = _tokenize(content)
            doc_idx = len(self.documents)
            self.documents.append(
                IndexedMessage(
                    session_id=session_id,
                    message_id=msg_id,
                    role=role,
                    content=content,
                    timestamp=ts,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tokens=tokens,
                )
            )
            for tok in set(tokens):
                if tok not in self._inverted_index:
                    self._inverted_index[tok] = []
                self._inverted_index[tok].append(doc_idx)

        self._recalc_stats()

    def scan_and_index_workspace(self) -> int:
        """Scan .coderAI/sessions/ or project sessions on disk and index JSONL files."""
        local_dir = pathlib.Path(self.project_root) / ".coderAI" / "sessions"
        if local_dir.exists():
            session_dirs = [local_dir]
        else:
            session_dirs = [pathlib.Path.home() / ".coderAI" / "sessions"]

        count = 0
        self.documents.clear()
        self._inverted_index.clear()

        for s_dir in session_dirs:
            if not s_dir.exists():
                continue
            for jsonl_file in s_dir.glob("*.jsonl"):
                sid = jsonl_file.stem
                try:
                    with open(jsonl_file, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            try:
                                data = json.loads(line)
                                msg_id = data.get("id") or f"msg_{len(self.documents)}"
                                role = data.get("role", "user")
                                content = str(data.get("content") or "")
                                ts = float(data.get("timestamp") or data.get("created_at") or 0.0)
                                tool_name = data.get("name") or data.get("tool_name")
                                tool_call_id = data.get("tool_call_id") or data.get("toolCallId")

                                tokens = _tokenize(content)
                                doc_idx = len(self.documents)
                                self.documents.append(
                                    IndexedMessage(
                                        session_id=sid,
                                        message_id=msg_id,
                                        role=role,
                                        content=content,
                                        timestamp=ts,
                                        tool_name=tool_name,
                                        tool_call_id=tool_call_id,
                                        tokens=tokens,
                                    )
                                )
                                for tok in set(tokens):
                                    if tok not in self._inverted_index:
                                        self._inverted_index[tok] = []
                                    self._inverted_index[tok].append(doc_idx)
                                count += 1
                            except Exception:
                                continue
                except Exception:
                    continue

        self._recalc_stats()
        self._last_indexed = time.time()
        return count

    def _recalc_stats(self) -> None:
        self._doc_lens = [len(doc.tokens) for doc in self.documents]
        self._avg_doc_len = sum(self._doc_lens) / len(self._doc_lens) if self._doc_lens else 0.0

    def search(
        self,
        query: str,
        session_id: str | None = None,
        role: str | None = None,
        tool_name: str | None = None,
        limit: int = 10,
    ) -> list[SessionSearchResult]:
        """Perform BM25 search over indexed session messages."""
        if not self.documents:
            self.scan_and_index_workspace()

        if not self.documents:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # BM25 Parameters
        k1 = 1.5
        b = 0.75
        n_docs = len(self.documents)

        scores: dict[int, float] = {}

        for tok in query_tokens:
            posting_list = self._inverted_index.get(tok, [])
            df = len(posting_list)
            if df == 0:
                continue

            # IDF
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)

            for doc_idx in posting_list:
                doc = self.documents[doc_idx]

                # Filters
                if session_id and doc.session_id != session_id:
                    continue
                if role and doc.role != role:
                    continue
                if tool_name and doc.tool_name != tool_name:
                    continue

                tf = doc.tokens.count(tok)
                doc_len = self._doc_lens[doc_idx]
                score = (
                    idf
                    * (tf * (k1 + 1))
                    / (tf + k1 * (1 - b + b * (doc_len / (self._avg_doc_len or 1.0))))
                )
                scores[doc_idx] = scores.get(doc_idx, 0.0) + score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        results: list[SessionSearchResult] = []

        for doc_idx, score in ranked:
            doc = self.documents[doc_idx]
            snippet = self._make_snippet(doc.content, query_tokens)
            results.append(
                SessionSearchResult(
                    session_id=doc.session_id,
                    message_id=doc.message_id,
                    role=doc.role,
                    content_snippet=snippet,
                    score=score,
                    timestamp=doc.timestamp,
                    tool_name=doc.tool_name,
                    tool_call_id=doc.tool_call_id,
                )
            )

        return results

    def _make_snippet(self, content: str, query_tokens: list[str], max_len: int = 250) -> str:
        """Create a highlighted excerpt centered around query matches."""
        content_clean = " ".join(content.split())
        if len(content_clean) <= max_len:
            return content_clean

        lower = content_clean.lower()
        first_pos = len(content_clean)
        for tok in query_tokens:
            pos = lower.find(tok)
            if pos != -1 and pos < first_pos:
                first_pos = pos

        if first_pos == len(content_clean):
            return content_clean[:max_len] + "..."

        start = max(0, first_pos - 60)
        end = min(len(content_clean), start + max_len)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content_clean) else ""
        return f"{prefix}{content_clean[start:end]}{suffix}"


_session_indices: dict[str, SessionIndex] = {}


def get_session_index(project_root: str) -> SessionIndex:
    """Get or create the SessionIndex for a given project."""
    root = str(pathlib.Path(project_root).resolve())
    if root not in _session_indices:
        _session_indices[root] = SessionIndex(root)
    return _session_indices[root]

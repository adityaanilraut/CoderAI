"""Snippet-scoped file state

Core invariant: `read` returns a `snippet_id`; `edit` requires it and is scoped
to that snippet's line range. Edits are rejected when the file changed since the
snippet was issued (fileVersion check). Full-file views use a separate
`full_file_*` id space. `recordFileState` tracks the exact bytes we showed the
model so `write`/`edit` can detect external modification.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from coderai.core.common.file_utils import read_text_file_with_metadata


@dataclass
class FileState:
    file_path: str
    content: str
    timestamp: int
    version: int = 0
    offset: int | None = None
    limit: int | None = None
    is_partial_view: bool = False
    encoding: str = "utf8"
    line_endings: str = "LF"


@dataclass(frozen=True)
class FileSnippet:
    id: str
    file_path: str
    start_line: int
    end_line: int
    preview: str
    file_version: int
    scope_type: str  # "snippet" | "full"


def normalize_file_path(file_path: str) -> str:
    if not file_path:
        return ""
    return os.path.normpath(file_path)


def is_absolute_file_path(file_path: str) -> bool:
    if not file_path:
        return False
    return os.path.isabs(file_path)


class SessionStateManager:
    """Encapsulates snippet-scoped file states, file versions, and snippet counters."""

    def __init__(self) -> None:
        self._file_states: dict[str, dict[str, FileState]] = {}
        self._snippets: dict[str, dict[str, FileSnippet]] = {}
        self._counters: dict[str, int] = {}
        self._full_counters: dict[str, int] = {}
        self._versions: dict[str, dict[str, int]] = {}

    def clear(self, session_id: str) -> None:
        if not session_id:
            return
        self._file_states.pop(session_id, None)
        self._snippets.pop(session_id, None)
        self._counters.pop(session_id, None)
        self._full_counters.pop(session_id, None)
        self._versions.pop(session_id, None)

    def has_session_state(self, session_id: str) -> bool:
        return bool(
            self._file_states.get(session_id)
            or self._snippets.get(session_id)
            or session_id in self._counters
            or session_id in self._full_counters
            or self._versions.get(session_id)
        )

    def get_file_version(self, session_id: str, file_path: str) -> int:
        return self._versions.get(session_id, {}).get(normalize_file_path(file_path), 0)

    def set_file_version(self, session_id: str, file_path: str, version: int) -> None:
        self._versions.setdefault(session_id, {})[normalize_file_path(file_path)] = version

    def record_file_state(
        self, session_id: str, state: FileState, increment_version: bool = False
    ) -> None:
        if not session_id or not state.file_path:
            return
        normalized = normalize_file_path(state.file_path)
        current = self.get_file_version(session_id, normalized)
        next_version = current + 1 if increment_version else current
        self.set_file_version(session_id, normalized, next_version)
        state.file_path = normalized
        state.version = next_version
        self._file_states.setdefault(session_id, {})[normalized] = state

    def mark_file_read(
        self, session_id: str, file_path: str, state: dict[str, Any] | None = None
    ) -> None:
        if not session_id or not file_path:
            return
        state = state or {}
        self.record_file_state(
            session_id,
            FileState(
                file_path=file_path,
                content=state.get("content", ""),
                timestamp=state.get("timestamp", 0),
                offset=state.get("offset"),
                limit=state.get("limit"),
                is_partial_view=state.get("is_partial_view", False),
                encoding=state.get("encoding", "utf8"),
                line_endings=state.get("line_endings", "LF"),
            ),
        )

    def get_file_state(self, session_id: str, file_path: str) -> FileState | None:
        if not session_id or not file_path:
            return None
        return self._file_states.get(session_id, {}).get(normalize_file_path(file_path))

    def was_file_read(self, session_id: str, file_path: str) -> bool:
        return self.get_file_state(session_id, file_path) is not None

    def store_snippet(self, session_id: str, snippet: FileSnippet) -> FileSnippet:
        self._snippets.setdefault(session_id, {})[snippet.id] = snippet
        return snippet

    def create_with_id(
        self,
        session_id: str,
        file_path: str,
        start_line: int,
        end_line: int,
        preview: str,
        sid: str,
        scope_type: str,
    ) -> FileSnippet | None:
        if not session_id or not file_path or start_line < 1 or end_line < start_line:
            return None
        snippet = FileSnippet(
            id=sid,
            file_path=normalize_file_path(file_path),
            start_line=start_line,
            end_line=end_line,
            preview=preview,
            file_version=self.get_file_version(session_id, file_path),
            scope_type=scope_type,
        )
        return self.store_snippet(session_id, snippet)

    def create_snippet(
        self, session_id: str, file_path: str, start_line: int, end_line: int, preview: str
    ) -> FileSnippet | None:
        nxt = self._counters.get(session_id, 0) + 1
        self._counters[session_id] = nxt
        return self.create_with_id(
            session_id, file_path, start_line, end_line, preview, f"snippet_{nxt}", "snippet"
        )

    def create_full_file_snippet(
        self, session_id: str, file_path: str, start_line: int, end_line: int, preview: str
    ) -> FileSnippet | None:
        nxt = self._full_counters.get(session_id, 0)
        self._full_counters[session_id] = nxt + 1
        return self.create_with_id(
            session_id, file_path, start_line, end_line, preview, f"full_file_{nxt}", "full"
        )

    def restore_snippet(
        self,
        session_id: str,
        *,
        id: str,
        file_path: str,
        start_line: int,
        end_line: int,
        preview: str = "",
        scope_type: str | None = None,
    ) -> FileSnippet | None:
        if not session_id or not id or not file_path or start_line < 1 or end_line < start_line:
            return None
        scope = scope_type or ("full" if id.startswith("full_file_") else "snippet")
        restored = self.create_with_id(
            session_id, file_path, start_line, end_line, preview, id, scope
        )
        if restored:
            if id.startswith("full_file_"):
                try:
                    n = int(id.split("_")[2])
                    self._full_counters[session_id] = max(
                        self._full_counters.get(session_id, 0), n + 1
                    )
                except (IndexError, ValueError):
                    pass
            elif id.startswith("snippet_"):
                try:
                    n = int(id.split("_")[1])
                    self._counters[session_id] = max(self._counters.get(session_id, 0), n)
                except (IndexError, ValueError):
                    pass
        return restored

    def get_snippet(self, session_id: str, snippet_id: str) -> FileSnippet | None:
        if not session_id or not snippet_id:
            return None
        return self._snippets.get(session_id, {}).get(snippet_id)

    def has_snippet_outdated_file_version(self, session_id: str, snippet: FileSnippet) -> bool:
        return self.get_file_version(session_id, snippet.file_path) > snippet.file_version


_default_manager = SessionStateManager()

# Global module-level compatibility bindings
_file_states = _default_manager._file_states
_snippets = _default_manager._snippets
_counters = _default_manager._counters
_full_counters = _default_manager._full_counters
_versions = _default_manager._versions


def clear_session_state(session_id: str) -> None:
    _default_manager.clear(session_id)


def has_session_state(session_id: str) -> bool:
    return _default_manager.has_session_state(session_id)


def get_file_version(session_id: str, file_path: str) -> int:
    return _default_manager.get_file_version(session_id, file_path)


def _set_file_version(session_id: str, file_path: str, version: int) -> None:
    _default_manager.set_file_version(session_id, file_path, version)


def record_file_state(session_id: str, state: FileState, increment_version: bool = False) -> None:
    _default_manager.record_file_state(session_id, state, increment_version=increment_version)


def mark_file_read(session_id: str, file_path: str, state: dict[str, Any] | None = None) -> None:
    _default_manager.mark_file_read(session_id, file_path, state)


def get_file_state(session_id: str, file_path: str) -> FileState | None:
    return _default_manager.get_file_state(session_id, file_path)


def was_file_read(session_id: str, file_path: str) -> bool:
    return _default_manager.was_file_read(session_id, file_path)


def is_full_file_view(state: FileState | None) -> bool:
    return bool(
        state and not state.is_partial_view and state.offset is None and state.limit is None
    )


def create_snippet(
    session_id: str, file_path: str, start_line: int, end_line: int, preview: str
) -> FileSnippet | None:
    return _default_manager.create_snippet(session_id, file_path, start_line, end_line, preview)


def create_full_file_snippet(
    session_id: str, file_path: str, start_line: int, end_line: int, preview: str
) -> FileSnippet | None:
    return _default_manager.create_full_file_snippet(
        session_id, file_path, start_line, end_line, preview
    )


def restore_snippet(
    session_id: str,
    *,
    id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    preview: str = "",
    scope_type: str | None = None,
) -> FileSnippet | None:
    return _default_manager.restore_snippet(
        session_id,
        id=id,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        preview=preview,
        scope_type=scope_type,
    )


def get_snippet(session_id: str, snippet_id: str) -> FileSnippet | None:
    return _default_manager.get_snippet(session_id, snippet_id)


def has_snippet_outdated_file_version(session_id: str, snippet: FileSnippet) -> bool:
    return _default_manager.has_snippet_outdated_file_version(session_id, snippet)


def rebuild_session_state_from_history(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Replay persisted tool results to restore snippets + file versions."""
    if not session_id or has_session_state(session_id):
        return
    for message in messages:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        try:
            result = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = result.get("name")
        if name == "read":
            _rebuild_read(session_id, result, metadata)
        elif name == "edit":
            _rebuild_edit(session_id, metadata)
        elif name == "write":
            _rebuild_write(session_id, metadata)


def _refresh_rebuilt_file_state(
    session_id: str,
    raw_file_path: str,
    *,
    scope_type: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    increment_version: bool = False,
) -> None:
    file_path = normalize_file_path(raw_file_path)
    if not file_path or not os.path.isfile(file_path):
        return
    try:
        meta = read_text_file_with_metadata(file_path)
    except Exception:
        return
    is_partial = scope_type == "snippet"
    content = meta["content"]
    if is_partial and start_line is not None:
        content = "\n".join(content.split("\n")[start_line - 1 : end_line])
    record_file_state(
        session_id,
        FileState(
            file_path=file_path,
            content=content,
            timestamp=meta["timestamp"],
            offset=start_line if is_partial else None,
            limit=(
                max(1, end_line - start_line + 1)
                if (is_partial and start_line and end_line)
                else None
            ),
            is_partial_view=is_partial,
            encoding=meta["encoding"],
            line_endings=meta["lineEndings"],
        ),
        increment_version=increment_version,
    )


def _rebuild_read(session_id: str, result: dict, metadata: dict) -> None:
    snippet = metadata.get("snippet")
    if not isinstance(snippet, dict):
        return
    restored = restore_snippet(
        session_id,
        id=str(snippet.get("id", "")),
        file_path=str(snippet.get("filePath", "")),
        start_line=int(snippet.get("startLine", 1)),
        end_line=int(snippet.get("endLine", 1)),
        preview=str(result.get("output", "")),
    )
    if restored:
        _refresh_rebuilt_file_state(
            session_id,
            restored.file_path,
            scope_type=restored.scope_type,
            start_line=restored.start_line,
            end_line=restored.end_line,
        )


def _rebuild_edit(session_id: str, metadata: dict) -> None:
    scope = metadata.get("scope")
    if isinstance(scope, dict):
        restore_snippet(
            session_id,
            id=str(scope.get("snippet_id", "")),
            file_path=str(scope.get("file_path", "")),
            start_line=int(scope.get("start_line", 1)),
            end_line=int(scope.get("end_line", 1)),
            scope_type="full" if metadata.get("read_scope_type") == "full" else "snippet",
        )
    scope_file_path = scope.get("file_path") if isinstance(scope, dict) else None
    _rebuild_candidates(session_id, metadata, scope_file_path)
    file_path = metadata.get("file_path") or scope_file_path
    if file_path and metadata.get("cache_refreshed") is True:
        _refresh_rebuilt_file_state(session_id, str(file_path), increment_version=True)


def _rebuild_candidates(session_id: str, metadata: dict, file_path: str | None) -> None:
    if not file_path:
        return
    candidates = metadata.get("candidates")
    if isinstance(candidates, list):
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            restore_snippet(
                session_id,
                id=str(cand.get("snippet_id", "")),
                file_path=file_path,
                start_line=int(cand.get("start_line", 1)),
                end_line=int(cand.get("end_line", 1)),
                preview=str(cand.get("preview", "")),
                scope_type="snippet",
            )
    closest = metadata.get("closest_match")
    if isinstance(closest, dict):
        restore_snippet(
            session_id,
            id=str(closest.get("snippet_id", "")),
            file_path=file_path,
            start_line=int(closest.get("start_line", 1)),
            end_line=int(closest.get("end_line", 1)),
            preview=str(closest.get("preview", "")),
            scope_type="snippet",
        )


def _rebuild_write(session_id: str, metadata: dict) -> None:
    if metadata.get("cache_refreshed") is True and isinstance(metadata.get("file_path"), str):
        _refresh_rebuilt_file_state(session_id, metadata["file_path"], increment_version=True)

"""Snippet-scoped file state — port of deepcode core/src/common/state.ts.

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


_file_states: dict[str, dict[str, FileState]] = {}
_snippets: dict[str, dict[str, FileSnippet]] = {}
_counters: dict[str, int] = {}
_full_counters: dict[str, int] = {}
_versions: dict[str, dict[str, int]] = {}


def normalize_file_path(file_path: str) -> str:
    if not file_path:
        return ""
    return os.path.normpath(file_path)


def is_absolute_file_path(file_path: str) -> bool:
    if not file_path:
        return False
    return os.path.isabs(file_path)


def clear_session_state(session_id: str) -> None:
    if not session_id:
        return
    _file_states.pop(session_id, None)
    _snippets.pop(session_id, None)
    _counters.pop(session_id, None)
    _full_counters.pop(session_id, None)
    _versions.pop(session_id, None)


def has_session_state(session_id: str) -> bool:
    return bool(
        _file_states.get(session_id)
        or _snippets.get(session_id)
        or session_id in _counters
        or session_id in _full_counters
        or _versions.get(session_id)
    )


def get_file_version(session_id: str, file_path: str) -> int:
    return _versions.get(session_id, {}).get(normalize_file_path(file_path), 0)


def _set_file_version(session_id: str, file_path: str, version: int) -> None:
    _versions.setdefault(session_id, {})[normalize_file_path(file_path)] = version


def record_file_state(session_id: str, state: FileState, increment_version: bool = False) -> None:
    if not session_id or not state.file_path:
        return
    normalized = normalize_file_path(state.file_path)
    current = get_file_version(session_id, normalized)
    next_version = current + 1 if increment_version else current
    _set_file_version(session_id, normalized, next_version)
    state.file_path = normalized
    state.version = next_version
    _file_states.setdefault(session_id, {})[normalized] = state


def mark_file_read(session_id: str, file_path: str, state: dict[str, Any] | None = None) -> None:
    if not session_id or not file_path:
        return
    state = state or {}
    record_file_state(
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


def get_file_state(session_id: str, file_path: str) -> FileState | None:
    if not session_id or not file_path:
        return None
    return _file_states.get(session_id, {}).get(normalize_file_path(file_path))


def was_file_read(session_id: str, file_path: str) -> bool:
    return get_file_state(session_id, file_path) is not None


def is_full_file_view(state: FileState | None) -> bool:
    return bool(
        state and not state.is_partial_view and state.offset is None and state.limit is None
    )


def _store(session_id: str, snippet: FileSnippet) -> FileSnippet:
    _snippets.setdefault(session_id, {})[snippet.id] = snippet
    return snippet


def _create_with_id(
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
        file_version=get_file_version(session_id, file_path),
        scope_type=scope_type,
    )
    return _store(session_id, snippet)


def create_snippet(
    session_id: str, file_path: str, start_line: int, end_line: int, preview: str
) -> FileSnippet | None:
    nxt = _counters.get(session_id, 0) + 1
    _counters[session_id] = nxt
    return _create_with_id(
        session_id, file_path, start_line, end_line, preview, f"snippet_{nxt}", "snippet"
    )


def create_full_file_snippet(
    session_id: str, file_path: str, start_line: int, end_line: int, preview: str
) -> FileSnippet | None:
    nxt = _full_counters.get(session_id, 0)
    _full_counters[session_id] = nxt + 1
    return _create_with_id(
        session_id, file_path, start_line, end_line, preview, f"full_file_{nxt}", "full"
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
    if not session_id or not id or not file_path or start_line < 1 or end_line < start_line:
        return None
    scope = scope_type or ("full" if id.startswith("full_file_") else "snippet")
    restored = _create_with_id(session_id, file_path, start_line, end_line, preview, id, scope)
    if restored:
        if id.startswith("full_file_"):
            try:
                n = int(id.split("_")[2])
                _full_counters[session_id] = max(_full_counters.get(session_id, 0), n + 1)
            except (IndexError, ValueError):
                pass
        elif id.startswith("snippet_"):
            try:
                n = int(id.split("_")[1])
                _counters[session_id] = max(_counters.get(session_id, 0), n)
            except (IndexError, ValueError):
                pass
    return restored


def get_snippet(session_id: str, snippet_id: str) -> FileSnippet | None:
    if not session_id or not snippet_id:
        return None
    return _snippets.get(session_id, {}).get(snippet_id)


def has_snippet_outdated_file_version(session_id: str, snippet: FileSnippet) -> bool:
    return get_file_version(session_id, snippet.file_path) > snippet.file_version


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

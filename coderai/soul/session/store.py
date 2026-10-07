"""Project-local JSONL session storage and index management."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

from coderai.events import SessionEvent, legacy_message_to_event
from coderai.utils.storage import owned_path, storage_id, read_bytes, write_bytes, parent_directory


def get_project_code(project_root: str) -> str:
    """Return the stable directory name used by legacy global storage."""
    normalized = str(pathlib.Path(project_root).resolve())
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    base = pathlib.Path(normalized).name[:32].replace(" ", "-") or "project"
    return f"{base}-{digest}"


class JsonlSessionStore:
    """Own the session index and mixed legacy/event JSONL logs for one project."""

    def __init__(self, project_root: str, *, max_entries: int = 50, cleanup: bool = True) -> None:
        self.project_root = str(pathlib.Path(project_root).resolve())
        self.max_entries = max_entries
        self.project_dir, self.index_path = self._resolve_storage()
        if cleanup:
            self.cleanup_orphan_tmps()

    def cleanup_orphan_tmps(self) -> int:
        """Remove leftover atomic-write temp files; returns the count removed."""
        removed = 0
        try:
            entries = list(self.project_dir.iterdir())
        except OSError:
            return 0
        for path in entries:
            name = path.name
            # Accept both legacy and shared atomic-writer crash leftovers.
            if (
                not re.search(r"(?:\.tmp-[0-9a-f]+|\.[0-9a-f]{12,16}\.tmp)$", name)
                or not path.is_file()
            ):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def _resolve_storage(self) -> tuple[pathlib.Path, pathlib.Path]:
        local_dir = owned_path(pathlib.Path(self.project_root), ".coderai", "sessions")
        from coderai.share import get_share_dir

        global_dir = owned_path(
            get_share_dir().resolve(), "projects", get_project_code(self.project_root)
        )
        try:
            if (
                not (local_dir / "sessions-index.json").exists()
                and (global_dir / "sessions-index.json").exists()
            ):
                self._migrate(global_dir, local_dir)
            with parent_directory(local_dir / "sessions-index.json", create=True):
                pass
            return local_dir, local_dir / "sessions-index.json"
        except OSError:
            with parent_directory(global_dir / "sessions-index.json", create=True):
                pass
            return global_dir, global_dir / "sessions-index.json"

    @staticmethod
    def _migrate(source: pathlib.Path, destination: pathlib.Path) -> None:
        """Migrate completely before selecting the project-local session index."""
        if not source.is_dir():
            return
        from coderai.cli.migrate import migrate_sessions

        # Function-local import avoids a cycle: the explicit CLI uses this
        # module's stable project-code helper to find the legacy store.
        migrate_sessions(source, destination.parent.parent)

    def storage_paths(self) -> dict[str, pathlib.Path]:
        return {"project_dir": self.project_dir, "index_path": self.index_path}

    def messages_path(self, session_id: str) -> pathlib.Path:
        return owned_path(self.project_dir) / f"{storage_id(session_id)}.jsonl"

    def load_index(self) -> dict[str, Any]:
        empty = {"version": 1, "entries": [], "originalPath": self.project_root}
        if not self.index_path.exists():
            return empty
        try:
            data = json.loads(read_bytes(self.index_path, limit=4_000_000).decode("utf-8"))
        except (OSError, ValueError, TypeError):
            return empty
        if not isinstance(data, dict):
            return empty
        entries = data.get("entries")
        return {
            "version": 1,
            "entries": entries if isinstance(entries, list) else [],
            "originalPath": self.project_root,
        }

    def save_index(self, index: dict[str, Any]) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        entries = index.get("entries")
        if not isinstance(entries, list):
            entries = []
        if len(entries) > self.max_entries:
            entries = sorted(
                entries,
                key=lambda entry: (
                    entry.get("updateTime") or entry.get("createTime") or ""
                    if isinstance(entry, dict)
                    else ""
                ),
                reverse=True,
            )[: self.max_entries]
        index["entries"] = entries
        write_bytes(self.index_path, json.dumps(index, indent=2).encode("utf-8"))

    def append_row(self, session_id: str, row: dict[str, Any]) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        write_bytes(
            self.messages_path(session_id),
            (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"),
            append=True,
        )

    def replace_rows(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        target = self.messages_path(session_id)
        write_bytes(
            target,
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8"),
        )

    def read_rows(self, session_id: str) -> list[dict[str, Any]]:
        path = self.messages_path(session_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = (
                read_bytes(path, limit=128_000_000).decode("utf-8", errors="replace").splitlines()
            )
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def read_raw_lines(self, session_id: str) -> list[str]:
        path = self.messages_path(session_id)
        if not path.exists():
            return []
        try:
            return [
                line.strip()
                for line in read_bytes(path, limit=128_000_000)
                .decode("utf-8", errors="replace")
                .splitlines()
                if line.strip()
            ]
        except OSError:
            return []

    def write_raw_lines(self, session_id: str, lines: list[str]) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        target = self.messages_path(session_id)
        write_bytes(target, "".join(line + "\n" for line in lines).encode("utf-8"))

    def list_events(self, session_id: str) -> list[SessionEvent]:
        """Read event rows and adapt legacy message rows without rewriting the log."""
        events: list[SessionEvent] = []
        for sequence, row in enumerate(self.read_rows(session_id)):
            try:
                if "type" in row:
                    events.append(SessionEvent.from_dict(row))
                elif "role" in row:
                    events.append(legacy_message_to_event(sequence, row, session_id=session_id))
            except (ValueError, TypeError, KeyError):
                continue
        return events

    def validate_and_repair_invariants(self, session_id: str) -> list[str]:
        """Validate session invariants from event stream and automatically synthesize missing aborts if needed."""
        from coderai.utils.common.invariants import (
            verify_paired_tool_calls,
            verify_session_invariants,
        )
        from coderai.tools.legacy.types import TOOL_ABORTED_BEFORE_DISPATCH

        rows = self.read_rows(session_id)
        if not rows:
            return []

        violations = verify_session_invariants(rows)
        if not violations:
            return []

        # Self-healing: check if there are dangling tool calls that need synthetic abort results
        paired_violations = verify_paired_tool_calls(rows)
        if paired_violations:
            repaired_rows: list[dict[str, Any]] = []
            pending_in_turn: dict[str, dict[str, Any]] = {}
            for row in rows:
                role = row.get("role")
                if role in ("user", "system") and pending_in_turn:
                    # Synthesize missing tool messages before starting new turn
                    for tc_id, tc in list(pending_in_turn.items()):
                        repaired_rows.append(
                            {
                                "id": f"repair_{tc_id}",
                                "session_id": session_id,
                                "role": "tool",
                                "content": json.dumps(
                                    {
                                        "ok": False,
                                        "name": (tc.get("function") or {}).get("name", "tool"),
                                        "error": TOOL_ABORTED_BEFORE_DISPATCH,
                                        "message": "Self-healing repair synthesized aborted tool result.",
                                    }
                                ),
                                "tool_call_id": tc_id,
                                "meta": {"function": tc.get("function")},
                            }
                        )
                    pending_in_turn.clear()

                repaired_rows.append(row)

                if role == "assistant" and row.get("tool_calls"):
                    for tc in row.get("tool_calls") or []:
                        tc_id = tc.get("id")
                        if tc_id:
                            pending_in_turn[str(tc_id)] = tc
                elif role == "tool" and row.get("tool_call_id"):
                    pending_in_turn.pop(str(row.get("tool_call_id")), None)

            if pending_in_turn:
                for tc_id, tc in list(pending_in_turn.items()):
                    repaired_rows.append(
                        {
                            "id": f"repair_{tc_id}",
                            "session_id": session_id,
                            "role": "tool",
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "name": (tc.get("function") or {}).get("name", "tool"),
                                    "error": TOOL_ABORTED_BEFORE_DISPATCH,
                                    "message": "Self-healing repair synthesized aborted tool result.",
                                }
                            ),
                            "tool_call_id": tc_id,
                            "meta": {"function": tc.get("function")},
                        }
                    )
                pending_in_turn.clear()

            self.replace_rows(session_id, repaired_rows)

        return violations

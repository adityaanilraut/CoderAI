"""Explicit, non-destructive migration of legacy global CoderAI sessions.

This migrates CoderAI's legacy project JSONL store, preserving logs, session
state, checkpoints and images. It does not translate other products' schemas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from coderai.soul.session.store import get_project_code
from coderai.utils.io import atomic_json_write


def _load_index(path: Path, *, optional: bool = False) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"Session index must not be a symlink: {path}")
    if optional and not path.exists():
        return {"version": 1, "entries": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"Invalid session index: {path}")
    for entry in data["entries"]:
        sid = entry.get("id") if isinstance(entry, dict) else None
        if (
            not isinstance(sid, str)
            or not sid
            or sid in {".", ".."}
            or any(c in sid for c in "/\\\x00")
        ):
            raise ValueError(f"Invalid session id in index: {path}")
    return data


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _file_action(source: Path, target: Path, destination: Path) -> str:
    if source.is_symlink() or target.is_symlink():
        return "conflict"
    if not target.resolve().is_relative_to(destination.resolve()):
        return "conflict"
    if not target.exists():
        return "copy"
    if not target.is_file():
        return "conflict"
    return "existing" if _digest(source) == _digest(target) else "conflict"


def _copy_new_file(source: Path, target: Path) -> None:
    """Publish a complete copy atomically without overwriting any destination."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".migration-", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        # Unlike replace(), link() never clobbers a destination created after
        # collision inspection. The temporary and target share a filesystem.
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def migrate_sessions(source: Path, work_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Copy a supported legacy store, committing the merged index last."""
    source = source.expanduser().resolve()
    work_dir = work_dir.expanduser().resolve()
    destination = work_dir / ".coderai" / "sessions"
    if (work_dir / ".coderai").is_symlink() or destination.is_symlink():
        raise ValueError("Migration destination must not be a symlink")
    if (
        source == destination.resolve()
        or source.is_relative_to(destination.resolve())
        or destination.resolve().is_relative_to(source)
    ):
        raise ValueError("Migration source and destination must not overlap")
    source_index = _load_index(source / "sessions-index.json")
    target_index = _load_index(destination / "sessions-index.json", optional=True)
    report: dict[str, Any] = {
        "version": 1,
        "scope": "legacy-coderai-project-sessions",
        "source": str(source),
        "destination": str(destination),
        "dryRun": dry_run,
        "files": {"copied": [], "existing": [], "conflicts": []},
        "sessions": {"imported": [], "existing": [], "conflicts": [], "missingLogs": []},
    }
    lock = destination / ".migration.lock"
    lock_fd: int | None = None
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ValueError(f"Another migration holds {lock}") from error
    try:
        if lock_fd is not None:
            os.write(lock_fd, str(os.getpid()).encode())
        # Inspect the destination under the lock; a concurrent migration may
        # have committed between preflight and lock acquisition.
        if not dry_run:
            target_index = _load_index(destination / "sessions-index.json", optional=True)
        existing_entries = {entry["id"]: entry for entry in target_index["entries"]}
        blocked_sessions = {
            entry["id"]
            for entry in source_index["entries"]
            if (entry["id"] in existing_entries and existing_entries[entry["id"]] != entry)
            or (
                (source / f"{entry['id']}.jsonl").is_file()
                and _file_action(
                    source / f"{entry['id']}.jsonl",
                    destination / f"{entry['id']}.jsonl",
                    destination,
                )
                == "conflict"
            )
        }
        checkpoint_files = list((source / "file-history").rglob("*"))
        preserve_checkpoints = any(
            path.is_file()
            and _file_action(path, destination / path.relative_to(source), destination)
            == "conflict"
            for path in checkpoint_files
        )
        actions: dict[str, str] = {}
        for path in sorted(source.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(source)
            if (
                relative.name == "sessions-index.json"
                or relative.name.startswith(".migration")
                or ".tmp-" in relative.name
            ):
                continue
            action = _file_action(path, destination / relative, destination)
            if (
                relative.parts[0] in blocked_sessions
                or relative.name in {f"{sid}.jsonl" for sid in blocked_sessions}
                or (relative.parts[0] == "file-history" and preserve_checkpoints)
            ):
                action = "conflict"
            actions[str(relative)] = action
            if action == "copy" and not dry_run:
                _copy_new_file(path, destination / relative)
            report["files"][
                {"copy": "copied", "existing": "existing", "conflict": "conflicts"}[action]
            ].append(str(relative))
        entries = list(target_index["entries"])
        known = {entry["id"]: entry for entry in entries}
        for entry in source_index["entries"]:
            sid = entry["id"]
            log_action = actions.get(f"{sid}.jsonl")
            if log_action is None:
                report["sessions"]["missingLogs"].append(sid)
            elif log_action == "conflict":
                report["sessions"]["conflicts"].append(sid)
            elif sid in known:
                category = "existing" if known[sid] == entry else "conflicts"
                report["sessions"][category].append(sid)
            else:
                entries.append(entry)
                known[sid] = entry
                report["sessions"]["imported"].append(sid)
        if not dry_run:
            merged = {
                **target_index,
                "version": 1,
                "originalPath": str(work_dir),
                "entries": entries,
            }
            atomic_json_write(merged, destination / "sessions-index.json", mode=0o600)
            atomic_json_write(report, destination / "migration-report.json", mode=0o600)
        return report
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock.unlink(missing_ok=True)


def run_migrate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="coderai migrate",
        description="Copy legacy global CoderAI project sessions into workspace storage without removing the source.",
    )
    parser.add_argument(
        "--work-dir", default=".", help="Destination workspace (default: current directory)"
    )
    parser.add_argument("--source", help="Legacy project store containing sessions-index.json")
    parser.add_argument(
        "--dry-run", action="store_true", help="Inspect and report without changing session storage"
    )
    parser.add_argument("--report", help="Write the JSON migration report to this file")
    args = parser.parse_args(argv)
    work_dir = Path(args.work_dir).expanduser().resolve()
    if args.source:
        source = Path(args.source)
    else:
        share_dir = Path(
            os.environ.get("CODERAI_SHARE_DIR") or (Path.home() / ".coderai")
        ).expanduser()
        source = share_dir / "projects" / get_project_code(str(work_dir))
    try:
        report = migrate_sessions(source, work_dir, dry_run=args.dry_run)
        if args.report:
            atomic_json_write(report, Path(args.report).expanduser(), mode=0o600)
    except (OSError, ValueError) as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0

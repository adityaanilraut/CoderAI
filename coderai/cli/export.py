# Ported from coderai/cli/export_cmd.py - kimi structure (cli/export.py).
"""``coderai export`` subcommand (Kimi ``cli/export.py`` parity, stdlib only).

Packages a session directory + recent diagnostics into a distributable ZIP:

- session files: ``<id>.jsonl``, ``<id>/state.json``, ``images/<id>/``
- recent ``~/.coderai/logs/coderai.log*`` files near session activity (±2d)
- ``manifest.json`` with CLI/python/OS versions + activity window

Kimi names: ``session-<id>.zip`` default output; ``-o`` override; ``-y``
skips the "export previous session?" confirmation.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import platform
import time
import zipfile
from pathlib import Path
from typing import Any

_LOG_RETENTION_SECONDS = 2 * 24 * 60 * 60  # 2 days, mirrors Kimi
_MAX_LOG_BYTES = 100 * 1024 * 1024  # 100 MB cap, mirrors Kimi


def _version() -> str:
    try:
        from coderai._version import __version__

        return __version__
    except Exception:
        return "0.0.0"


def _project_dir(project_root: str) -> Path:
    from coderai.core.session_store import JsonlSessionStore

    return JsonlSessionStore(project_root).project_dir


def _find_session_files(project_root: str, session_id: str) -> list[Path]:
    """Collect on-disk files belonging to ``session_id`` (index prefix ok)."""
    from coderai.core.session_store import JsonlSessionStore

    store = JsonlSessionStore(project_root)
    resolved = session_id
    for entry in (store.load_index().get("entries") or []):
        if isinstance(entry, dict) and str(entry.get("id", "")).startswith(session_id):
            resolved = str(entry["id"])
            break
    base = store.project_dir
    candidates = [
        base / f"{resolved}.jsonl",
        base / resolved / "state.json",
        base / "sessions-index.json",
    ]
    images = base / "images" / resolved
    if images.is_dir():
        candidates.extend(sorted(p for p in images.rglob("*") if p.is_file()))
    return [p for p in candidates if p.is_file()]


def _previous_session_id(project_root: str) -> str | None:
    from coderai.core.session_store import JsonlSessionStore

    entries = JsonlSessionStore(project_root).load_index().get("entries") or []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id"):
            return str(entry["id"])
    try:
        from coderai.metadata import get_last_session_id

        return get_last_session_id(project_root)
    except Exception:
        return None


def _session_time_range(paths: list[Path]) -> tuple[float | None, float | None]:
    """Best-effort (first, last) unix timestamps from JSONL ``timestamp`` fields."""
    first: float | None = None
    last: float | None = None
    for path in paths:
        if path.suffix != ".jsonl":
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    ts = row.get("timestamp")
                    if isinstance(ts, (int, float)):
                        value = float(ts / 1000.0) if ts > 1e12 else float(ts)
                    elif isinstance(row.get("createTime"), str):
                        try:
                            value = datetime.datetime.fromisoformat(
                                row["createTime"]
                            ).timestamp()
                        except ValueError:
                            continue
                    else:
                        continue
                    first = value if first is None else min(first, value)
                    last = value if last is None else max(last, value)
        except OSError:
            continue
    return first, last


def _collect_recent_log_files(
    first_ts: float | None, last_ts: float | None
) -> list[Path]:
    """Recent ``coderai.log*`` files near session activity or export time."""
    from coderai.core.share import get_share_dir

    log_dir = get_share_dir() / "logs"
    if not log_dir.is_dir():
        return []
    now = time.time()
    export_cutoff = now - _LOG_RETENTION_SECONDS
    session_cutoff = session_upper = None
    if first_ts is not None:
        session_cutoff = first_ts - _LOG_RETENTION_SECONDS
        session_upper = (last_ts or first_ts) + _LOG_RETENTION_SECONDS
    candidates: list[tuple[float, int, Path]] = []
    for item in log_dir.iterdir():
        if not item.is_file() or not item.name.startswith("coderai."):
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        if stat.st_mtime >= export_cutoff:
            candidates.append((stat.st_mtime, stat.st_size, item))
        elif (
            session_cutoff is not None
            and session_upper is not None
            and session_cutoff <= stat.st_mtime <= session_upper
        ):
            candidates.append((stat.st_mtime, stat.st_size, item))
    anchor = last_ts or first_ts or now
    candidates.sort(key=lambda item: abs(item[0] - anchor))
    collected: list[tuple[float, Path]] = []
    total = 0
    for mtime, size, path in candidates:
        if total + size > _MAX_LOG_BYTES:
            break
        collected.append((mtime, path))
        total += size
    collected.sort(key=lambda item: item[0])
    return [path for _, path in collected]


def _build_manifest(session_id: str, first_ts: float | None, last_ts: float | None) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "session_id": session_id,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "coderai_version": _version(),
        "python_version": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.machine(),
    }
    if first_ts is not None:
        manifest["session_first_activity"] = datetime.datetime.fromtimestamp(
            first_ts, datetime.timezone.utc
        ).isoformat()
    if last_ts is not None:
        manifest["session_last_activity"] = datetime.datetime.fromtimestamp(
            last_ts, datetime.timezone.utc
        ).isoformat()
    return manifest


def run_export(argv: list[str], *, project_root: str) -> int:
    """Run ``coderai export [session_id] [-o path] [--yes]``. Returns exit code."""
    session_id: str | None = None
    output: str | None = None
    yes = False
    idx = 0
    while idx < len(argv):
        tok = argv[idx]
        if tok in ("-o", "--output") and idx + 1 < len(argv):
            output = argv[idx + 1]
            idx += 2
        elif tok.startswith("-o") and len(tok) > 2:
            output = tok[2:]
            idx += 1
        elif tok.startswith("--output="):
            output = tok.split("=", 1)[1]
            idx += 1
        elif tok in ("-y", "--yes"):
            yes = True
            idx += 1
        elif tok in ("-h", "--help"):
            print(
                "Usage: coderai export [<session_id>] [-o <output.zip>] [--yes]\n"
                "\n"
                "Export a session as a ZIP archive (session files + recent logs)."
            )
            return 0
        elif tok.startswith("-"):
            print(f"Unknown option for 'export': {tok}")
            return 2
        elif session_id is None:
            session_id = tok
            idx += 1
        else:
            print(f"Unexpected argument for 'export': {tok}")
            return 2

    if session_id is None:
        previous = _previous_session_id(project_root)
        if previous is None:
            print("Error: no previous session found for the working directory.")
            return 1
        if not yes:
            answer = input(
                f"Export previous session {previous[:16]}…? [y/N] "
            ).strip().lower()
            if answer not in ("y", "yes"):
                print("Export cancelled.")
                return 0
        session_id = previous

    files = _find_session_files(project_root, session_id)
    if not files:
        print(f"Error: session '{session_id}' not found or has no files.")
        return 1

    first_ts, last_ts = _session_time_range(files)
    log_files = _collect_recent_log_files(first_ts, last_ts)
    manifest = _build_manifest(session_id, first_ts, last_ts)

    out_path = Path(output).expanduser() if output else Path.cwd() / f"session-{session_id}.zip"
    buf = io.BytesIO()
    base = _project_dir(project_root)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for file_path in sorted(files):
            with contextlib.suppress(OSError):
                try:
                    arcname = str(file_path.relative_to(base))
                except ValueError:
                    arcname = file_path.name
                archive.write(file_path, arcname=arcname)
        for log_path in log_files:
            with contextlib.suppress(OSError):
                archive.write(log_path, arcname=f"logs/{log_path.name}")
    buf.seek(0)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(buf.getvalue())
    except OSError as exc:
        print(f"Error: cannot write '{out_path}': {exc}")
        return 1

    print(str(out_path))
    if log_files:
        print(
            "Note: this archive includes recent diagnostic logs that may contain "
            "file paths or commands from other sessions. Review before sharing."
        )
    return 0

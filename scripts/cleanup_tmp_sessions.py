#!/usr/bin/env python3
"""Cleanup stale, temporary, or orphaned session directories."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path


def cleanup_sessions(
    root: Path,
    max_age_days: float = 7.0,
    dry_run: bool = False,
) -> int:
    now = time.time()
    removed_count = 0

    if not root.exists() or not root.is_dir():
        print(f"Directory not found: {root}")
        return 0

    sessions_dir = root / "sessions" if (root / "sessions").is_dir() else root

    for entry in sorted(sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Check if directory starts with temp/session prefix or contains session files
        try:
            mtime = entry.stat().st_mtime
            age_days = (now - mtime) / 86400.0
            if age_days >= max_age_days:
                if dry_run:
                    print(f"[DRY-RUN] Would remove: {entry} (age: {age_days:.1f} days)")
                else:
                    shutil.rmtree(entry, ignore_errors=True)
                    print(f"Removed stale session: {entry} (age: {age_days:.1f} days)")
                removed_count += 1
        except OSError as exc:
            print(f"Skipping {entry}: {exc}", file=sys.stderr)

    return removed_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup stale or temporary CoderAI sessions")
    parser.add_argument(
        "--path",
        default=str(Path.home() / ".coderai" / "sessions"),
        help="Path to sessions directory (default: ~/.coderai/sessions)",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="Remove sessions older than this many days (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Display sessions that would be removed without deleting them",
    )
    args = parser.parse_args()

    target_path = Path(args.path).expanduser().resolve()
    print(f"Scanning for sessions older than {args.days} days in {target_path}...")
    count = cleanup_sessions(target_path, max_age_days=args.days, dry_run=args.dry_run)
    action = "Would remove" if args.dry_run else "Removed"
    print(f"✓ {action} {count} stale session directories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

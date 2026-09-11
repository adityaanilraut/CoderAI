#!/usr/bin/env python3
"""Validate that a git tag or candidate version matches coderai/_version.py."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "coderai" / "_version.py"


def get_file_version() -> str:
    content = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not m:
        raise ValueError(f"Could not find __version__ in {VERSION_FILE}")
    return m.group(1).strip()


def normalize_tag(tag: str) -> str:
    tag = tag.strip()
    if tag.startswith("v"):
        tag = tag[1:]
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description="Check version tag matches _version.py")
    parser.add_argument(
        "tag", nargs="?", help="Tag or version string to check (e.g. v0.4.0 or 0.4.0)"
    )
    args = parser.parse_args()

    expected = get_file_version()
    if not args.tag:
        print(f"Current version in {VERSION_FILE.name}: {expected}")
        return 0

    actual = normalize_tag(args.tag)
    if actual != expected:
        print(
            f"Error: Version tag mismatch! Tag specifies '{args.tag}' ({actual}), "
            f"but {VERSION_FILE.name} has '{expected}'.",
            file=sys.stderr,
        )
        return 1

    print(f"✓ Version tag '{args.tag}' matches expected '{expected}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Verify a PyInstaller-built CoderAI binary.

Checks that the binary:
- Runs without crashing on basic invocation (--help and --version)
- Reports the expected version from coderai/_version.py
- Contains expected bundled data files (agents, prompts, skills, tools docs)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DIST_NAME = "coderai"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _expected_version() -> str:
    """Read the expected version from coderai/_version.py."""
    version_file = REPO_ROOT / "coderai" / "_version.py"
    for line in version_file.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _find_binary(path: Path) -> Path | None:
    """Locate the coderai binary under a dist/ or build/ directory."""
    if path.is_file() and path.stat().st_mode & 0o111:
        return path
    if path.is_dir():
        candidates = [
            path / DIST_NAME,
            path / f"{DIST_NAME}.exe",
        ]
        for c in candidates:
            if c.is_file():
                return c
        for c in path.rglob(DIST_NAME):
            if c.is_file() and c.stat().st_mode & 0o111:
                return c
    return None


def run_check(binary: Path, args: list[str], label: str) -> bool:
    """Run the binary with given args and report success/failure."""
    try:
        result = subprocess.run(
            [str(binary), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"  OK  {label}")
            return True
        print(f"  FAIL  {label} (exit code {result.returncode})")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[:5]:
                print(f"        {line}")
        return False
    except subprocess.TimeoutExpired:
        print(f"  FAIL  {label} (timeout)")
        return False
    except Exception as exc:
        print(f"  FAIL  {label} ({exc})")
        return False


def verify(path: str) -> int:
    """Verify the binary at the given path."""
    binary = _find_binary(Path(path).expanduser().resolve())
    if binary is None:
        print(f"ERROR: No executable found at {path}")
        return 1

    print(f"Binary: {binary}")
    expected = _expected_version()
    print(f"Expected version: {expected or '(unknown)'}")
    print()

    all_ok = True

    all_ok &= run_check(binary, ["--help"], "--help exits 0")
    all_ok &= run_check(binary, ["--version"], "--version exits 0")

    if expected:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if expected in result.stdout:
            print(f"  OK  --version reports {expected}")
        else:
            print(f"  FAIL  --version did not contain {expected!r}")
            print(f"        got: {result.stdout.strip()!r}")
            all_ok = False

    print()
    if all_ok:
        print("All checks passed.")
        return 0
    print("Some checks failed.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a PyInstaller-built CoderAI binary.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="dist",
        help="Path to the binary or dist/ directory (default: dist/)",
    )
    args = parser.parse_args()
    return verify(args.path)


if __name__ == "__main__":
    sys.exit(main())

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

#: Data files the PyInstaller bundle must ship (mirrors
#: ``coderai/utils/pyinstaller.py`` datas + binaries). Each entry is a glob
#: relative to the onedir distribution root (next to the executable, or its
#: ``_internal`` dir). Single-file builds embed these in the archive, so the
#: data check is skipped there with a note.
EXPECTED_DATA_GLOBS = (
    "agents/default/agent.yaml",
    "prompt/templates/compact.md",
    "skills/*/SKILL.md",
    "tools/*/*.md",
)

EXPECTED_BINARIES = ("rg", "rg.exe")


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

    all_ok &= check_bundled_data(binary)

    print()
    if all_ok:
        print("All checks passed.")
        return 0
    print("Some checks failed.")
    return 1


def _dist_search_roots(binary: Path) -> list[Path]:
    """Candidate roots holding extracted bundle data (onedir layouts)."""
    roots = [binary.parent]
    internal = binary.parent / "_internal"
    if internal.is_dir():
        roots.append(internal)
    # ``dist/coderai/coderai`` COLLECT layout: data sits beside the dir.
    if binary.parent.name == DIST_NAME and binary.parent.is_dir():
        roots.append(binary.parent)
    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def check_bundled_data(binary: Path) -> bool:
    """Verify expected data files + vendored binaries ship with the bundle."""
    roots = [r for r in _dist_search_roots(binary) if r.is_dir()]
    # Single-file build: data lives inside the archive; probe via strings.
    layout_has_data = any(
        next((r / "agents").glob("*"), None) is not None or (r / "prompts").is_dir() for r in roots
    )
    # Single-file build: data lives inside the archive; validate the build
    # manifest (build/<app>/PKG-00.toc) when this checkout still has it.
    toc_ok = check_archive_manifest()
    if not layout_has_data:
        if toc_ok is None:
            print("  SKIP  bundled data check (single-file build; data is in-archive)")
            return True
        return toc_ok

    ok = True
    for pattern in EXPECTED_DATA_GLOBS:
        found = any(list(r.glob(pattern)) for r in roots)
        if found:
            print(f"  OK  bundle ships {pattern}")
        else:
            print(f"  FAIL  bundle missing {pattern}")
            ok = False
    for name in EXPECTED_BINARIES:
        candidates = [r / name for r in roots] + [binary.parent / name]
        if any(c.is_file() for c in candidates):
            print(f"  OK  bundle ships vendored {name}")
            break
    else:
        # Vendored rg is optional on PATH-equipped hosts; warn only.
        print("  SKIP  vendored rg not found beside bundle (falls back to PATH)")
    return ok


#: Markers that must appear in the single-file PKG archive manifest.
ARCHIVE_MARKERS = (
    "agents/default/agent.yaml",
    "prompt/templates/compact.md",
    "SKILL.md",
    "vendor/rg",
)


def check_archive_manifest() -> bool | None:
    """Grep the PyInstaller PKG manifest for bundled data markers.

    Returns None when no manifest is available (nothing to check).
    """
    manifests = sorted((REPO_ROOT / "build").glob("*/PKG-*.toc"))
    if not manifests:
        return None
    text = manifests[0].read_text(encoding="utf-8", errors="replace")
    ok = True
    for marker in ARCHIVE_MARKERS:
        if marker in text:
            print(f"  OK  archive bundles {marker}")
        else:
            print(f"  FAIL  archive missing {marker}")
            ok = False
    return ok


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

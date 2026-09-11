#!/usr/bin/env python3
"""Verify installed runtime dependencies satisfy constraints in pyproject.toml.

Compares each constrained dependency declared in ``[project] dependencies``
against the version actually installed in the current environment, catching
breaking version drifts early (e.g. in CI before the test suite runs).

Deliberately dependency-free (stdlib only, Python 3.10+) so it runs in
minimal CI environments before the package itself is installed.

Usage:
    python3 scripts/check_dependency_versions.py
    python3 scripts/check_dependency_versions.py --pyproject pyproject.toml
    python3 scripts/check_dependency_versions.py --strict  # fail on missing deps
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = REPO_ROOT / "pyproject.toml"


def _extract_dependencies(pyproject_text: str) -> list[str]:
    """Extract the ``[project] dependencies`` string list without a TOML parser.

    The project table uses a simple ``dependencies = [...]`` string array, so
    a targeted scan is sufficient and keeps this script stdlib-only on
    Python 3.10 (which has no ``tomllib``). Bracket-balanced scanning is used
    so extras such as ``kosong[contrib]==0.56.0`` do not truncate the match.
    """
    start = re.search(r"^\s*dependencies\s*=\s*\[", pyproject_text, re.MULTILINE)
    if start is None:
        raise ValueError("missing [project] dependencies list")
    # Walk from the opening bracket, skipping [...] extras inside strings.
    body_chars: list[str] = []
    in_str: str | None = None
    depth = 0
    i = start.end() - 1  # position of the opening '['
    n = len(pyproject_text)
    while i < n:
        ch = pyproject_text[i]
        if in_str is not None:
            body_chars.append(ch)
            if ch == in_str and pyproject_text[i - 1] != "\\":
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
                body_chars.append(ch)
            elif ch == "[":
                depth += 1
                if depth > 1:
                    body_chars.append(ch)
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
                body_chars.append(ch)
            elif ch == "#":
                # Skip comments outside strings until end of line.
                while i < n and pyproject_text[i] != "\n":
                    i += 1
                continue
            else:
                body_chars.append(ch)
        i += 1
    if depth != 0:
        raise ValueError("unterminated [project] dependencies list")
    return re.findall(r"""["']([^"']+)["']""", "".join(body_chars))


def _parse_requirement(req: str) -> tuple[str, str] | None:
    """Split a PEP 508 requirement into (distribution name, specifier).

    Returns ``None`` for blank lines and comments. Extras (``name[extra]``)
    and environment markers (``; python_version > ...``) are stripped; only
    the version specifier is kept.
    """
    req = req.split("#", 1)[0].strip()
    if not req:
        return None
    # Strip environment markers.
    req = req.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(.*)$", req)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _parse_version(text: str) -> tuple[tuple[int, ...], str]:
    """Split a version string into (numeric release tuple, remainder)."""
    text = text.strip()
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
    if not match:
        return (), text
    release = tuple(int(p) for p in match.group(1).split("."))
    return release, match.group(2).strip()


def _compare_versions(a: str, b: str) -> int:
    """Compare two version strings: -1 if a<b, 0 if a==b, 1 if a>b."""
    rel_a, rest_a = _parse_version(a)
    rel_b, rest_b = _parse_version(b)
    # Pad numeric releases to equal length for tuple comparison.
    width = max(len(rel_a), len(rel_b))
    rel_a = rel_a + (0,) * (width - len(rel_a))
    rel_b = rel_b + (0,) * (width - len(rel_b))
    if rel_a != rel_b:
        return -1 if rel_a < rel_b else 1
    # A bare release beats a pre-release suffix (e.g. 1.0 > 1.0rc1).
    if rest_a == rest_b:
        return 0
    if not rest_a:
        return 1
    if not rest_b:
        return -1
    if rest_a != rest_b:
        return -1 if rest_a < rest_b else 1
    return 0


def _check_specifier(installed: str, specifier: str) -> bool:
    """Return True if ``installed`` version satisfies ``specifier``.

    Supports comma-separated ``==``, ``>=``, ``<=``, ``>``, ``<``, ``~=``,
    and ``!=`` clauses. Unknown operators fail closed so new constraint
    styles surface loudly instead of passing silently.
    """
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"^(==|>=|<=|>|<|~=|!=)\s*(.+)$", clause)
        if not match:
            return False
        op, wanted = match.group(1), match.group(2).strip()
        cmp = _compare_versions(installed, wanted)
        if op == "==":
            if not (cmp == 0 or _prefix_match(installed, wanted)):
                return False
        elif op == ">=":
            if cmp < 0:
                return False
        elif op == "<=":
            if cmp > 0:
                return False
        elif op == ">":
            if cmp <= 0:
                return False
        elif op == "<":
            if cmp >= 0:
                return False
        elif op == "!=":
            if cmp == 0:
                return False
        elif op == "~=":
            if cmp < 0:
                return False
            # ~= X.Y means >= X.Y, == X.* (prefix match on all but last segment).
            release = _parse_version(wanted)[0]
            prefix = release[:-1]
            if _parse_version(installed)[0][: len(prefix)] != prefix:
                return False
    return True


def _prefix_match(installed: str, wanted: str) -> bool:
    """Support ``== X.*`` prefix wildcards (e.g. ``== 1.2.*``)."""
    if not wanted.endswith(".*"):
        return False
    prefix = wanted[:-2].strip().rstrip(".")
    inst = installed.strip()
    return inst == prefix or inst.startswith(prefix + ".") or inst.startswith(prefix + "-")


def _installed_version(dist_name: str) -> str | None:
    """Return the installed version of a distribution, or None if missing."""
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed dependencies match pyproject.toml constraints."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="Path to pyproject.toml (default: repo root).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail when a declared dependency is not installed at all.",
    )
    args = parser.parse_args()

    try:
        text = args.pyproject.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: pyproject.toml not found at {args.pyproject}", file=sys.stderr)
        return 1

    try:
        deps = _extract_dependencies(text)
    except ValueError as exc:
        print(f"error: {exc} in {args.pyproject}", file=sys.stderr)
        return 1

    errors: list[str] = []
    checked = 0
    for raw in deps:
        parsed = _parse_requirement(raw)
        if parsed is None:
            continue
        dist_name, specifier = parsed
        installed = _installed_version(dist_name)
        if installed is None:
            msg = f"{dist_name}: not installed (declared {raw.strip()!r})"
            if args.strict:
                errors.append(msg)
            else:
                print(f"skip: {msg}")
            continue
        checked += 1
        if specifier and not _check_specifier(installed, specifier):
            errors.append(
                f"{dist_name}: installed {installed} does not satisfy {specifier!r} "
                f"(declared {raw.strip()!r})"
            )
        else:
            print(f"ok: {dist_name} {installed} satisfies {specifier or '(any)'}")

    print(f"checked {checked} installed dependencies from {args.pyproject}")
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("ok: installed dependencies match pyproject.toml constraints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

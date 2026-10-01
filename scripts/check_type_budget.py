"""Ratchet module, line and inline-error suppression debt for engine and SDK."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "docs/type-suppression-budget.json"


def module_sources(root: Path) -> dict[str, Path]:
    return {
        (".".join(path.relative_to(root).with_suffix("").parts)).removesuffix(".__init__"): path
        for path in (root / "coderai").rglob("*.py")
    }


def measure(root: Path, config: dict) -> dict:
    sources = module_sources(root)
    patterns = [
        pattern
        for override in config["tool"]["mypy"].get("overrides", [])
        if override.get("ignore_errors")
        for pattern in override["module"]
    ]
    suppressed = {
        module: path
        for module, path in sources.items()
        if any(
            fnmatch.fnmatchcase(module, pattern)
            or (pattern.endswith(".*") and module == pattern[:-2])
            for pattern in patterns
        )
    }
    files = list(sources.values()) + list((root / "sdks/coderai-sdk/src").rglob("*.py"))
    return {
        "modules": len(suppressed),
        "lines": sum(len(p.read_text(encoding="utf-8").splitlines()) for p in suppressed.values()),
        "total_modules": len(sources),
        "total_lines": sum(
            len(p.read_text(encoding="utf-8").splitlines()) for p in sources.values()
        ),
        "inline_ignores": sum(p.read_text(encoding="utf-8").count("type: ignore") for p in files),
        "remaining": sorted(suppressed),
        "patterns": patterns,
    }


def check_budget(current: dict, budget: dict) -> list[str]:
    problems = []
    if any("*" in pattern for pattern in current["patterns"]):
        problems.append("Wildcard ignore_errors exclusions are forbidden")
    if current["remaining"] != budget["remaining"]:
        problems.append("Remaining suppressed modules differ from the reviewed inventory")
    for key in ("modules", "lines", "inline_ignores"):
        if current[key] > budget[f"max_{key}"]:
            problems.append(f"{key}: {current[key]} exceeds {budget[f'max_{key}']}")
    return problems


def main() -> int:
    current = measure(ROOT, tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")))
    problems = check_budget(current, json.loads(BUDGET.read_text(encoding="utf-8")))
    print(json.dumps(current, indent=2))
    for problem in problems:
        print(f"error: {problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())

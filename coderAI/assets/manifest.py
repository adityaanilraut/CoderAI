"""Single source of truth for built-in packaged capabilities."""

from __future__ import annotations

import importlib.resources
from pathlib import Path


BUILTIN_PERSONAS = (
    "architect",
    "build-error-resolver",
    "code-reviewer",
    "planner",
    "security-reviewer",
    "tdd-guide",
)
BUILTIN_SKILLS = ("security-audit", "tdd-workflow")
BUILTIN_RULES = ("001-common-principles", "101-python-standards")
BUILTIN_STARTERS = ("CODERAI.md",)


def asset_root() -> Path:
    """Return the installed package-resource root.

    Wheels are installed as ordinary filesystem trees by pip, so exposing a
    concrete path keeps existing safe-path loaders reusable while removing the
    old dependency on a source checkout's top-level ``.coderAI`` directory.
    """
    return Path(str(importlib.resources.files("coderAI.assets"))).resolve()


def asset_directory(name: str) -> Path:
    path = (asset_root() / name).resolve()
    if not path.is_relative_to(asset_root()) or not path.is_dir():
        raise FileNotFoundError(f"Built-in asset directory is unavailable: {name}")
    return path


def asset_text(*parts: str) -> str:
    path = asset_root().joinpath(*parts).resolve()
    if not path.is_relative_to(asset_root()) or not path.is_file():
        raise FileNotFoundError(f"Built-in asset is unavailable: {'/'.join(parts)}")
    return path.read_text(encoding="utf-8")


def verify_builtin_assets() -> dict[str, list[str]]:
    """Fail if the installed distribution is missing an advertised asset."""
    agents = asset_directory("agents")
    skills = asset_directory("skills")
    rules = asset_directory("rules")
    starter = asset_directory("starter")
    missing: list[str] = []
    for name in BUILTIN_PERSONAS:
        if not (agents / f"{name}.md").is_file():
            missing.append(f"agents/{name}.md")
    for name in BUILTIN_SKILLS:
        if not (skills / name / "SKILLS.md").is_file():
            missing.append(f"skills/{name}/SKILLS.md")
    for name in BUILTIN_RULES:
        if not (rules / f"{name}.md").is_file():
            missing.append(f"rules/{name}.md")
    for name in BUILTIN_STARTERS:
        if not (starter / name).is_file():
            missing.append(f"starter/{name}")
    if missing:
        raise FileNotFoundError("Missing built-in package assets: " + ", ".join(missing))
    return {
        "personas": list(BUILTIN_PERSONAS),
        "skills": list(BUILTIN_SKILLS),
        "rules": list(BUILTIN_RULES),
    }

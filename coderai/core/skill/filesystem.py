"""Skill filesystem discovery — scan roots and exempt paths."""

from __future__ import annotations

import os
import pathlib


def get_extension_root() -> str:
    """Return the installed package root without importing prompt construction."""
    return str(pathlib.Path(__file__).resolve().parent.parent.parent)


def get_bundled_skills_root() -> str:
    return str(pathlib.Path(get_extension_root()) / "skills")


def get_skill_scan_roots(
    project_root: str | None = None, custom_scan_paths: list[str] | None = None
) -> list[tuple[str, str]]:
    """Return (filesystem_root, display_root) pairs. First match wins by skill name."""
    home = pathlib.Path.home()
    roots: list[tuple[str, str]] = []
    if project_root:
        root = pathlib.Path(project_root)
        roots.extend(
            [
                (str(root / ".coderai" / "skills"), "./.coderai/skills"),
                (str(root / ".agents" / "skills"), "./.agents/skills"),
                (str(root / ".claude" / "skills"), "./.claude/skills"),
            ]
        )
    roots.extend(
        [
            (str(home / ".coderai" / "skills"), "~/.coderai/skills"),
            (str(home / ".agents" / "skills"), "~/.agents/skills"),
            (str(home / ".claude" / "skills"), "~/.claude/skills"),
        ]
    )
    if custom_scan_paths:
        for custom_path in custom_scan_paths:
            if not custom_path:
                continue
            expanded = str(pathlib.Path(os.path.expanduser(custom_path)).resolve())
            display = custom_path if not project_root else f"custom:{custom_path}"
            if (expanded, display) not in roots and (expanded, custom_path) not in roots:
                roots.append((expanded, display))
    roots.append((get_bundled_skills_root(), "bundled:"))
    return roots


def get_skill_read_exempt_paths(
    project_root: str | None = None, custom_scan_paths: list[str] | None = None
) -> list[str]:
    return [
        root for root, _ in get_skill_scan_roots(project_root, custom_scan_paths=custom_scan_paths)
    ]


def _skill_markdown_path(skill_dir: pathlib.Path) -> pathlib.Path | None:
    candidate = skill_dir / "SKILL.md"
    return candidate if candidate.is_file() else None

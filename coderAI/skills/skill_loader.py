"""Skill file discovery and loading.

Scans project-local and external skill directories to build ``Skill``
instances from ``SKILLS.md`` files (or legacy flat ``.md`` files).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coderAI.system.project_layout import find_dot_coderai_subdir

logger = logging.getLogger(__name__)

SKILLS_FILE_NAME = "SKILLS.md"
LEGACY_SKILLS_DIR_NAME = "skills"
MAX_SKILL_FILE_BYTES = 100 * 1024


@dataclass
class Skill:
    """A discovered skill with parsed metadata and instructions."""

    name: str
    description: str = ""
    instructions: str = ""
    path: Optional[Path] = None
    version: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    source: str = "local"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Skill):
            return NotImplemented
        return self.name == other.name


def _find_skills_root(project_root: str = ".") -> Optional[Path]:
    """Locate the ``.coderAI/skills/`` directory."""
    return find_dot_coderai_subdir(LEGACY_SKILLS_DIR_NAME, project_root)


def _is_safe_path(file_path: Path, skills_root: Path) -> bool:
    """Block path-traversal attempts outside *skills_root*."""
    try:
        resolved = file_path.resolve()
        root_resolved = skills_root.resolve()
        return (
            str(resolved).startswith(str(root_resolved) + "/")
            or resolved == root_resolved
            or str(resolved).startswith(str(root_resolved))
        )
    except Exception:
        return False


def _parse_frontmatter(content: str) -> tuple[Dict[str, Any], str]:
    """Extract YAML frontmatter and body from a skill file.

    Returns ``(metadata_dict, body_text)``.
    """
    metadata: Dict[str, Any] = {}
    instructions = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
                instructions = parts[2].strip()
            except yaml.YAMLError as e:
                logger.warning("Failed to parse YAML frontmatter: %s", e)

    return metadata, instructions


def load_skill_from_path(file_path: Path, source: str = "local") -> Optional[Skill]:
    """Read a single ``SKILLS.md`` (or legacy ``.md``) file into a ``Skill``.

    Args:
        file_path: Absolute path to the skill file.
        source: Label for the skill origin (``"local"``, ``"hasna"``, etc.).

    Returns:
        ``Skill`` instance or ``None`` on failure.
    """
    if not file_path.exists():
        return None

    if file_path.stat().st_size > MAX_SKILL_FILE_BYTES:
        logger.warning("Skill file too large: %s", file_path)
        return None

    try:
        content = file_path.read_text(encoding="utf-8")
        metadata, instructions = _parse_frontmatter(content)

        # Derive name from frontmatter → parent directory (SKILLS.md) → file stem (legacy)
        if "name" in metadata:
            skill_name = str(metadata["name"])
        elif file_path.name == SKILLS_FILE_NAME:
            skill_name = file_path.parent.name
        else:
            skill_name = file_path.stem
        return Skill(
            name=skill_name,
            description=metadata.get("description", f"Skill: {skill_name}"),
            instructions=instructions,
            path=file_path,
            version=metadata.get("version"),
            dependencies=metadata.get("dependencies") or [],
            category=metadata.get("category"),
            tags=metadata.get("tags") or [],
            source=source,
        )
    except Exception as e:
        logger.error("Error loading skill from %s: %s", file_path, e)
        return None


def discover_local_skills(project_root: str = ".") -> List[Skill]:
    """Scan ``.coderAI/skills/`` for both subdirectory and legacy formats.

    - New (preferred):  ``skills/<name>/SKILLS.md``
    - Legacy (fallback): ``skills/<name>.md``
    """
    skills_root = _find_skills_root(project_root)
    if skills_root is None:
        return []

    skills: List[Skill] = []
    seen_names: set[str] = set()

    # --- New subdirectory format: skills/<name>/SKILLS.md ---
    for item in sorted(skills_root.iterdir()):
        if item.is_dir():
            skills_file = item / SKILLS_FILE_NAME
            if skills_file.is_file():
                skill = load_skill_from_path(skills_file, source="local")
                if skill and skill.name not in seen_names:
                    skills.append(skill)
                    seen_names.add(skill.name)

    # --- Legacy flat format: skills/<name>.md (only for files not already
    #     covered by the subdirectory format) ---
    for md_file in sorted(skills_root.glob("*.md")):
        if md_file.stem in seen_names:
            continue
        skill = load_skill_from_path(md_file, source="local")
        if skill and skill.name not in seen_names:
            skills.append(skill)
            seen_names.add(skill.name)

    return skills


def load_skill_by_name(skill_name: str, project_root: str = ".") -> Optional[Skill]:
    """Load a single skill by name from the project-local skills directory.

    Prefer the subdirectory format (``skills/<name>/SKILLS.md``) but fall
    back to the legacy flat file (``skills/<name>.md``).
    """
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        logger.warning("Rejected skill_name with path traversal: %s", skill_name)
        return None

    skills_root = _find_skills_root(project_root)
    if skills_root is None:
        return None

    # Prefer subdirectory format
    subdir_file = (skills_root / skill_name / SKILLS_FILE_NAME).resolve()
    if subdir_file.is_file() and _is_safe_path(subdir_file, skills_root):
        return load_skill_from_path(subdir_file, source="local")

    # Fallback legacy flat file
    legacy_file = (skills_root / f"{skill_name}.md").resolve()
    if legacy_file.is_file() and _is_safe_path(legacy_file, skills_root):
        return load_skill_from_path(legacy_file, source="local")

    return None

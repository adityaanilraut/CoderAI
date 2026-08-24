""""""

from __future__ import annotations

import pathlib
import re
from typing import Any

from coderai.core.skill.filesystem import (
    get_skill_scan_roots,
    _skill_markdown_path,
)
from coderai.core.skill.loader import (
    extract_skill_frontmatter,
)

STOP_WORDS = {
    "this",
    "that",
    "with",
    "from",
    "make",
    "change",
    "have",
    "file",
    "please",
    "code",
    "user",
    "what",
    "when",
    "where",
    "which",
    "your",
    "about",
    "their",
    "there",
    "would",
    "could",
    "should",
    "follow",
    "using",
    "into",
    "some",
    "only",
    "then",
    "also",
    "more",
    "most",
    "than",
    "other",
    "such",
    "just",
    "like",
    "will",
}


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")


def _implicit_invocation_allowed(meta: dict[str, Any]) -> bool:
    raw = (
        meta.get("allow-implicit-invocation")
        if meta.get("allow-implicit-invocation") is not None
        else meta.get("allow_implicit_invocation")
    )
    if raw is None:
        metadata = meta.get("metadata")
        if isinstance(metadata, dict):
            raw = metadata.get("allow-implicit-invocation") or metadata.get(
                "allow_implicit_invocation"
            )
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("false", "0", "no")


class SkillRegistry:
    """Layered skill registry with filesystem discovery and dynamic keyword matching."""

    def __init__(
        self,
        project_root: str | None = None,
        custom_scan_paths: list[str] | None = None,
    ) -> None:
        self.project_root = project_root
        self.custom_scan_paths = custom_scan_paths or []

    def list_skills(
        self,
        enabled_skills: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        """List bundled + project + user + external compatibility skills. First-wins by name."""
        enabled = enabled_skills or {}
        skills_by_name: dict[str, dict[str, Any]] = {}
        for root, display_root in get_skill_scan_roots(
            self.project_root, custom_scan_paths=self.custom_scan_paths
        ):
            path = pathlib.Path(root)
            if not path.is_dir():
                continue
            try:
                entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for skill_dir in entries:
                if not skill_dir.is_dir():
                    continue
                skill_file = _skill_markdown_path(skill_dir)
                if skill_file is None:
                    continue
                try:
                    raw_content = skill_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta = extract_skill_frontmatter(raw_content)
                name = (meta.get("name") or "").strip() or skill_dir.name.replace("_", "-")
                if name in skills_by_name:
                    continue
                if enabled.get(name) is False:
                    continue
                location = (
                    f"bundled:{skill_dir.name}/{skill_file.name}"
                    if display_root == "bundled:"
                    else f"{display_root}/{skill_dir.name}/{skill_file.name}"
                )
                skills_by_name[name] = {
                    "name": name,
                    "path": str(skill_file),
                    "location": location,
                    "description": meta.get("description", ""),
                    "allowImplicitInvocation": _implicit_invocation_allowed(meta),
                }
        return sorted(skills_by_name.values(), key=lambda s: str(s["name"]))

    def load_skill(self, name: str) -> dict[str, Any] | None:
        needle = name.strip().lower()
        for skill in self.list_skills():
            if skill["name"].lower() == needle:
                try:
                    content = _read(skill["path"])
                    return {
                        "name": skill["name"],
                        "content": content,
                        "instructions": content,
                        "path": skill["path"],
                        "skillFilePath": skill["path"],
                        "location": skill.get("location", ""),
                        "description": skill.get("description", ""),
                        "allowImplicitInvocation": skill.get("allowImplicitInvocation", True),
                    }
                except OSError:
                    return None
        return None

    def match_skills(
        self,
        user_prompt: str,
        enabled_skills: dict[str, bool] | None = None,
        loaded_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Match skills automatically based on user prompt query terms and skill descriptions."""
        if not user_prompt.strip():
            return []
        loaded = {n.lower() for n in (loaded_names or set())}
        prompt_lower = user_prompt.lower()
        prompt_tokens = set(re.findall(r"\w+", prompt_lower))

        matched: list[dict[str, Any]] = []
        for skill in self.list_skills(enabled_skills=enabled_skills):
            name_lower = skill["name"].lower()
            if name_lower in loaded:
                continue
            if skill.get("allowImplicitInvocation") is False:
                continue

            # Exact skill name mentioned in prompt or /skill command
            if name_lower in prompt_lower or f"/{name_lower}" in prompt_lower:
                matched.append(skill)
                continue

            name_tokens = {
                t for t in re.findall(r"\w+", name_lower) if t not in STOP_WORDS and len(t) >= 3
            }
            if name_tokens and name_tokens.issubset(prompt_tokens):
                matched.append(skill)
                continue
        return matched


# Global helpers for module-level compatibility
def list_skills(
    project_root: str | None = None,
    enabled_skills: dict[str, bool] | None = None,
    custom_scan_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    registry = SkillRegistry(project_root=project_root, custom_scan_paths=custom_scan_paths)
    return registry.list_skills(enabled_skills=enabled_skills)


def load_skill(
    name: str,
    project_root: str | None = None,
    custom_scan_paths: list[str] | None = None,
) -> dict[str, Any] | None:
    registry = SkillRegistry(project_root=project_root, custom_scan_paths=custom_scan_paths)
    return registry.load_skill(name)


def match_skills_for_prompt(
    user_prompt: str,
    project_root: str | None = None,
    enabled_skills: dict[str, bool] | None = None,
    loaded_names: set[str] | None = None,
    custom_scan_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    registry = SkillRegistry(project_root=project_root, custom_scan_paths=custom_scan_paths)
    return registry.match_skills(
        user_prompt, enabled_skills=enabled_skills, loaded_names=loaded_names
    )

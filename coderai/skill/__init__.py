"""Skill filesystem discovery — scan roots and exempt paths."""

from __future__ import annotations

import os
import pathlib


def get_extension_root() -> str:
    """Return the installed package root without importing prompt construction."""
    # ponytail: one fewer parent than the original (lived at core/skill/filesystem.py).
    return str(pathlib.Path(__file__).resolve().parent.parent)


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
# --- from coderai/core/skill/loader.py ---
""""""


import pathlib
import re
from typing import Any

DEFAULT_SKILL_RESOURCE_FILE_LIMIT = 50
SKILL_RESOURCE_EXCLUDED_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def strip_skill_prompt_metadata(content: str) -> str:
    """Strip YAML frontmatter metadata from SKILL.md content."""
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(content)
    if match:
        return content[match.end() :].lstrip()
    return content


def extract_skill_frontmatter(content: str) -> dict[str, Any]:
    """Extract metadata (name, description, etc.) from SKILL.md YAML frontmatter."""
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(content)
    if not match:
        return {}
    yaml_text = match.group(1)
    try:
        import yaml

        parsed = yaml.safe_load(yaml_text)
        if isinstance(parsed, dict):
            meta: dict[str, Any] = {}
            for k, v in parsed.items():
                key = str(k).strip().lower()
                if isinstance(v, str):
                    meta[key] = v.strip()
                elif isinstance(v, (bool, int, float, dict, list)):
                    meta[key] = v
                else:
                    meta[key] = str(v)
            return meta
    except Exception:
        pass

    # Fallback to simple line-based parsing
    meta = {}
    for line in yaml_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip().lower()] = val.strip().strip("'\"")
    return meta


def list_skill_resource_files(
    skill_file_path: str, limit: int = DEFAULT_SKILL_RESOURCE_FILE_LIMIT
) -> tuple[list[str], bool]:
    """Discover helper and resource files located in the skill directory."""
    skill_dir = pathlib.Path(skill_file_path).parent
    if not skill_dir.is_dir():
        return [], False

    files: list[str] = []
    truncated = False

    for item in sorted(skill_dir.rglob("*")):
        if item.is_dir():
            continue
        parts = item.relative_to(skill_dir).parts
        if any(p in SKILL_RESOURCE_EXCLUDED_DIRS or p.startswith(".") for p in parts):
            continue
        rel = "/".join(parts)
        if rel == "SKILL.md":
            continue
        if len(files) >= limit:
            truncated = True
            break
        files.append(rel)

    return files[:limit], truncated


def render_skill_resources(skill_file_path: str | None) -> str:
    if not skill_file_path:
        return ""
    files, truncated = list_skill_resource_files(skill_file_path, DEFAULT_SKILL_RESOURCE_FILE_LIMIT)
    if not files and not truncated:
        return ""
    lines = [f"  <file>{_escape(f)}</file>" for f in files]
    if truncated:
        lines.append(
            f"  <note>Listing capped at {DEFAULT_SKILL_RESOURCE_FILE_LIMIT} files and may be incomplete.</note>"
        )
    return "\n\n<skill_resources>\n" + "\n".join(lines) + "\n</skill_resources>"


def render_skill_document_block(skill: dict[str, Any]) -> str:
    name = skill.get("name", "skill")
    attrs: list[str] = []
    if skill.get("path"):
        attrs.append(f'path="{_escape(skill.get("path", ""))}"')
    if skill.get("version"):
        attrs.append(f'version="{_escape(skill.get("version", ""))}"')
    if skill.get("deprecated"):
        dep_str = "true" if skill.get("deprecated") is True else str(skill.get("deprecated"))
        attrs.append(f'deprecated="{_escape(dep_str)}"')
    path_attr = (" " + " ".join(attrs)) if attrs else ""
    content = strip_skill_prompt_metadata(skill.get("content", ""))
    skill_file_path = skill.get("skillFilePath") or skill.get("path")
    resources = render_skill_resources(skill_file_path)
    dep_warning = ""
    if skill.get("deprecated"):
        reason = (
            f": {skill.get('deprecated')}"
            if isinstance(skill.get("deprecated"), str) and skill.get("deprecated") != "true"
            else ""
        )
        dep_warning = f"> [!WARNING]\n> This skill is deprecated{reason}.\n\n"
    return f"<{name}-skill{path_attr}>\n{dep_warning}{content}{resources}\n</{name}-skill>"


def build_skill_documents_prompt(skills: list[dict[str, Any]]) -> str:
    blocks = [render_skill_document_block(skill) for skill in skills]
    if not blocks:
        return ""
    return "Use the skill documents below to assist the user:\n" + "\n\n".join(blocks)
# --- from coderai/core/skill/registry.py ---
""""""


import pathlib
import re
from typing import Any

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
        merge_all_available_skills: bool = True,
    ) -> None:
        self.project_root = project_root
        self.custom_scan_paths = custom_scan_paths or []
        self.merge_all_available_skills = merge_all_available_skills

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
                    # flow skills (frontmatter ``type: flow``) run
                    # via /flow:<name>, never auto-injected (see match_skills).
                    "type": str(meta.get("type", "standard") or "standard").strip().lower(),
                    "allowImplicitInvocation": _implicit_invocation_allowed(meta),
                    "version": str(meta.get("version", "")).strip() or None,
                    "minRuntimeVersion": str(
                        meta.get("min_runtime_version") or meta.get("minruntimeversion", "")
                    ).strip()
                    or None,
                    "deprecated": meta.get("deprecated"),
                }
        skills = sorted(skills_by_name.values(), key=lambda s: str(s["name"]))
        if not self.merge_all_available_skills:
            # with merging off, only explicitly enabled skills show.
            skills = [s for s in skills if enabled.get(s["name"]) is True]
        return skills

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
                        "type": skill.get("type", "standard"),
                        "allowImplicitInvocation": skill.get("allowImplicitInvocation", True),
                        "version": skill.get("version"),
                        "minRuntimeVersion": skill.get("minRuntimeVersion"),
                        "deprecated": skill.get("deprecated"),
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
            # Flow skills run explicitly via /flow:<name>; never auto-inject
            # diagram source into the prompt (parity).
            if str(skill.get("type", "standard")).lower() == "flow":
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
    merge_all_available_skills: bool = True,
) -> list[dict[str, Any]]:
    registry = SkillRegistry(
        project_root=project_root,
        custom_scan_paths=custom_scan_paths,
        merge_all_available_skills=merge_all_available_skills,
    )
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
    merge_all_available_skills: bool = True,
) -> list[dict[str, Any]]:
    registry = SkillRegistry(
        project_root=project_root,
        custom_scan_paths=custom_scan_paths,
        merge_all_available_skills=merge_all_available_skills,
    )
    return registry.match_skills(
        user_prompt, enabled_skills=enabled_skills, loaded_names=loaded_names
    )

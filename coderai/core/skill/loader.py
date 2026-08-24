""""""

from __future__ import annotations

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
    path_attr = f' path="{_escape(skill.get("path", ""))}"' if skill.get("path") else ""
    content = strip_skill_prompt_metadata(skill.get("content", ""))
    skill_file_path = skill.get("skillFilePath") or skill.get("path")
    resources = render_skill_resources(skill_file_path)
    return f"<{name}-skill{path_attr}>\n{content}{resources}\n</{name}-skill>"


def build_skill_documents_prompt(skills: list[dict[str, Any]]) -> str:
    blocks = [render_skill_document_block(skill) for skill in skills]
    if not blocks:
        return ""
    return "Use the skill documents below to assist the user:\n" + "\n\n".join(blocks)

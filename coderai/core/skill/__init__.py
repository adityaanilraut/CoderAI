"""Skill Subsystem for CoderAI — port of dsh layered skill architecture."""

from coderai.core.skill.filesystem import (
    get_bundled_skills_root,
    get_extension_root,
    get_skill_read_exempt_paths,
    get_skill_scan_roots,
)
from coderai.core.skill.loader import (
    DEFAULT_SKILL_RESOURCE_FILE_LIMIT,
    SKILL_RESOURCE_EXCLUDED_DIRS,
    build_skill_documents_prompt,
    extract_skill_frontmatter,
    list_skill_resource_files,
    render_skill_document_block,
    render_skill_resources,
    strip_skill_prompt_metadata,
)
from coderai.core.skill.registry import (
    SkillRegistry,
    _implicit_invocation_allowed,
    list_skills,
    load_skill,
    match_skills_for_prompt,
    parse_skill_match_response,
)

__all__ = [
    "DEFAULT_SKILL_RESOURCE_FILE_LIMIT",
    "SKILL_RESOURCE_EXCLUDED_DIRS",
    "SkillRegistry",
    "_implicit_invocation_allowed",
    "build_skill_documents_prompt",
    "extract_skill_frontmatter",
    "get_bundled_skills_root",
    "get_extension_root",
    "get_skill_read_exempt_paths",
    "get_skill_scan_roots",
    "list_skill_resource_files",
    "list_skills",
    "load_skill",
    "match_skills_for_prompt",
    "parse_skill_match_response",
    "render_skill_document_block",
    "render_skill_resources",
    "strip_skill_prompt_metadata",
]

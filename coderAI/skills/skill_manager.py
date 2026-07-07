"""Skill Manager — LLM-based relevance matching with multi-source orchestration.

Contains the Skill dataclass, SkillRegistry cache, skill file discovery/loading,
and the SkillManager orchestrator.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from coderAI.system.project_layout import find_dot_coderai_subdir

logger = logging.getLogger(__name__)

SKILL_MGR_PREFIX = "[SkillManager]"
SKILLS_FILE_NAME = "SKILLS.md"
LEGACY_SKILLS_DIR_NAME = "skills"
MAX_SKILL_FILE_BYTES = 100 * 1024

# ------------------------------------------------------------------
# Skill dataclass
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# SkillRegistry
# ------------------------------------------------------------------


class SkillRegistry:
    """Session-scoped container that indexes skills by name."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            logger.debug("[SkillRegistry] Overwriting existing skill: %s", skill.name)
        else:
            logger.debug("[SkillRegistry] Registered skill: %s", skill.name)
        self._skills[skill.name] = skill

    def register_all(self, skills: Iterable[Skill]) -> None:
        for skill in skills:
            self.register(skill)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        return list(self._skills.values())

    def find_by_source(self, source: str) -> List[Skill]:
        return [s for s in self._skills.values() if s.source == source]

    def clear(self) -> None:
        self._skills.clear()
        logger.debug("[SkillRegistry] Registry cleared")

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills


# ------------------------------------------------------------------
# Skill file discovery and loading (formerly skill_loader.py)
# ------------------------------------------------------------------


def _find_skills_root(project_root: str = ".") -> Optional[Path]:
    return find_dot_coderai_subdir(LEGACY_SKILLS_DIR_NAME, project_root)


def _is_safe_path(file_path: Path, skills_root: Path) -> bool:
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
    if not file_path.exists():
        return None
    if file_path.stat().st_size > MAX_SKILL_FILE_BYTES:
        logger.warning("Skill file too large: %s", file_path)
        return None
    try:
        content = file_path.read_text(encoding="utf-8")
        metadata, instructions = _parse_frontmatter(content)
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
    skills_root = _find_skills_root(project_root)
    if skills_root is None:
        return []
    skills: List[Skill] = []
    seen_names: set[str] = set()
    for item in sorted(skills_root.iterdir()):
        if item.is_dir():
            skills_file = item / SKILLS_FILE_NAME
            if skills_file.is_file():
                skill = load_skill_from_path(skills_file, source="local")
                if skill and skill.name not in seen_names:
                    skills.append(skill)
                    seen_names.add(skill.name)
    for md_file in sorted(skills_root.glob("*.md")):
        if md_file.stem in seen_names:
            continue
        skill = load_skill_from_path(md_file, source="local")
        if skill and skill.name not in seen_names:
            skills.append(skill)
            seen_names.add(skill.name)
    return skills


def load_skill_by_name(skill_name: str, project_root: str = ".") -> Optional[Skill]:
    if ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        logger.warning("Rejected skill_name with path traversal: %s", skill_name)
        return None
    skills_root = _find_skills_root(project_root)
    if skills_root is None:
        return None
    subdir_file = (skills_root / skill_name / SKILLS_FILE_NAME).resolve()
    if subdir_file.is_file() and _is_safe_path(subdir_file, skills_root):
        return load_skill_from_path(subdir_file, source="local")
    legacy_file = (skills_root / f"{skill_name}.md").resolve()
    if legacy_file.is_file() and _is_safe_path(legacy_file, skills_root):
        return load_skill_from_path(legacy_file, source="local")
    return None


# ------------------------------------------------------------------
# SkillManager
# ------------------------------------------------------------------


# Default prompt template used to ask the LLM to score skill relevance.
_SKILL_MATCHING_SYSTEM_PROMPT = """\
You are a skill-matching classifier. Given a task description and a list of \
available skills with descriptions, determine which skills are relevant to \
the task. Return ONLY a JSON object. Do not include any other text.

For each skill, assign a confidence score between 0.0 and 1.0 where:
- 0.9-1.0: The skill directly addresses the core task
- 0.7-0.89: The skill is highly related to the task
- 0.5-0.69: The skill could be useful as supplementary guidance
- 0.0-0.49: The skill is not relevant (omit from response)

Only include skills with confidence >= 0.5.

Return format:
{"matches": [{"skill_name": "...", "confidence": 0.XX, "reasoning": "..."}]}"""


class SkillManager:
    """Orchestrates skill discovery and LLM-based relevance matching."""

    def __init__(
        self,
        sources: Sequence[Any],
        threshold: float = 0.7,
        top_n: int = 3,
        provider: Any = None,
    ) -> None:
        self._sources = list(sources)
        self.threshold = threshold
        self.top_n = top_n
        self._provider = provider
        self.registry = SkillRegistry()
        self._discovery_complete = False
        self._discovery_lock = asyncio.Lock()
        self._match_lock = asyncio.Lock()
        self._match_cache: Dict[str, List[Tuple[Skill, float]]] = {}

    @property
    def provider(self) -> Any:
        return self._provider

    @provider.setter
    def provider(self, value: Any) -> None:
        self._provider = value

    async def _ensure_discovered(self) -> None:
        if self._discovery_complete:
            return
        async with self._discovery_lock:
            if self._discovery_complete:
                return
            logger.info(
                "%s Discovering skills from %d source(s)...", SKILL_MGR_PREFIX, len(self._sources)
            )
            for source in self._sources:
                try:
                    skills = await source.discover()
                    self.registry.register_all(skills)
                    logger.info(
                        "%s Source '%s': discovered %d skill(s)",
                        SKILL_MGR_PREFIX,
                        source.source_name,
                        len(skills),
                    )
                except Exception as e:
                    logger.warning(
                        "%s Source '%s' failed: %s", SKILL_MGR_PREFIX, source.source_name, e
                    )
            self._discovery_complete = True
            logger.info(
                "%s Discovery complete — %d total skill(s) in registry",
                SKILL_MGR_PREFIX,
                len(self.registry),
            )

    async def _match_via_llm(
        self, task_description: str, skills: List[Skill]
    ) -> List[Tuple[Skill, float]]:
        if not self._provider or not skills:
            return []
        skill_index: Dict[str, Skill] = {s.name: s for s in skills}
        skill_lines: List[str] = []
        for s in skills:
            source_tag = f" [{s.source}]" if s.source != "local" else ""
            skill_lines.append(f"- {s.name}{source_tag}: {s.description}")
        user_message = (
            f"Task: {task_description}\n\n"
            f"Available skills ({len(skills)}):\n" + "\n".join(skill_lines)
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _SKILL_MATCHING_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        try:
            response = await self._provider.chat(
                messages,
                tools=None,
                max_tokens=1024,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning("%s LLM matching call failed: %s", SKILL_MGR_PREFIX, e)
            return []
        content = self._extract_response_content(response)
        if not content:
            return []
        matches = self._parse_matches_json(content, skill_index)
        return matches

    def _extract_response_content(self, response: Any) -> Optional[str]:
        try:
            if isinstance(response, dict):
                choices = response.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    return msg.get("content") or ""
            return ""
        except Exception as e:
            logger.debug("%s Failed to extract response content: %s", SKILL_MGR_PREFIX, e)
            return ""

    def _parse_matches_json(
        self, content: str, skill_index: Dict[str, Skill]
    ) -> List[Tuple[Skill, float]]:
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError as e:
            logger.warning("%s Failed to parse LLM response as JSON: %s", SKILL_MGR_PREFIX, e)
            return []
        if not isinstance(data, dict) or "matches" not in data:
            return []
        raw_matches = data["matches"]
        if not isinstance(raw_matches, list):
            return []
        results: List[Tuple[Skill, float]] = []
        for item in raw_matches:
            if not isinstance(item, dict):
                continue
            skill_name = str(item.get("skill_name", "")).strip()
            try:
                confidence = float(item.get("confidence", 0))
            except (ValueError, TypeError):
                confidence = 0.0
            if confidence < self.threshold:
                continue
            skill = skill_index.get(skill_name)
            if skill is None:
                logger.debug("%s LLM returned unknown skill: %s", SKILL_MGR_PREFIX, skill_name)
                continue
            reasoning = str(item.get("reasoning", ""))
            logger.info(
                "%s Matched: %s (%.2f) — %s", SKILL_MGR_PREFIX, skill_name, confidence, reasoning
            )
            results.append((skill, confidence))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[: self.top_n]

    def _keyword_score(
        self, task_description: str, skills: List[Skill]
    ) -> List[Tuple[Skill, float]]:
        query_lower = task_description.lower()
        query_words = set(query_lower.split())
        scored: List[Tuple[Skill, float]] = []
        for skill in skills:
            searchable = f"{skill.name} {skill.description} {' '.join(skill.tags)} {skill.category or ''}".lower()
            text_words = set(searchable.split())
            overlap = query_words & text_words
            if not overlap:
                continue
            score = len(overlap) / max(len(query_words), 1)
            score = min(score * 1.2, 0.7)
            if score >= self.threshold:
                scored.append((skill, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: self.top_n]

    async def get_top_skills(
        self,
        task_description: str,
        top_n: Optional[int] = None,
        threshold: Optional[float] = None,
    ) -> List[Skill]:
        await self._ensure_discovered()
        effective_top_n = top_n if top_n is not None else self.top_n
        effective_threshold = threshold if threshold is not None else self.threshold
        cache_key = f"{task_description}:{effective_top_n}:{effective_threshold}"
        if cache_key in self._match_cache:
            logger.debug("%s Cache hit for task: %s...", SKILL_MGR_PREFIX, task_description[:60])
            return [s for s, _ in self._match_cache[cache_key]]
        logger.info("%s Searching skills for: %s...", SKILL_MGR_PREFIX, task_description[:80])
        async with self._match_lock:
            original_threshold = self.threshold
            self.threshold = effective_threshold
            original_top_n = self.top_n
            self.top_n = effective_top_n
            try:
                local_skills = self.registry.find_by_source("local")
                llm_matches: List[Tuple[Skill, float]] = []
                if local_skills:
                    logger.debug(
                        "%s Evaluating %d local skill(s) via LLM...",
                        SKILL_MGR_PREFIX,
                        len(local_skills),
                    )
                    llm_matches = await self._match_via_llm(task_description, local_skills)
                source_matches: List[Tuple[Skill, float]] = []
                for source in self._sources:
                    if source.source_name == "local":
                        continue
                    try:
                        results = await source.search(task_description, top_n=effective_top_n)
                        source_matches.extend(results)
                        for skill, conf in results:
                            logger.info(
                                "%s Matched (hasna): %s (%.2f)",
                                SKILL_MGR_PREFIX,
                                skill.name,
                                conf,
                            )
                    except Exception as e:
                        logger.warning(
                            "%s Source '%s' search failed: %s",
                            SKILL_MGR_PREFIX,
                            source.source_name,
                            e,
                        )
                if not llm_matches and local_skills:
                    logger.debug(
                        "%s LLM matching returned no results — trying keyword fallback",
                        SKILL_MGR_PREFIX,
                    )
                    llm_matches = self._keyword_score(task_description, local_skills)
                merged: Dict[str, Tuple[Skill, float]] = {}
                for skill, conf in llm_matches + source_matches:
                    existing = merged.get(skill.name)
                    if existing is None or conf > existing[1]:
                        merged[skill.name] = (skill, conf)
                final = sorted(merged.values(), key=lambda x: x[1], reverse=True)[:effective_top_n]
                self._match_cache[cache_key] = final
                if final:
                    skill_names = [f"{s.name} ({c:.2f})" for s, c in final]
                    logger.info("%s Skills selected: %s", SKILL_MGR_PREFIX, ", ".join(skill_names))
                else:
                    logger.debug("%s No relevant skills found", SKILL_MGR_PREFIX)
                return [s for s, _ in final]
            finally:
                self.threshold = original_threshold
                self.top_n = original_top_n

    async def get_relevant_skill(self, task_description: str) -> Optional[Skill]:
        skills = await self.get_top_skills(task_description, top_n=1)
        return skills[0] if skills else None

    async def preload_skills(self, skill_names: List[str]) -> List[Skill]:
        await self._ensure_discovered()
        loaded: List[Skill] = []
        for name in skill_names:
            existing = self.registry.get(name)
            if existing is not None:
                if existing not in loaded:
                    loaded.append(existing)
                continue
            for source in self._sources:
                try:
                    skill = await source.get_skill(name)
                    if skill is not None:
                        self.registry.register(skill)
                        if skill not in loaded:
                            loaded.append(skill)
                        logger.info("%s Preloaded: %s", SKILL_MGR_PREFIX, skill.name)
                        break
                except Exception as e:
                    logger.warning("%s Preload failed for '%s': %s", SKILL_MGR_PREFIX, name, e)
        return loaded

    def clear_cache(self) -> None:
        self._match_cache.clear()
        logger.debug("%s Match cache cleared", SKILL_MGR_PREFIX)

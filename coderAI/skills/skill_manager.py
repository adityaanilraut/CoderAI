"""Skill Manager — LLM-based relevance matching with multi-source orchestration.

Discovers skills from local files and @hasna/skills, ranks them by relevance
to the user's task, and returns the best matches.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from coderAI.skills.skill_loader import Skill
from coderAI.skills.skill_registry import SkillRegistry
from coderAI.skills.sources.base import SkillSource

logger = logging.getLogger(__name__)

SKILL_MGR_PREFIX = "[SkillManager]"

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
    """Orchestrates skill discovery and LLM-based relevance matching.

    Delegates to one or more :class:`SkillSource` backends to discover
    available skills, then uses an LLM provider to score each skill's
    relevance to a given task description.

    Attributes:
        threshold: Minimum confidence (0-1) to consider a skill relevant.
        top_n: Maximum number of skills to return.
        registry: Session-level skill cache.
    """

    def __init__(
        self,
        sources: Sequence[SkillSource],
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
        """LLM provider used for relevance scoring."""
        return self._provider

    @provider.setter
    def provider(self, value: Any) -> None:
        self._provider = value

    async def _ensure_discovered(self) -> None:
        """Lazily discover skills from all sources and populate the registry."""
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
        """Ask the LLM to score skill relevance to *task_description*.

        Returns skills sorted by descending confidence, filtered to those
        at or above ``self.threshold``.
        """
        if not self._provider or not skills:
            return []

        skill_index: Dict[str, Skill] = {s.name: s for s in skills}

        # Build a compact skill listing for the prompt
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
        """Safely extract the text content from an LLM response."""
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
        """Parse the JSON response from the LLM matching call."""
        # Strip markdown code fences and leading/trailing whitespace
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
        """Fast keyword-overlap baseline (no LLM call).

        Used as a fallback when no LLM provider is available.
        """
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
            score = min(score * 1.2, 0.7)  # boost slightly but cap
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
        """Find the most relevant skills for a given task.

        Workflow:
        1. Ensure all skill sources have been discovered (cached).
        2. Check the in-memory match cache (keyed by task description).
        3. If an LLM provider is available, use it to score local skills.
        4. Also query external sources (e.g., @hasna/skills).
        5. Merge, deduplicate, and return the top-N skills.

        Args:
            task_description: The user's request or task description.
            top_n: Override the configured maximum skills to return.
            threshold: Override the confidence threshold.

        Returns:
            List of matching :class:`Skill` objects sorted by relevance.
        """
        await self._ensure_discovered()

        effective_top_n = top_n if top_n is not None else self.top_n
        effective_threshold = threshold if threshold is not None else self.threshold

        cache_key = f"{task_description}:{effective_top_n}:{effective_threshold}"
        if cache_key in self._match_cache:
            logger.debug("%s Cache hit for task: %s...", SKILL_MGR_PREFIX, task_description[:60])
            return [s for s, _ in self._match_cache[cache_key]]

        logger.info("%s Searching skills for: %s...", SKILL_MGR_PREFIX, task_description[:80])

        # Temporarily adjust threshold for the matching call
        async with self._match_lock:
            original_threshold = self.threshold
            self.threshold = effective_threshold
            original_top_n = self.top_n
            self.top_n = effective_top_n

            try:
                local_skills = self.registry.find_by_source("local")

                # 1) LLM-based matching for local skills
                llm_matches: List[Tuple[Skill, float]] = []
                if local_skills:
                    logger.debug(
                        "%s Evaluating %d local skill(s) via LLM...",
                        SKILL_MGR_PREFIX,
                        len(local_skills),
                    )
                    llm_matches = await self._match_via_llm(task_description, local_skills)

                # 2) Source-based search for external skills (hasna)
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

                # 3) If LLM returned nothing but we have local skills, fall back to keyword
                if not llm_matches and local_skills:
                    logger.debug(
                        "%s LLM matching returned no results — trying keyword fallback",
                        SKILL_MGR_PREFIX,
                    )
                    llm_matches = self._keyword_score(task_description, local_skills)

                # 4) Merge, deduplicate by name, sort by confidence
                merged: Dict[str, Tuple[Skill, float]] = {}
                for skill, conf in llm_matches + source_matches:
                    existing = merged.get(skill.name)
                    if existing is None or conf > existing[1]:
                        merged[skill.name] = (skill, conf)

                final = sorted(merged.values(), key=lambda x: x[1], reverse=True)[:effective_top_n]

                # Cache the result
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
        """Return the single best-match skill, or ``None``."""
        skills = await self.get_top_skills(task_description, top_n=1)
        return skills[0] if skills else None

    async def preload_skills(self, skill_names: List[str]) -> List[Skill]:
        """Eagerly load specific skills by name from all sources.

        Useful for persisting manually selected skills across the session.
        """
        await self._ensure_discovered()
        loaded: List[Skill] = []

        for name in skill_names:
            # Check registry first
            existing = self.registry.get(name)
            if existing is not None:
                if existing not in loaded:
                    loaded.append(existing)
                continue

            # Try each source
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
        """Clear the match-result cache (not the skill registry)."""
        self._match_cache.clear()
        logger.debug("%s Match cache cleared", SKILL_MGR_PREFIX)

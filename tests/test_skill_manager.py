"""Tests for skill_manager.py — LLM matching, ranking, multi-source orchestration."""

import json
from unittest.mock import AsyncMock

import pytest

from coderAI.skills.skill_manager import Skill, SkillManager
from coderAI.skills.sources.base import SkillSource


class FakeSkillSource(SkillSource):
    """A test double that returns a fixed set of skills."""

    def __init__(self, name: str = "local", skills=None):
        self._name = name
        self._skills = skills or []

    @property
    def source_name(self) -> str:
        return self._name

    async def discover(self):
        for s in self._skills:
            s.source = self._name
        return self._skills

    async def search(self, query, top_n=5):
        results = [(s, 1.0) for s in self._skills[:top_n]]
        return results

    async def get_skill(self, name):
        for s in self._skills:
            if s.name == name:
                return s
        return None


def make_mock_provider(response_text):
    """Build an AsyncMock LLM provider that returns *response_text*."""
    provider = AsyncMock()
    provider.chat = AsyncMock(return_value={"choices": [{"message": {"content": response_text}}]})
    return provider


class TestSkillManagerInitialization:
    def test_initial_state(self):
        manager = SkillManager(sources=[])
        assert manager.threshold == 0.7
        assert manager.top_n == 3
        assert len(manager.registry) == 0
        assert manager._provider is None

    def test_custom_threshold_and_top_n(self):
        source = FakeSkillSource(skills=[Skill(name="a")])
        manager = SkillManager(sources=[source], threshold=0.5, top_n=5)
        assert manager.threshold == 0.5
        assert manager.top_n == 5

    def test_provider_setter(self):
        manager = SkillManager(sources=[])
        provider = make_mock_provider("{}")
        manager.provider = provider
        assert manager.provider is provider


class TestSkillManagerDiscovery:
    @pytest.mark.asyncio
    async def test_lazy_discovery(self):
        source = FakeSkillSource(
            skills=[
                Skill(name="a", description="Skill A"),
                Skill(name="b", description="Skill B"),
            ]
        )
        manager = SkillManager(sources=[source])
        assert len(manager.registry) == 0

        await manager._ensure_discovered()
        assert len(manager.registry) == 2
        assert manager.registry.get("a") is not None

    @pytest.mark.asyncio
    async def test_discovery_is_idempotent(self):
        source = FakeSkillSource(skills=[Skill(name="x")])
        manager = SkillManager(sources=[source])
        await manager._ensure_discovered()
        await manager._ensure_discovered()
        assert len(manager.registry) == 1


class TestLLMMatching:
    @pytest.mark.asyncio
    async def test_basic_matching(self):
        source = FakeSkillSource(
            name="local",
            skills=[
                Skill(name="csv-analyzer", description="Analyze CSV files"),
                Skill(name="security-audit", description="Security audit workflow"),
                Skill(name="tdd-workflow", description="Test-driven development"),
            ],
        )
        manager = SkillManager(sources=[source], threshold=0.5)

        response_data = {
            "matches": [
                {"skill_name": "csv-analyzer", "confidence": 0.95, "reasoning": "direct match"},
                {"skill_name": "security-audit", "confidence": 0.3, "reasoning": "not relevant"},
                {"skill_name": "tdd-workflow", "confidence": 0.1, "reasoning": "unrelated"},
            ]
        }
        manager.provider = make_mock_provider(json.dumps(response_data))

        await manager._ensure_discovered()
        local_skills = manager.registry.find_by_source("local")
        matches = await manager._match_via_llm("analyze this CSV file", local_skills, manager.threshold, manager.top_n)

        assert len(matches) == 1
        assert matches[0][0].name == "csv-analyzer"
        assert matches[0][1] == 0.95

    @pytest.mark.asyncio
    async def test_no_matches_above_threshold(self):
        source = FakeSkillSource(name="local", skills=[Skill(name="x", description="Not relevant")])
        manager = SkillManager(sources=[source], threshold=0.8)

        response_data = {
            "matches": [
                {"skill_name": "x", "confidence": 0.4, "reasoning": "low relevance"},
            ]
        }
        manager.provider = make_mock_provider(json.dumps(response_data))

        await manager._ensure_discovered()
        local = manager.registry.find_by_source("local")
        matches = await manager._match_via_llm("unrelated task", local, manager.threshold, manager.top_n)
        assert matches == []

    @pytest.mark.asyncio
    async def test_llm_returns_markdown_fenced_json(self):
        source = FakeSkillSource(name="local", skills=[Skill(name="a", description="Skill A")])
        manager = SkillManager(sources=[source], threshold=0.5)

        manager.provider = make_mock_provider(
            '```json\n{"matches": [{"skill_name": "a", "confidence": 0.9, "reasoning": "match"}]}\n```'
        )

        await manager._ensure_discovered()
        local = manager.registry.find_by_source("local")
        matches = await manager._match_via_llm("task", local, manager.threshold, manager.top_n)
        assert len(matches) == 1
        assert matches[0][0].name == "a"

    @pytest.mark.asyncio
    async def test_llm_error_returns_empty(self):
        source = FakeSkillSource(skills=[Skill(name="a")])
        manager = SkillManager(sources=[source], threshold=0.5)

        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=Exception("API unavailable"))
        manager.provider = provider

        await manager._ensure_discovered()
        local = manager.registry.find_by_source("local")
        matches = await manager._match_via_llm("task", local, manager.threshold, manager.top_n)
        assert matches == []

    @pytest.mark.asyncio
    async def test_no_provider_returns_empty(self):
        source = FakeSkillSource(skills=[Skill(name="a")])
        manager = SkillManager(sources=[source])
        await manager._ensure_discovered()
        local = manager.registry.find_by_source("local")
        matches = await manager._match_via_llm("task", local, manager.threshold, manager.top_n)
        assert matches == []


class TestKeywordScoring:
    def test_basic_overlap(self):
        source = FakeSkillSource(
            skills=[
                Skill(name="csv-tool", description="Process CSV files"),
                Skill(name="image-gen", description="Generate images"),
            ]
        )
        manager = SkillManager(sources=[source], threshold=0.3)
        manager.registry.register_all(source._skills)

        scored = manager._keyword_score("analyze CSV data", manager.registry.list_all(), manager.threshold, manager.top_n)
        assert len(scored) == 1
        assert scored[0][0].name == "csv-tool"

    def test_no_overlap(self):
        source = FakeSkillSource(skills=[Skill(name="x", description="Process widgets")])
        manager = SkillManager(sources=[source], threshold=0.3)
        manager.registry.register_all(source._skills)

        scored = manager._keyword_score("unrelated topic query", manager.registry.list_all(), manager.threshold, manager.top_n)
        assert scored == []


class TestGetTopSkills:
    @pytest.mark.asyncio
    async def test_returns_ranked_skills(self):
        source = FakeSkillSource(
            name="local",
            skills=[
                Skill(name="csv-analyzer", description="Analyze CSV files"),
                Skill(name="security-audit", description="Security audit"),
            ],
        )
        manager = SkillManager(sources=[source], threshold=0.5, top_n=2)

        response_data = {
            "matches": [
                {"skill_name": "csv-analyzer", "confidence": 0.95, "reasoning": "match"},
            ]
        }
        manager.provider = make_mock_provider(json.dumps(response_data))

        skills = await manager.get_top_skills("analyze CSV file")
        assert len(skills) == 1
        assert skills[0].name == "csv-analyzer"

    @pytest.mark.asyncio
    async def test_cache_prevents_redundant_llm_calls(self):
        source = FakeSkillSource(skills=[Skill(name="a", description="Skill A", source="fake")])
        manager = SkillManager(sources=[source], threshold=0.5)

        response_data = {"matches": [{"skill_name": "a", "confidence": 0.9, "reasoning": "match"}]}
        provider = make_mock_provider(json.dumps(response_data))
        manager.provider = provider

        await manager.get_top_skills("task X")
        call_count_1 = provider.chat.call_count

        await manager.get_top_skills("task X")
        call_count_2 = provider.chat.call_count

        assert call_count_2 == call_count_1  # cached

    @pytest.mark.asyncio
    async def test_respects_threshold_override(self):
        source = FakeSkillSource(
            name="local",
            skills=[Skill(name="a", description="A")],
        )
        manager = SkillManager(sources=[source], threshold=0.5)

        response_data = {"matches": [{"skill_name": "a", "confidence": 0.6, "reasoning": "ok"}]}
        manager.provider = make_mock_provider(json.dumps(response_data))

        skills_loose = await manager.get_top_skills("task", threshold=0.5)
        assert len(skills_loose) == 1

        skills_strict = await manager.get_top_skills("task", threshold=0.9)
        assert len(skills_strict) == 0


class TestGetRelevantSkill:
    @pytest.mark.asyncio
    async def test_returns_best_match(self):
        source = FakeSkillSource(
            name="local",
            skills=[Skill(name="best", description="Very relevant")],
        )
        manager = SkillManager(sources=[source], threshold=0.5)

        response_data = {
            "matches": [{"skill_name": "best", "confidence": 0.99, "reasoning": "perfect"}]
        }
        manager.provider = make_mock_provider(json.dumps(response_data))

        skill = await manager.get_relevant_skill("relevant task")
        assert skill is not None
        assert skill.name == "best"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self):
        source = FakeSkillSource(
            name="local",
            skills=[Skill(name="a", description="A")],
        )
        manager = SkillManager(sources=[source], threshold=0.8)

        response_data = {"matches": [{"skill_name": "a", "confidence": 0.3, "reasoning": "low"}]}
        manager.provider = make_mock_provider(json.dumps(response_data))

        skill = await manager.get_relevant_skill("unrelated")
        assert skill is None


class TestPreloadSkills:
    @pytest.mark.asyncio
    async def test_preload_from_source(self):
        source = FakeSkillSource(skills=[Skill(name="preloaded", description="Preloaded skill")])
        manager = SkillManager(sources=[source])

        skills = await manager.preload_skills(["preloaded"])
        assert len(skills) == 1
        assert skills[0].name == "preloaded"

    @pytest.mark.asyncio
    async def test_preload_nonexistent(self):
        source = FakeSkillSource()
        manager = SkillManager(sources=[source])

        skills = await manager.preload_skills(["ghost"])
        assert skills == []


class TestClearCache:
    def test_clears_match_cache(self):
        manager = SkillManager(sources=[])
        manager._match_cache["key"] = [(Skill(name="x"), 1.0)]
        assert len(manager._match_cache) == 1
        manager.clear_cache()
        assert len(manager._match_cache) == 0

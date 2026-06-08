"""Tests for skill_registry.py — registration, caching, deduplication."""

import pytest

from coderAI.skills.skill_loader import Skill
from coderAI.skills.skill_registry import SkillRegistry


class TestSkillRegistry:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.registry = SkillRegistry()

    def test_register_and_get(self):
        s = Skill(name="test", description="A test skill")
        self.registry.register(s)
        assert self.registry.get("test") is s
        assert len(self.registry) == 1

    def test_register_overwrites_duplicate(self):
        a = Skill(name="dup", description="First")
        b = Skill(name="dup", description="Second")
        self.registry.register(a)
        self.registry.register(b)
        assert self.registry.get("dup") is b
        assert self.registry.get("dup").description == "Second"
        assert len(self.registry) == 1

    def test_register_all(self):
        skills = [
            Skill(name="a"),
            Skill(name="b"),
            Skill(name="c"),
        ]
        self.registry.register_all(skills)
        assert len(self.registry) == 3
        assert self.registry.get("a") is not None
        assert self.registry.get("b") is not None
        assert self.registry.get("c") is not None

    def test_list_all(self):
        skills = [Skill(name="x"), Skill(name="y")]
        self.registry.register_all(skills)
        result = self.registry.list_all()
        assert len(result) == 2
        names = {s.name for s in result}
        assert names == {"x", "y"}

    def test_find_by_source(self):
        skills = [
            Skill(name="local-a", source="local"),
            Skill(name="hasna-b", source="hasna"),
            Skill(name="local-c", source="local"),
        ]
        self.registry.register_all(skills)

        local = self.registry.find_by_source("local")
        assert len(local) == 2
        assert all(s.source == "local" for s in local)

        hasna = self.registry.find_by_source("hasna")
        assert len(hasna) == 1

    def test_clear(self):
        self.registry.register(Skill(name="x"))
        assert len(self.registry) == 1
        self.registry.clear()
        assert len(self.registry) == 0
        assert self.registry.get("x") is None

    def test_contains(self):
        self.registry.register(Skill(name="present"))
        assert "present" in self.registry
        assert "absent" not in self.registry

    def test_get_nonexistent(self):
        assert self.registry.get("phantom") is None

"""Tests for skill loader — file discovery, parsing, and safety."""

import os

import pytest

from coderAI.skills.skill_manager import (
    Skill,
    SKILLS_FILE_NAME,
    _is_safe_path,
    _parse_frontmatter,
    discover_local_skills,
    load_skill_by_name,
    load_skill_from_path,
)


class TestSkillDataclass:
    def test_default_construction(self):
        s = Skill(name="test")
        assert s.name == "test"
        assert s.description == ""
        assert s.instructions == ""
        assert s.source == "local"
        assert s.dependencies == []
        assert s.tags == []
        assert s.version is None
        assert s.category is None

    def test_hash_and_equality(self):
        a = Skill(name="foo", description="A")
        b = Skill(name="foo", description="B")
        c = Skill(name="bar")
        assert a == b
        assert a != c
        assert hash(a) == hash(b)
        assert hash(a) != hash(c)

    def test_full_construction(self):
        s = Skill(
            name="csv-analyzer",
            description="Analyze CSV files",
            instructions="## Step 1\nDo stuff",
            version="1.0.0",
            dependencies=["pandas"],
            category="Data",
            tags=["csv", "data"],
            source="hasna",
        )
        assert s.version == "1.0.0"
        assert s.dependencies == ["pandas"]
        assert s.category == "Data"
        assert s.tags == ["csv", "data"]
        assert s.source == "hasna"


class TestParseFrontmatter:
    def test_with_yaml_frontmatter(self):
        metadata, body = _parse_frontmatter(
            "---\nname: foo\ndescription: A foo skill\n---\n\n# Foo\n\nDo foo things.\n"
        )
        assert metadata == {"name": "foo", "description": "A foo skill"}
        assert "# Foo" in body
        assert "Do foo things" in body

    def test_without_frontmatter(self):
        metadata, body = _parse_frontmatter("# Plain content\nNo frontmatter.")
        assert metadata == {}
        assert "Plain content" in body

    def test_empty_file(self):
        metadata, body = _parse_frontmatter("")
        assert metadata == {}
        assert body == ""

    def test_malformed_yaml(self):
        metadata, body = _parse_frontmatter("---\n{[bad yaml]}\n---\n\nInstructions")
        assert metadata == {}
        assert "Instructions" in body


class TestLoadSkillFromPath:
    def test_new_format(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / SKILLS_FILE_NAME
        skill_file.write_text(
            "---\nname: my-skill\ndescription: A test\n---\n\n# My Skill\n\nStep 1: Do X.\n"
        )

        skill = load_skill_from_path(skill_file, source="local")
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "A test"
        assert "Step 1" in skill.instructions
        assert skill.source == "local"

    def test_nonexistent_file(self, tmp_path):
        skill = load_skill_from_path(tmp_path / "nonexistent" / SKILLS_FILE_NAME)
        assert skill is None

    def test_file_too_large(self, tmp_path):
        skill_file = tmp_path / SKILLS_FILE_NAME
        skill_file.write_text("x" * (100 * 1024 + 1))
        from coderAI.skills.skill_manager import MAX_SKILL_FILE_BYTES

        assert skill_file.stat().st_size > MAX_SKILL_FILE_BYTES
        skill = load_skill_from_path(skill_file)
        assert skill is None


class TestDiscoverLocalSkills:
    def test_subdirectory_format(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)

        (dot_dir / "skill-a").mkdir()
        (dot_dir / "skill-a" / SKILLS_FILE_NAME).write_text(
            "---\nname: skill-a\ndescription: First skill\n---\n\n## A\n"
        )

        (dot_dir / "skill-b").mkdir()
        (dot_dir / "skill-b" / SKILLS_FILE_NAME).write_text(
            "---\nname: skill-b\ndescription: Second skill\n---\n\n## B\n"
        )

        skills = discover_local_skills(str(tmp_path))
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"skill-a", "skill-b"}

    def test_empty_directory(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)
        skills = discover_local_skills(str(tmp_path))
        assert skills == []


class TestLoadSkillByName:
    def test_load_by_name_subdirectory(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)
        (dot_dir / "csv-analyzer").mkdir()
        (dot_dir / "csv-analyzer" / SKILLS_FILE_NAME).write_text(
            "---\nname: csv-analyzer\ndescription: Parse CSV\n---\n\n# CSV\n"
        )

        skill = load_skill_by_name("csv-analyzer", str(tmp_path))
        assert skill is not None
        assert skill.name == "csv-analyzer"
        assert "CSV" in skill.instructions

    def test_path_traversal_rejected(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)

        skill = load_skill_by_name("../../etc/passwd", str(tmp_path))
        assert skill is None

        skill = load_skill_by_name("foo/../bar", str(tmp_path))
        assert skill is None

    def test_nonexistent_skill(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)

        skill = load_skill_by_name("nonexistent", str(tmp_path))
        assert skill is None


class TestIsSafePath:
    def test_child_and_root_accepted(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        assert _is_safe_path(root, root)
        assert _is_safe_path(root / "sub" / "SKILLS.md", root)

    def test_sibling_with_shared_prefix_rejected(self, tmp_path):
        root = tmp_path / "skills"
        root.mkdir()
        evil = tmp_path / "skills-evil"
        evil.mkdir()
        assert not _is_safe_path(evil / "payload.md", root)

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_symlink_escape_to_prefix_sibling_rejected(self, tmp_path):
        dot_dir = tmp_path / ".coderAI" / "skills"
        dot_dir.mkdir(parents=True)
        evil = tmp_path / ".coderAI" / "skills-evil"
        evil.mkdir()
        target = evil / "escape.md"
        target.write_text("---\nname: escape\ndescription: outside\n---\n\n# Escape\n")
        (dot_dir / "escape.md").symlink_to(target)

        assert load_skill_by_name("escape", str(tmp_path)) is None

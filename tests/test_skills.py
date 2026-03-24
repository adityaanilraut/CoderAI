"""Tests for Skills loading and UseSkillTool."""

import asyncio
import tempfile
from pathlib import Path
import pytest

from coderAI.skills import load_skill, get_available_skills
from coderAI.tools.skills import UseSkillTool


@pytest.fixture
def skills_dir(tmp_path):
    """Create a temp skills directory with sample skills."""
    sd = tmp_path / ".coderAI" / "skills"
    sd.mkdir(parents=True)

    # Create a test skill
    (sd / "test-skill.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n"
        "# Test Skill\n\n## Step 1\nDo step 1.\n\n## Step 2\nDo step 2.\n"
    )

    # Create a skill without frontmatter
    (sd / "plain-skill.md").write_text("# Plain Skill\n\nJust instructions.\n")

    return tmp_path


class TestSkillLoading:
    def test_load_skill_with_frontmatter(self, skills_dir):
        skill = load_skill("test-skill", str(skills_dir))
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert "Step 1" in skill.instructions

    def test_load_skill_without_frontmatter(self, skills_dir):
        skill = load_skill("plain-skill", str(skills_dir))
        assert skill is not None
        assert skill.name == "plain-skill"
        assert "Plain Skill" in skill.instructions

    def test_load_nonexistent_skill(self, skills_dir):
        skill = load_skill("nonexistent", str(skills_dir))
        assert skill is None

    def test_get_available_skills(self, skills_dir):
        skills = get_available_skills(str(skills_dir))
        assert len(skills) == 2
        names = [s["name"] for s in skills]
        assert "test-skill" in names


class TestUseSkillTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = UseSkillTool()

    def test_list_action(self):
        # Will use the project's actual .coderAI/skills/ dir
        result = asyncio.run(
            self.tool.execute(action="list")
        )
        assert result["success"]

    def test_unknown_action(self):
        result = asyncio.run(
            self.tool.execute(action="invalid")
        )
        assert not result["success"]

    def test_use_without_name(self):
        result = asyncio.run(
            self.tool.execute(action="use")
        )
        assert not result["success"]

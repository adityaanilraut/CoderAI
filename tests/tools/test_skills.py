"""Tests for Skills loading and UseSkillTool."""

import asyncio
import pytest

from coderAI.tools.use_skill import UseSkillTool, load_skill, get_available_skills
from coderAI.skills.skill_manager import SKILLS_FILE_NAME
from coderAI.types.provenance import Provenance


@pytest.fixture
def skills_dir(tmp_path):
    """Create a temp skills directory with sample skills (subdir format)."""
    sd = tmp_path / ".coderAI" / "skills"
    sd.mkdir(parents=True)

    skill_a = sd / "test-skill"
    skill_a.mkdir()
    (skill_a / SKILLS_FILE_NAME).write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n"
        "# Test Skill\n\n## Step 1\nDo step 1.\n\n## Step 2\nDo step 2.\n"
    )

    skill_b = sd / "plain-skill"
    skill_b.mkdir()
    (skill_b / SKILLS_FILE_NAME).write_text("# Plain Skill\n\nJust instructions.\n")

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
        skills = get_available_skills(str(skills_dir), include_builtin=False)
        assert len(skills) == 2
        names = [s["name"] for s in skills]
        assert "test-skill" in names


class TestUseSkillTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = UseSkillTool()

    def test_list_action(self):
        # Will use the project's actual .coderAI/skills/ dir
        result = asyncio.run(self.tool.execute(action="list"))
        assert result["success"]

    def test_unknown_action(self):
        result = asyncio.run(self.tool.execute(action="invalid"))
        assert not result["success"]

    def test_use_without_name(self):
        result = asyncio.run(self.tool.execute(action="use"))
        assert not result["success"]

    def test_results_are_labelled_untrusted(self):
        """Skill markdown is third-party content — ``skills install`` takes any repo.

        Without this label the tool handed a loaded skill to the model with system
        authority and left the egress / mutating-local gates disarmed, even though
        the auto-injection path fences the identical text.
        """
        assert self.tool.result_provenance == Provenance.UNTRUSTED_EXTERNAL

    def test_loaded_instructions_are_fenced_as_project_guidance(self, skills_dir, monkeypatch):
        monkeypatch.chdir(skills_dir)
        result = asyncio.run(self.tool.execute(action="use", skill_name="test-skill"))

        assert result["success"], result
        instructions = result["instructions"]
        assert instructions.startswith("[BEGIN PROJECT SKILL")
        assert instructions.rstrip().endswith("[END PROJECT SKILL]")
        assert "Do step 1." in instructions

    def test_services_pin_blocks_project_skills_after_mid_session_trust(
        self, skills_dir, monkeypatch
    ):
        """Agent pin False must win over a live trusted store / env trust."""
        from coderAI.core.services import services_scope

        monkeypatch.chdir(skills_dir)
        with services_scope(inherit=True, workspace_trusted=False):
            listed = asyncio.run(self.tool.execute(action="list"))
            used = asyncio.run(self.tool.execute(action="use", skill_name="test-skill"))

        assert listed["success"] is True
        assert {item["name"] for item in listed["skills"]} == {
            "security-audit",
            "tdd-workflow",
        }
        assert {item["source"] for item in listed["skills"]} == {"builtin"}
        assert used["success"] is False
        assert "not found" in used["error"]

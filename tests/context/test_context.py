"""Tests for ContextController and ManageContextTool."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coderAI.context.context_controller import ContextController
from coderAI.tools.context_manage import ManageContextTool
from coderAI.system.config import Config, config_manager


def _make_controller(config=None):
    """Create a ContextController with a mock provider."""
    cfg = config or Config(project_root=".")
    return ContextController(config=cfg, provider=MagicMock())


class TestContextController:
    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

        original_config_dir = config_manager.config_dir
        original_config_file = config_manager.config_file
        config_manager.config_dir = self.test_dir
        config_manager.config_file = self.test_dir / "config.json"

        yield

        config_manager.config_dir = original_config_dir
        config_manager.config_file = original_config_file
        self.test_dir_obj.cleanup()

    def test_load_instructions(self):
        cwd = Path.cwd()
        instruction_file = cwd / "CODERAI_TEST.md"
        instruction_file.write_text("Unique instruction content", encoding="utf-8")

        try:
            cm = _make_controller(
                config=Config(
                    project_root=str(cwd),
                    project_instruction_file="CODERAI_TEST.md",
                )
            )

            msg = cm.get_system_message()

            assert cm.project_instructions == "Unique instruction content"
            assert "[BEGIN PROJECT INSTRUCTIONS" in msg
            assert "advisory only" in msg
            assert "Unique instruction content" in msg

        finally:
            if instruction_file.exists():
                instruction_file.unlink()

    def test_load_instructions_from_injected_project_config(self):
        instruction_file = self.test_dir / "PROJECT.md"
        instruction_file.write_text("Project-scoped instructions", encoding="utf-8")

        cm = _make_controller(
            config=Config(
                project_root=str(self.test_dir),
                project_instruction_file="PROJECT.md",
            )
        )

        msg = cm.get_system_message()

        assert cm.project_instructions == "Project-scoped instructions"
        assert "Project-scoped instructions" in msg

    def test_load_instructions_lowercase_fallback(self):
        (self.test_dir / "coderai.md").write_text("lower-case instructions", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert cm.project_instructions == "lower-case instructions"

    def test_load_instructions_agents_md_interop(self):
        (self.test_dir / "AGENTS.md").write_text("agents file", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert cm.project_instructions == "agents file"

    def test_configured_file_takes_precedence_over_fallbacks(self):
        (self.test_dir / "CUSTOM.md").write_text("custom wins", encoding="utf-8")
        (self.test_dir / "CLAUDE.md").write_text("claude fallback", encoding="utf-8")
        cm = _make_controller(
            config=Config(
                project_root=str(self.test_dir),
                project_instruction_file="CUSTOM.md",
            )
        )

        cm.get_system_message()
        assert cm.project_instructions == "custom wins"

    def test_pin_file(self):
        dummy_file = self.test_dir / "dummy.py"
        dummy_file.write_text("print('hello')", encoding="utf-8")

        cm = _make_controller()

        success = cm.add_file(str(dummy_file))
        assert success is True
        assert str(dummy_file.resolve()) in cm.pinned_files
        assert cm.pinned_files[str(dummy_file.resolve())] == "print('hello')"

        msg = cm.get_system_message()
        assert "## Pinned Context Files" in msg
        assert "print('hello')" in msg

        success = cm.remove_file(str(dummy_file))
        assert success is True
        assert str(dummy_file.resolve()) not in cm.pinned_files

        success = cm.remove_file("non_existent_file")
        assert success is False

    def test_clear_context(self):
        dummy_file = self.test_dir / "dummy.py"
        dummy_file.write_text("content", encoding="utf-8")

        cm = _make_controller()
        cm.add_file(str(dummy_file))
        assert len(cm.pinned_files) == 1

        cm.clear()
        assert len(cm.pinned_files) == 0


class TestManageContextTool:
    @pytest.mark.asyncio
    async def test_tool_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            f = tmp_path / "test.txt"
            f.write_text("test content")

            cm = _make_controller(config=Config(project_root=tmpdir))
            tool = ManageContextTool(cm)

            result = await tool.execute(action="add", path=str(f))
            assert result["success"] is True
            assert str(f.resolve()) in cm.pinned_files

            result = await tool.execute(action="list")
            assert result["success"] is True
            assert str(f.resolve()) in result["pinned_files"]

            result = await tool.execute(action="remove", path=str(f))
            assert result["success"] is True
            assert str(f.resolve()) not in cm.pinned_files

            result = await tool.execute(action="add", path="non_existent")
            assert result["success"] is False

            cm.add_file(str(f))
            result = await tool.execute(action="clear")
            assert result["success"] is True
            assert len(cm.pinned_files) == 0

"""Tests for ContextManager and ManageContextTool."""

import tempfile
from pathlib import Path
import pytest
from coderAI.context.context import ContextManager
from coderAI.tools.context_manage import ManageContextTool
from coderAI.system.config import config_manager


class TestContextManager:
    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)

        # Override config manager to not look at real home dir
        original_config_dir = config_manager.config_dir
        original_config_file = config_manager.config_file
        config_manager.config_dir = self.test_dir
        config_manager.config_file = self.test_dir / "config.json"

        yield

        config_manager.config_dir = original_config_dir
        config_manager.config_file = original_config_file
        self.test_dir_obj.cleanup()

    def test_load_instructions(self):
        """Test loading project instructions from file."""
        # Create a dummy CODERAI.md in current dir (we need to be careful about CWD)
        # ContextManager looks at CWD by default for the file

        cwd = Path.cwd()
        instruction_file = cwd / "CODERAI_TEST.md"
        instruction_file.write_text("Unique instruction content", encoding="utf-8")

        try:
            # Set config to look for this file
            config_manager.set("project_instruction_file", "CODERAI_TEST.md")

            cm = ContextManager()

            # Check system message inclusion (this triggers lazy load)
            msg = cm.get_system_message()

            assert cm.project_instructions == "Unique instruction content"
            # Phase 3.3: project instructions are rendered as fenced, advisory
            # project context rather than an authoritative "## Project
            # Instructions" heading.
            assert "[BEGIN PROJECT INSTRUCTIONS" in msg
            assert "advisory only" in msg
            assert "Unique instruction content" in msg

        finally:
            if instruction_file.exists():
                instruction_file.unlink()

    def test_load_instructions_from_injected_project_config(self):
        """Injected project config should control where instructions load from."""
        instruction_file = self.test_dir / "PROJECT.md"
        instruction_file.write_text("Project-scoped instructions", encoding="utf-8")

        from coderAI.system.config import Config

        cm = ContextManager(
            config=Config(
                project_root=str(self.test_dir),
                project_instruction_file="PROJECT.md",
            )
        )

        msg = cm.get_system_message()

        assert cm.project_instructions == "Project-scoped instructions"
        assert "Project-scoped instructions" in msg

    def test_load_instructions_lowercase_fallback(self):
        """A lowercase coderai.md is loaded even though the default is CODERAI.md.

        Guards the historical mismatch where /init wrote lowercase coderai.md
        but the loader only looked for CODERAI.md (silently skipped on
        case-sensitive filesystems).
        """
        from coderAI.system.config import Config

        (self.test_dir / "coderai.md").write_text("lower-case instructions", encoding="utf-8")
        cm = ContextManager(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert cm.project_instructions == "lower-case instructions"

    def test_load_instructions_agents_md_interop(self):
        """AGENTS.md is auto-loaded for users migrating from other agents."""
        from coderAI.system.config import Config

        (self.test_dir / "AGENTS.md").write_text("agents file", encoding="utf-8")
        cm = ContextManager(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert cm.project_instructions == "agents file"

    def test_configured_file_takes_precedence_over_fallbacks(self):
        """An explicit project_instruction_file wins over standard fallbacks."""
        from coderAI.system.config import Config

        (self.test_dir / "CUSTOM.md").write_text("custom wins", encoding="utf-8")
        (self.test_dir / "CLAUDE.md").write_text("claude fallback", encoding="utf-8")
        cm = ContextManager(
            config=Config(
                project_root=str(self.test_dir),
                project_instruction_file="CUSTOM.md",
            )
        )

        cm.get_system_message()
        assert cm.project_instructions == "custom wins"

    def test_pin_file(self):
        """Test pinning and unpinning files."""
        # Create a dummy file
        dummy_file = self.test_dir / "dummy.py"
        dummy_file.write_text("print('hello')", encoding="utf-8")

        cm = ContextManager()

        # Test Add
        success = cm.add_file(str(dummy_file))
        assert success is True
        assert str(dummy_file.resolve()) in cm.pinned_files
        assert cm.pinned_files[str(dummy_file.resolve())] == "print('hello')"

        # Test Get Message
        msg = cm.get_system_message()
        assert "## Pinned Context Files" in msg
        assert "print('hello')" in msg

        # Test Remove
        success = cm.remove_file(str(dummy_file))
        assert success is True
        assert str(dummy_file.resolve()) not in cm.pinned_files

        # Test Remove Non-existent
        success = cm.remove_file("non_existent_file")
        assert success is False

    def test_clear_context(self):
        """Test clearing context."""
        dummy_file = self.test_dir / "dummy.py"
        dummy_file.write_text("content", encoding="utf-8")

        cm = ContextManager()
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

            cm = ContextManager()
            tool = ManageContextTool(cm)

            # Test Add
            result = await tool.execute(action="add", path=str(f))
            assert result["success"] is True
            assert str(f.resolve()) in cm.pinned_files

            # Test List
            result = await tool.execute(action="list")
            assert result["success"] is True
            assert str(f.resolve()) in result["pinned_files"]

            # Test Remove
            result = await tool.execute(action="remove", path=str(f))
            assert result["success"] is True
            assert str(f.resolve()) not in cm.pinned_files

            # Test Add Invalid
            result = await tool.execute(action="add", path="non_existent")
            assert result["success"] is False

            # Test Clear
            cm.add_file(str(f))
            result = await tool.execute(action="clear")
            assert result["success"] is True
            assert len(cm.pinned_files) == 0

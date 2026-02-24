"""Tests for ContextManager and ManageContextTool."""

import shutil
import tempfile
from pathlib import Path
import pytest
from coderAI.context import ContextManager
from coderAI.tools.context_manage import ManageContextTool
from coderAI.config import config_manager


class TestContextManager:
    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        self.test_dir_obj = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.test_dir_obj.name)
        
        # Override config manager to not look at real home dir
        original_config_dir = config_manager.config_dir
        config_manager.config_dir = self.test_dir
        
        yield
        
        config_manager.config_dir = original_config_dir
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
            assert cm.project_instructions == "Unique instruction content"
            
            # Check system message inclusion
            msg = cm.get_system_message()
            assert "## Project Instructions" in msg
            assert "Unique instruction content" in msg
            
        finally:
            if instruction_file.exists():
                instruction_file.unlink()

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

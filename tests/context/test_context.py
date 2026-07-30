"""Tests for ContextController and ManageContextTool."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coderAI.context.context_controller import ContextController
from coderAI.tools.context_manage import ManageContextTool
from coderAI.system.config import Config, config_manager


def _make_controller(config=None, *, allow_project_instructions=True):
    """Create a ContextController with a mock provider."""
    cfg = config or Config(project_root=".")
    return ContextController(
        config=cfg,
        provider=MagicMock(),
        allow_project_instructions=allow_project_instructions,
    )


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

            assert "Unique instruction content" in cm.project_instructions
            assert "[BEGIN PROJECT INSTRUCTIONS" in msg
            assert "below live user and safety" in msg
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

        assert "Project-scoped instructions" in cm.project_instructions
        assert "Project-scoped instructions" in msg

    def test_load_instructions_lowercase_fallback(self):
        (self.test_dir / "coderai.md").write_text("lower-case instructions", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert "lower-case instructions" in cm.project_instructions

    def test_load_instructions_agents_md_interop(self):
        (self.test_dir / "AGENTS.md").write_text("agents file", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        cm.get_system_message()
        assert "agents file" in cm.project_instructions

    def test_configured_and_fallback_files_compose_in_order(self):
        (self.test_dir / "CUSTOM.md").write_text("custom wins", encoding="utf-8")
        (self.test_dir / "CLAUDE.md").write_text("claude fallback", encoding="utf-8")
        cm = _make_controller(
            config=Config(
                project_root=str(self.test_dir),
                project_instruction_file="CUSTOM.md",
            )
        )

        cm.get_system_message()
        assert "custom wins" in cm.project_instructions
        assert "claude fallback" in cm.project_instructions
        assert cm.project_instructions.index("custom wins") < cm.project_instructions.index(
            "claude fallback"
        )

    def test_untrusted_controller_does_not_load_project_instructions(self):
        (self.test_dir / "AGENTS.md").write_text("do not load", encoding="utf-8")
        cm = _make_controller(
            config=Config(project_root=str(self.test_dir)),
            allow_project_instructions=False,
        )

        assert cm.get_system_message() is None
        assert cm.project_instructions is None

    def test_nested_agents_files_compose_and_refresh(self, monkeypatch):
        nested = self.test_dir / "src" / "feature"
        nested.mkdir(parents=True)
        root_agents = self.test_dir / "AGENTS.md"
        nested_agents = nested / "AGENTS.md"
        root_agents.write_text("root guidance", encoding="utf-8")
        nested_agents.write_text("nested guidance", encoding="utf-8")
        monkeypatch.chdir(nested)
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        first = cm.get_system_message()
        assert first.index("root guidance") < first.index("nested guidance")

        nested_agents.write_text("updated nested guidance", encoding="utf-8")
        assert "updated nested guidance" in cm.get_system_message()

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

    def test_pin_rejects_protected_file(self, monkeypatch):
        protected = self.test_dir / "credentials.txt"
        protected.write_text("do-not-send", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        monkeypatch.setattr(
            "coderAI.tools.filesystem._guards._is_path_protected",
            lambda candidate: candidate == protected.resolve(),
        )

        assert cm.add_file(str(protected)) is False
        assert cm.pinned_files == {}

    def test_pin_rejects_symlink_leaf(self):
        target = self.test_dir / "target.txt"
        target.write_text("sensitive", encoding="utf-8")
        link = self.test_dir / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")

        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        assert cm.add_file(str(link)) is False
        assert cm.pinned_files == {}

    def test_refresh_removes_file_replaced_by_symlink(self):
        pinned = self.test_dir / "pinned.txt"
        replacement = self.test_dir / "replacement.txt"
        pinned.write_text("safe content", encoding="utf-8")
        replacement.write_text("must not be read", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))
        assert cm.add_file(str(pinned)) is True
        pinned_key = str(pinned.resolve())

        pinned.unlink()
        try:
            pinned.symlink_to(replacement)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        cm._last_refresh_at = float("-inf")

        cm.refresh_pinned_files()

        assert pinned_key not in cm.pinned_files
        assert "must not be read" not in "".join(cm.pinned_files.values())

    def test_inject_context_refreshes_pinned_file_before_cache_hit(self):
        pinned = self.test_dir / "pinned.txt"
        pinned.write_text("old pinned value", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))
        assert cm.add_file(str(pinned))

        first = cm.inject_context([], query="pinned value")
        assert "old pinned value" in first[0]["content"]

        pinned.write_text("new pinned value", encoding="utf-8")
        second = cm.inject_context([], query="pinned value")

        assert "new pinned value" in second[0]["content"]
        assert "old pinned value" not in second[0]["content"]

    def test_inject_context_invalidates_cache_when_instructions_change(self):
        instructions = self.test_dir / "AGENTS.md"
        instructions.write_text("old project guidance", encoding="utf-8")
        cm = _make_controller(config=Config(project_root=str(self.test_dir)))

        first = cm.inject_context([], query="same query")
        assert "old project guidance" in first[0]["content"]

        instructions.write_text("new project guidance with more text", encoding="utf-8")
        second = cm.inject_context([], query="same query")

        assert "new project guidance with more text" in second[0]["content"]
        assert "old project guidance" not in second[0]["content"]

    def test_clear_context(self):
        dummy_file = self.test_dir / "dummy.py"
        dummy_file.write_text("content", encoding="utf-8")

        cm = _make_controller()
        cm.add_file(str(dummy_file))
        assert len(cm.pinned_files) == 1

        cm.clear()
        assert len(cm.pinned_files) == 0

    def test_request_budget_counts_model_override_output_tools_reasoning_and_images(self):
        provider = SimpleNamespace(
            count_tokens=lambda text: len(text) // 4,
            model_context_window=32_000,
            max_tokens=4_096,
        )
        controller = ContextController(
            config=Config(context_window=128_000, max_tokens=8_192),
            provider=provider,  # type: ignore[arg-type]
        )
        controller.request_tool_schemas = [
            {"type": "function", "function": {"name": "read", "description": "x" * 80}}
        ]

        message_tokens = controller.estimate_tokens(
            [
                {
                    "role": "assistant",
                    "content": "abcdefgh",
                    "reasoning_content": "abcdefghijkl",
                    "tool_images": [{"data": "a"}, {"data": "b"}],
                }
            ]
        )

        assert controller._effective_context_limit(None) == 32_000
        assert controller._output_token_reserve() == 4_096
        assert controller.estimate_tool_tokens() > 0
        assert message_tokens >= 3_000 + 2 + 3


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

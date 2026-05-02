"""Tests for CoderAI tools."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


# ============================================================================
# Filesystem Tools
# ============================================================================


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_file(temp_dir):
    """Create a sample file for testing."""
    filepath = os.path.join(temp_dir, "test_file.py")
    with open(filepath, "w") as f:
        f.write("def hello():\n    print('Hello, World!')\n\nhello()\n")
    return filepath


class TestReadFileTool:
    """Tests for ReadFileTool."""

    def test_read_existing_file(self, sample_file):
        from coderAI.tools.filesystem import ReadFileTool

        tool = ReadFileTool()
        result = asyncio.run(tool.execute(path=sample_file))
        assert result["success"] is True
        assert "Hello, World!" in result["content"]

    def test_read_nonexistent_file(self, temp_dir):
        from coderAI.tools.filesystem import ReadFileTool

        tool = ReadFileTool()
        result = asyncio.run(tool.execute(path=os.path.join(temp_dir, "nonexistent.txt")))
        assert result["success"] is False
        assert "error" in result

    def test_read_with_line_range(self, sample_file):
        from coderAI.tools.filesystem import ReadFileTool

        tool = ReadFileTool()
        result = asyncio.run(tool.execute(path=sample_file, start_line=1, end_line=2))
        assert result["success"] is True
        assert "def hello" in result["content"]

    def test_parameters_schema(self):
        from coderAI.tools.filesystem import ReadFileTool

        tool = ReadFileTool()
        params = tool.get_parameters()
        assert params["type"] == "object"
        assert "path" in params["properties"]
        assert "path" in params["required"]


class TestWriteFileTool:
    """Tests for WriteFileTool."""

    def test_write_new_file(self, temp_dir):
        from coderAI.tools.filesystem import WriteFileTool

        tool = WriteFileTool()
        filepath = os.path.join(temp_dir, "new_file.txt")
        result = asyncio.run(tool.execute(path=filepath, content="hello world"))
        assert result["success"] is True
        with open(filepath) as f:
            assert f.read() == "hello world"

    def test_write_creates_parent_dirs(self, temp_dir):
        from coderAI.tools.filesystem import WriteFileTool

        tool = WriteFileTool()
        filepath = os.path.join(temp_dir, "subdir", "nested", "file.txt")
        result = asyncio.run(tool.execute(path=filepath, content="nested"))
        assert result["success"] is True
        assert os.path.exists(filepath)


class TestSearchReplaceTool:
    """Tests for SearchReplaceTool."""

    def test_search_and_replace(self, sample_file):
        from coderAI.tools.filesystem import SearchReplaceTool

        tool = SearchReplaceTool()
        result = asyncio.run(
            tool.execute(
                path=sample_file,
                search="Hello, World!",
                replace="Hi, Universe!",
            )
        )
        assert result["success"] is True
        with open(sample_file) as f:
            assert "Hi, Universe!" in f.read()

    def test_search_text_not_found(self, sample_file):
        from coderAI.tools.filesystem import SearchReplaceTool

        tool = SearchReplaceTool()
        result = asyncio.run(
            tool.execute(
                path=sample_file,
                search="NOT_IN_FILE",
                replace="replacement",
            )
        )
        assert result["success"] is False

    def test_empty_path_rejected(self):
        from coderAI.tools.filesystem import SearchReplaceTool

        tool = SearchReplaceTool()
        result = asyncio.run(
            tool.execute(path="", search="x", replace="y")
        )
        assert result["success"] is False
        assert "path is required" in result["error"].lower()

    def test_empty_search_rejected(self, sample_file):
        from coderAI.tools.filesystem import SearchReplaceTool

        tool = SearchReplaceTool()
        result = asyncio.run(
            tool.execute(path=sample_file, search="", replace="y")
        )
        assert result["success"] is False
        assert "search text" in result["error"].lower()


class TestListDirectoryTool:
    """Tests for ListDirectoryTool."""

    def test_list_directory(self, temp_dir, sample_file):
        from coderAI.tools.filesystem import ListDirectoryTool

        tool = ListDirectoryTool()
        result = asyncio.run(tool.execute(path=temp_dir))
        assert result["success"] is True
        assert any("test_file.py" in e["name"] for e in result["entries"])


class TestGlobSearchTool:
    """Tests for GlobSearchTool."""

    def test_glob_search(self, temp_dir, sample_file):
        from coderAI.tools.filesystem import GlobSearchTool

        tool = GlobSearchTool()
        result = asyncio.run(tool.execute(pattern="**/*.py", base_path=temp_dir))
        assert result["success"] is True
        assert result["count"] > 0


# ============================================================================
# Terminal Tools
# ============================================================================


class TestRunCommandTool:
    """Tests for RunCommandTool."""

    def test_run_simple_command(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="echo 'hello test'"))
        assert result["success"] is True
        assert "hello test" in result["stdout"]

    def test_run_command_with_timeout(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="sleep 10", timeout=1))
        assert result["success"] is False
        assert "timed out" in result.get("error", "").lower()

    def test_blocked_command(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="rm -rf /"))
        assert result["success"] is False
        assert result.get("blocked") is True

    def test_command_that_fails(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="false"))
        assert result["success"] is False
        assert result["returncode"] != 0


class TestCommandSafety:
    """Tests for command safety checks."""

    def test_is_command_blocked(self):
        from coderAI.tools.terminal import is_command_blocked

        assert is_command_blocked("rm -rf /")
        assert not is_command_blocked("echo hello")
        assert not is_command_blocked("ls -la")

    def test_is_command_dangerous(self):
        from coderAI.tools.terminal import is_command_dangerous

        assert is_command_dangerous("rm file.txt")
        assert is_command_dangerous("sudo apt install something")
        assert not is_command_dangerous("echo hello")
        assert not is_command_dangerous("ls -la")
        assert not is_command_dangerous("cat file.txt")


# ============================================================================
# Git Tools
# ============================================================================


class TestGitTools:
    """Tests for Git tools (require git to be installed)."""

    @pytest.fixture
    def git_repo(self, temp_dir):
        """Create a temp git repo."""
        asyncio.run(self._init_repo(temp_dir))
        return temp_dir

    async def _init_repo(self, path):
        proc = await asyncio.create_subprocess_exec(
            "git", "init", cwd=path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "config", "user.email", "test@test.com", cwd=path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git", "config", "user.name", "Test", cwd=path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    def test_git_status(self, git_repo):
        from coderAI.tools.git import GitStatusTool

        tool = GitStatusTool()
        result = asyncio.run(tool.execute(repo_path=git_repo))
        assert result["success"] is True

    def test_git_log_empty_repo(self, git_repo):
        from coderAI.tools.git import GitLogTool

        tool = GitLogTool()
        result = asyncio.run(tool.execute(repo_path=git_repo))
        assert isinstance(result, dict)

    def test_git_commit_with_special_chars(self, git_repo):
        """Test that git commit handles special characters safely (no shell injection)."""
        from coderAI.tools.git import GitCommitTool

        tool = GitCommitTool()
        filepath = os.path.join(git_repo, "test.txt")
        with open(filepath, "w") as f:
            f.write("test content")

        asyncio.run(self._stage_file(git_repo, filepath))

        result = asyncio.run(
            tool.execute(
                message='test message with "quotes" and $(whoami) and `backticks`',
                repo_path=git_repo,
            )
        )
        assert result["success"] is True

    async def _stage_file(self, repo_path, filepath):
        proc = await asyncio.create_subprocess_exec(
            "git", "add", filepath, cwd=repo_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()


# ============================================================================
# Search Tools
# ============================================================================


class TestTextSearchTool:
    """Tests for TextSearchTool (formerly CodebaseSearchTool)."""

    def test_search_finds_content(self, temp_dir, sample_file):
        from coderAI.tools.search import TextSearchTool

        tool = TextSearchTool()
        result = asyncio.run(tool.execute(query="Hello", base_path=temp_dir))
        assert result["success"] is True
        assert result["count"] > 0

    def test_search_no_results(self, temp_dir, sample_file):
        from coderAI.tools.search import TextSearchTool

        tool = TextSearchTool()
        result = asyncio.run(
            tool.execute(query="NONEXISTENT_TEXT_xyz123", base_path=temp_dir)
        )
        assert result["success"] is True
        assert result["count"] == 0

    def test_search_invalid_path(self):
        from coderAI.tools.search import TextSearchTool

        tool = TextSearchTool()
        result = asyncio.run(
            tool.execute(query="test", base_path="/nonexistent/path/xyz")
        )
        assert result["success"] is False


class TestGrepTool:
    """Tests for GrepTool."""

    def test_grep_finds_pattern(self, temp_dir, sample_file):
        from coderAI.tools.search import GrepTool

        tool = GrepTool()
        result = asyncio.run(tool.execute(pattern="hello", path=temp_dir, case_insensitive=True))
        assert result["success"] is True
        assert result["count"] > 0


# ============================================================================
# Config Management
# ============================================================================


class TestConfig:
    """Tests for configuration management."""

    def test_config_defaults(self):
        from coderAI.config import Config

        config = Config()
        assert config.default_model == "claude-4-sonnet"
        assert config.temperature == 0.7
        assert config.streaming is True
        assert config.save_history is True
        assert config.log_level == "WARNING"

    def test_config_validation(self):
        from coderAI.config import Config
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Config(temperature=3.0)

    def test_config_manager_save_load(self, temp_dir):
        from coderAI.config import Config, ConfigManager

        manager = ConfigManager()
        manager.config_dir = Path(temp_dir)
        manager.config_file = Path(temp_dir) / "config.json"

        manager._config = Config(default_model="gpt-5.4")
        manager.save()

        # Verify file permissions (unix-only)
        if os.name != "nt":
            mode = oct(os.stat(manager.config_file).st_mode)[-3:]
            assert mode == "600", f"Config file should be 0600, got {mode}"

        manager._config = None
        config = manager.load()
        assert config.default_model == "gpt-5.4"

    def test_config_show_masks_keys(self):
        from coderAI.config import Config, ConfigManager

        manager = ConfigManager()
        manager._config = Config(openai_api_key="sk-1234567890abcdef1234567890abcdef")
        shown = manager.show()
        # Should be partially masked (first 7 chars + ***)
        assert "***" in shown["openai_api_key"]
        # Should NOT show the full key
        assert shown["openai_api_key"] != "sk-1234567890abcdef1234567890abcdef"


# ============================================================================
# History Management
# ============================================================================


class TestHistory:
    """Tests for history management."""

    def test_message_creation(self):
        from coderAI.history import Message

        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.timestamp > 0

    def test_session_creation(self):
        from coderAI.history import HistoryManager

        manager = HistoryManager()
        session = manager.create_session(model="test-model")
        assert session.session_id is not None
        assert len(session.messages) == 0

    def test_session_add_message(self):
        from coderAI.history import HistoryManager

        manager = HistoryManager()
        session = manager.create_session(model="test-model")
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi there!")
        assert len(session.messages) == 2
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"

    def test_session_get_messages_for_api(self):
        from coderAI.history import HistoryManager

        manager = HistoryManager()
        session = manager.create_session(model="test-model")
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi")
        api_messages = session.get_messages_for_api()
        assert len(api_messages) == 2
        assert api_messages[0]["role"] == "user"
        assert api_messages[0]["content"] == "Hello"

    def test_history_manager_save_load(self, temp_dir):
        from coderAI.history import HistoryManager

        manager = HistoryManager()
        manager.history_dir = Path(temp_dir)

        session = manager.create_session(model="test-model")
        session.add_message("user", "test message")
        manager.save_session(session)

        loaded = manager.load_session(session.session_id)
        assert loaded is not None
        assert len(loaded.messages) == 1
        assert loaded.messages[0].content == "test message"

    def test_history_manager_list_sessions(self, temp_dir):
        from coderAI.history import HistoryManager
        import time

        manager = HistoryManager()
        manager.history_dir = Path(temp_dir)

        s1 = manager.create_session(model="test")
        s1.add_message("user", "session 1")
        manager.save_session(s1)

        # Wait a tiny moment so the second session gets a different timestamp-based ID
        time.sleep(0.01)

        s2 = manager.create_session(model="test")
        s2.add_message("user", "session 2")
        manager.save_session(s2)

        sessions = manager.list_sessions()
        assert len(sessions) >= 2


# ============================================================================
# Memory Tools
# ============================================================================


class TestMemoryTools:
    """Tests for memory tools."""

    def test_save_and_recall(self, temp_dir):
        from coderAI.tools.memory import SaveMemoryTool, RecallMemoryTool, MemoryStore

        store = MemoryStore()
        store.memory_file = Path(temp_dir) / "memory.json"

        save_tool = SaveMemoryTool()
        recall_tool = RecallMemoryTool()

        import coderAI.tools.memory as mem_module
        original_store = mem_module._memory_store
        mem_module._memory_store = store

        try:
            result = asyncio.run(save_tool.execute(key="test_key", value="test_value"))
            assert result["success"] is True

            result = asyncio.run(recall_tool.execute(key="test_key"))
            assert result["success"] is True
            assert result["value"] == "test_value"

            result = asyncio.run(recall_tool.execute(key="nonexistent"))
            assert result["success"] is False
        finally:
            mem_module._memory_store = original_store


# ============================================================================
# System Prompt
# ============================================================================


class TestSystemPrompt:
    """Tests for the system prompt."""

    @staticmethod
    def _full_tool_registry():
        """Registry with every discoverable tool plus ``manage_context``."""
        from coderAI.context import ContextManager
        from coderAI.tools.base import ToolRegistry
        from coderAI.tools.context_manage import ManageContextTool
        from coderAI.tools.discovery import discover_tools

        reg = ToolRegistry()
        discover_tools(reg)
        reg.register(ManageContextTool(ContextManager()))
        return reg

    def test_system_prompt_exists(self):
        from coderAI.system_prompt import compose_default_system_prompt
        from coderAI.tools import ToolRegistry
        from coderAI.tools.discovery import discover_tools

        reg = ToolRegistry()
        discover_tools(reg)
        text = compose_default_system_prompt(reg)

        assert isinstance(text, str)
        assert len(text) > 100

    def test_system_prompt_mentions_tools(self):
        from coderAI.system_prompt import compose_default_system_prompt

        text = compose_default_system_prompt(self._full_tool_registry())
        assert "read_file" in text
        assert "write_file" in text
        assert "run_command" in text
        assert "git_status" in text
        assert "text_search" in text
        assert "web_search" in text
        assert "delegate_task" in text
        assert "lint" in text
        assert "read_image" in text
        assert "manage_tasks" in text

    def test_system_prompt_has_agentic_guidance(self):
        from coderAI.system_prompt import compose_default_system_prompt

        text = compose_default_system_prompt(self._full_tool_registry())

        # Agentic reasoning / planning keywords (static narrative tail)
        assert "step-by-step" in text.lower()
        assert "Search before" in text or "search before" in text.lower()
        assert "Verify after" in text or "verify after" in text.lower()
        assert "delegate" in text.lower()


# ============================================================================
# Tool Registry
# ============================================================================


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_and_get(self):
        from coderAI.tools.base import ToolRegistry
        from coderAI.tools.filesystem import ReadFileTool

        registry = ToolRegistry()
        tool = ReadFileTool()
        registry.register(tool)

        assert registry.get("read_file") is tool
        assert registry.get("nonexistent") is None

    def test_get_schemas(self):
        from coderAI.tools.base import ToolRegistry
        from coderAI.tools.filesystem import ReadFileTool

        registry = ToolRegistry()
        registry.register(ReadFileTool())

        schemas = registry.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "read_file"

    def test_execute_tool(self, sample_file):
        from coderAI.tools.base import ToolRegistry
        from coderAI.tools.filesystem import ReadFileTool

        registry = ToolRegistry()
        registry.register(ReadFileTool())

        result = asyncio.run(registry.execute("read_file", path=sample_file))
        assert result["success"] is True


# ============================================================================
# OpenAI Provider
# ============================================================================


class TestOpenAIProvider:
    """Tests for OpenAI provider (no API calls, just configuration)."""

    def test_model_mapping(self):
        from coderAI.llm.openai import OpenAIProvider

        provider = OpenAIProvider(model="gpt-5.4", api_key="test-key")
        assert provider.actual_model == "gpt-5.4"

        provider = OpenAIProvider(model="gpt-5.4-mini", api_key="test-key")
        assert provider.actual_model == "gpt-5.4-mini"

    def test_unknown_model_passthrough(self):
        from coderAI.llm.openai import OpenAIProvider

        provider = OpenAIProvider(model="custom-model", api_key="test-key")
        assert provider.actual_model == "custom-model"

    def test_token_counting(self):
        from coderAI.llm.openai import OpenAIProvider

        provider = OpenAIProvider(model="gpt-5.4", api_key="test-key")
        count = provider.count_tokens("Hello, how are you doing today?")
        assert count > 0
        assert isinstance(count, int)

    def test_supported_models_are_real(self):
        from coderAI.llm.openai import OpenAIProvider

        assert "gpt-5.4-mini" in OpenAIProvider.SUPPORTED_MODELS
        assert "gpt-5.4-nano" in OpenAIProvider.SUPPORTED_MODELS
        assert "gpt-4" not in OpenAIProvider.SUPPORTED_MODELS
        assert "gpt-4-turbo" not in OpenAIProvider.SUPPORTED_MODELS
        assert "gpt-3.5-turbo" not in OpenAIProvider.SUPPORTED_MODELS


# ============================================================================
# DeepSeek Provider
# ============================================================================


class TestDeepSeekProvider:
    """Tests for DeepSeek provider configuration and model aliases."""

    def test_model_mapping(self):
        from coderAI.llm.deepseek import DeepSeekProvider

        provider = DeepSeekProvider(model="deepseek-v4-flash", api_key="test-key")
        assert provider.actual_model == "deepseek-v4-flash"

        provider = DeepSeekProvider(model="deepseek-v4-pro", api_key="test-key")
        assert provider.actual_model == "deepseek-v4-pro"

        provider = DeepSeekProvider(model="deepseek-v3.2", api_key="test-key")
        assert provider.actual_model == "deepseek-chat-v3.2"

    def test_v4_requests_disable_thinking_by_default(self):
        from coderAI.llm.deepseek import DeepSeekProvider

        provider = DeepSeekProvider(model="deepseek-v4-pro", api_key="test-key")
        params = provider._build_request_params(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "ping"}}],
        )

        assert params["model"] == "deepseek-v4-pro"
        assert params["extra_body"] == {"thinking": {"type": "disabled"}}
        assert params["tool_choice"] == "auto"

    def test_supported_models_include_v4(self):
        from coderAI.llm.deepseek import DeepSeekProvider

        assert "deepseek-v4-flash" in DeepSeekProvider.SUPPORTED_MODELS
        assert "deepseek-v4-pro" in DeepSeekProvider.SUPPORTED_MODELS

    def test_v4_cost_pricing_is_registered(self):
        from coderAI.cost import CostTracker

        assert CostTracker.get_model_pricing("deepseek-v4-flash") == {
            "input": 0.14,
            "output": 0.28,
        }
        assert CostTracker.get_model_pricing("deepseek-v4-pro") == {
            "input": 1.74,
            "output": 3.48,
        }


# ============================================================================
# LM Studio Provider
# ============================================================================


class TestLMStudioProvider:
    """Tests for LM Studio provider (no actual connection required)."""

    def test_initialization(self):
        from coderAI.llm.lmstudio import LMStudioProvider

        provider = LMStudioProvider(model="test-model", endpoint="http://localhost:1234/v1")
        assert provider.model == "test-model"
        assert provider.endpoint == "http://localhost:1234/v1"

    def test_token_counting(self):
        from coderAI.llm.lmstudio import LMStudioProvider

        provider = LMStudioProvider()
        count = provider.count_tokens("Hello world, this is a test.")
        assert count > 0
        assert isinstance(count, int)

    def test_supports_tools(self):
        from coderAI.llm.lmstudio import LMStudioProvider

        provider = LMStudioProvider()
        assert provider.supports_tools() is True


# ============================================================================
# File Size Limits & Path Protection
# ============================================================================


class TestFileSizeLimits:
    """Tests for file size limits in ReadFileTool."""

    def test_read_large_file_rejected(self, temp_dir):
        from coderAI.tools.filesystem import ReadFileTool, DEFAULT_MAX_FILE_SIZE

        tool = ReadFileTool()
        filepath = os.path.join(temp_dir, "big_file.txt")
        # Create a file slightly larger than the limit
        with open(filepath, "w") as f:
            f.write("x" * (DEFAULT_MAX_FILE_SIZE + 1))

        result = asyncio.run(tool.execute(path=filepath))
        assert result["success"] is False
        assert "too large" in result["error"].lower() or "File too large" in result["error"]
        assert "hint" in result


class TestPathProtection:
    """Tests for path protection in write tools."""

    def test_write_to_protected_path_blocked(self):
        from coderAI.tools.filesystem import WriteFileTool

        tool = WriteFileTool()
        # Attempt to write to ~/.ssh (protected)
        result = asyncio.run(tool.execute(
            path=os.path.expanduser("~/.ssh/injected_key"),
            content="malicious"
        ))
        assert result["success"] is False
        assert "protected" in result["error"].lower()

    def test_search_replace_on_protected_path_blocked(self):
        from coderAI.tools.filesystem import SearchReplaceTool

        tool = SearchReplaceTool()
        result = asyncio.run(tool.execute(
            path=os.path.expanduser("~/.ssh/config"),
            search="Host",
            replace="Evil",
        ))
        assert result["success"] is False
        assert "protected" in result["error"].lower()


class TestGlobLimits:
    """Tests for glob result limits."""

    def test_glob_returns_limited_results(self, temp_dir):
        from coderAI.tools.filesystem import GlobSearchTool

        tool = GlobSearchTool()
        # Create a few test files
        for i in range(5):
            with open(os.path.join(temp_dir, f"file_{i}.txt"), "w") as f:
                f.write(f"file {i}")

        result = asyncio.run(tool.execute(pattern="**/*.txt", base_path=temp_dir))
        assert result["success"] is True
        assert result["count"] <= 200  # MAX_GLOB_RESULTS


# ============================================================================
# Dangerous Command Confirmation
# ============================================================================


class TestDangerousCommands:
    """Tests for dangerous command confirmation."""

    def test_dangerous_command_executes_when_confirmed(self):
        """Dangerous commands should execute (confirmation is handled by ToolRegistry, not execute)."""
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        # rm on a non-existent file will fail with returncode != 0, but the
        # tool should let it through (not hard-reject it)
        result = asyncio.run(tool.execute(command="rm some_file.txt"))
        assert "blocked" not in result
        assert "dangerous" not in result

    def test_blocked_command_rejected(self):
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command="rm -rf /"))
        assert result["success"] is False
        assert result.get("blocked") is True


# ============================================================================
# Undo / Rollback
# ============================================================================


class TestUndoTools:
    """Tests for undo/rollback functionality."""

    def test_backup_and_undo(self, temp_dir):
        from coderAI.tools.undo import FileBackupStore

        store = FileBackupStore(backup_dir=os.path.join(temp_dir, "backups"))

        # Create a file
        filepath = os.path.join(temp_dir, "original.txt")
        with open(filepath, "w") as f:
            f.write("original content")

        # Backup
        store.backup_file(filepath, operation="modify")

        # Modify the file
        with open(filepath, "w") as f:
            f.write("modified content")

        # Verify modified
        with open(filepath) as f:
            assert f.read() == "modified content"

        # Undo
        result = store.undo_last()
        assert result["success"] is True
        assert result["action"] == "restored"

        # Verify restored
        with open(filepath) as f:
            assert f.read() == "original content"

    def test_undo_empty_history(self, temp_dir):
        from coderAI.tools.undo import FileBackupStore

        store = FileBackupStore(backup_dir=os.path.join(temp_dir, "backups"))
        result = store.undo_last()
        assert result["success"] is False

    def test_undo_history(self, temp_dir):
        from coderAI.tools.undo import FileBackupStore

        store = FileBackupStore(backup_dir=os.path.join(temp_dir, "backups"))
        filepath = os.path.join(temp_dir, "test.txt")
        with open(filepath, "w") as f:
            f.write("test")

        store.backup_file(filepath, operation="modify")
        store.backup_file(filepath, operation="modify")

        history = store.get_history(limit=10)
        assert len(history) == 2


# ============================================================================
# Project Context
# ============================================================================


class TestProjectContextTool:
    """Tests for project context auto-detection."""

    def test_detect_python_project(self, temp_dir):
        from coderAI.tools.project import ProjectContextTool

        tool = ProjectContextTool()
        # Create a Python project indicator
        with open(os.path.join(temp_dir, "requirements.txt"), "w") as f:
            f.write("flask==2.0\nrequests\n")

        result = asyncio.run(tool.execute(path=temp_dir))
        assert result["success"] is True
        assert "python" in result["detected_types"]

    def test_detect_node_project(self, temp_dir):
        from coderAI.tools.project import ProjectContextTool

        tool = ProjectContextTool()
        with open(os.path.join(temp_dir, "package.json"), "w") as f:
            json.dump({"name": "test-app", "version": "1.0.0", "dependencies": {"express": "^4.0.0"}}, f)

        result = asyncio.run(tool.execute(path=temp_dir))
        assert result["success"] is True
        assert "node" in result["detected_types"]
        assert "express" in result["context"]["node"]["dependencies"]

    def test_directory_structure(self, temp_dir):
        from coderAI.tools.project import ProjectContextTool

        tool = ProjectContextTool()
        os.makedirs(os.path.join(temp_dir, "src"), exist_ok=True)
        with open(os.path.join(temp_dir, "src", "main.py"), "w") as f:
            f.write("print('hello')")

        result = asyncio.run(tool.execute(path=temp_dir))
        assert result["success"] is True
        assert any("src/" in entry for entry in result["directory_structure"])


# ============================================================================
# Anthropic Provider
# ============================================================================


class TestAnthropicProvider:
    """Tests for Anthropic provider (no API calls)."""

    def test_model_aliases(self):
        from coderAI.llm.anthropic import MODEL_ALIASES

        assert "claude-4-sonnet" in MODEL_ALIASES
        assert "claude-3.5-sonnet" in MODEL_ALIASES
        assert "claude-3-opus" in MODEL_ALIASES

    def test_initialization(self):
        from coderAI.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(model="claude-4-sonnet", api_key="test-key")
        assert provider.supports_tools() is True

    def test_token_counting(self):
        from coderAI.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(model="claude-3.5-sonnet", api_key="test-key")
        count = provider.count_tokens("Hello, how are you?")
        assert count > 0
        assert isinstance(count, int)

    def test_thinking_payload_uses_budget_tokens_format(self):
        from coderAI.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(model="claude-4-sonnet", api_key="test-key", reasoning_effort="medium")
        payload = provider._build_payload(messages=[{"role": "user", "content": "hi"}], tools=None)
        thinking = payload["thinking"]
        assert thinking["type"] == "enabled"
        assert isinstance(thinking["budget_tokens"], int)
        assert thinking["budget_tokens"] > 0
        assert thinking["budget_tokens"] < payload["max_tokens"]
        assert "output_config" not in payload

    def test_thinking_payload_disabled_when_effort_none(self):
        from coderAI.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(model="claude-4-sonnet", api_key="test-key", reasoning_effort="none")
        payload = provider._build_payload(messages=[{"role": "user", "content": "hi"}], tools=None)
        assert "thinking" not in payload
        assert "output_config" not in payload


# ============================================================================
# MCP Tools
# ============================================================================


class TestMCPTools:
    """Tests for MCP tool definitions."""

    def test_mcp_connect_parameters(self):
        from coderAI.tools.mcp import MCPConnectTool

        tool = MCPConnectTool()
        params = tool.get_parameters()
        assert "server_name" in params["properties"]
        assert "command" in params["properties"]

    def test_mcp_list(self):
        from coderAI.tools.mcp import MCPListTool

        tool = MCPListTool()
        result = asyncio.run(tool.execute())
        assert result["success"] is True
        assert result["connected_servers"] == 0

    def test_mcp_call_tool_not_connected(self):
        from coderAI.tools.mcp import MCPCallTool

        tool = MCPCallTool()
        result = asyncio.run(tool.execute(server_name="nonexistent", tool_name="test"))
        assert result["success"] is False
        assert "not connected" in result["error"].lower()


# ============================================================================
# Updated System Prompt
# ============================================================================


class TestUpdatedSystemPrompt:
    """Tests that composed prompt mentions tools registered in the full registry."""

    def test_system_prompt_mentions_new_tools(self):
        from coderAI.system_prompt import compose_default_system_prompt

        text = compose_default_system_prompt(TestSystemPrompt._full_tool_registry())
        assert "mcp_connect" in text
        assert "undo" in text
        assert "project_context" in text


# ============================================================================
# Config with Anthropic
# ============================================================================


class TestConfigAnthropicKey:
    """Tests for Anthropic API key in config."""

    def test_config_has_anthropic_key(self):
        from coderAI.config import Config

        config = Config(anthropic_api_key="sk-ant-test1234567890abcdef")
        assert config.anthropic_api_key == "sk-ant-test1234567890abcdef"

    def test_anthropic_key_masked_in_show(self):
        from coderAI.config import Config, ConfigManager

        manager = ConfigManager()
        manager._config = Config(anthropic_api_key="sk-ant-test1234567890abcdef1234567890")
        shown = manager.show()
        assert "***" in shown["anthropic_api_key"]
        assert shown["anthropic_api_key"] != "sk-ant-test1234567890abcdef1234567890"


# ============================================================================
# Create Folder on Desktop & Build Login Page
# ============================================================================

# The login page HTML that CoderAI tools will write
LOGIN_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login Page</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh;
        }
        .login-container {
            background: rgba(255, 255, 255, 0.95);
            padding: 2.5rem; border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            width: 100%; max-width: 400px;
        }
        .login-container h1 {
            text-align: center; margin-bottom: 1.5rem;
            color: #333; font-size: 1.8rem;
        }
        .form-group { margin-bottom: 1.2rem; }
        .form-group label {
            display: block; margin-bottom: 0.4rem;
            color: #555; font-weight: 600;
        }
        .form-group input {
            width: 100%; padding: 0.75rem 1rem;
            border: 2px solid #ddd; border-radius: 8px;
            font-size: 1rem; transition: border-color 0.3s;
        }
        .form-group input:focus {
            outline: none; border-color: #667eea;
        }
        .login-btn {
            width: 100%; padding: 0.85rem;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white; border: none; border-radius: 8px;
            font-size: 1.1rem; font-weight: 600;
            cursor: pointer; transition: transform 0.2s, box-shadow 0.2s;
        }
        .login-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.5);
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Welcome Back</h1>
        <form id="loginForm">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username"
                       placeholder="Enter your username" required>
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password"
                       placeholder="Enter your password" required>
            </div>
            <button type="submit" class="login-btn">Log In</button>
        </form>
    </div>
    <script>
        document.getElementById('loginForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const username = document.getElementById('username').value;
            alert('Login submitted for: ' + username);
        });
    </script>
</body>
</html>
"""

PROJECT_DIR = None  # Set dynamically in fixtures


class TestCreateFolderAndLoginPage:
    """End-to-end test: create a folder and build a login page."""

    @pytest.fixture(autouse=True, scope="class")
    def setup_project_dir(self):
        """Create a temporary project directory for the test class."""
        import shutil
        import tempfile

        global PROJECT_DIR
        PROJECT_DIR = tempfile.mkdtemp(prefix="coderAI_test_")
        yield
        # Teardown
        if os.path.exists(PROJECT_DIR):
            shutil.rmtree(PROJECT_DIR)
        PROJECT_DIR = None

    # -- Step 1: Create the folder on Desktop ----------------------------------

    def test_step1_create_folder_on_desktop(self):
        """Use RunCommandTool to create a project folder."""
        from coderAI.tools.terminal import RunCommandTool

        tool = RunCommandTool()
        result = asyncio.run(tool.execute(command=f"mkdir -p {PROJECT_DIR}"))
        assert result["success"] is True, f"mkdir failed: {result}"
        assert os.path.isdir(PROJECT_DIR), "Folder was not created"

    # -- Step 2: Write the login page HTML file --------------------------------

    def test_step2_write_login_page(self):
        """Use WriteFileTool to create index.html with a full login page."""
        from coderAI.tools.filesystem import WriteFileTool

        # Ensure folder exists (in case tests run individually)
        os.makedirs(PROJECT_DIR, exist_ok=True)

        tool = WriteFileTool()
        filepath = os.path.join(PROJECT_DIR, "index.html")
        result = asyncio.run(tool.execute(path=filepath, content=LOGIN_PAGE_HTML))
        assert result["success"] is True, f"write_file failed: {result}"
        assert result["bytes_written"] > 0

    # -- Step 3: Verify the folder contents ------------------------------------

    def test_step3_verify_folder_contents(self):
        """Use ListDirectoryTool to confirm index.html is inside the folder."""
        from coderAI.tools.filesystem import ListDirectoryTool

        # Ensure file exists (in case tests run individually)
        os.makedirs(PROJECT_DIR, exist_ok=True)
        index_path = os.path.join(PROJECT_DIR, "index.html")
        if not os.path.exists(index_path):
            with open(index_path, "w") as f:
                f.write(LOGIN_PAGE_HTML)

        tool = ListDirectoryTool()
        result = asyncio.run(tool.execute(path=PROJECT_DIR))
        assert result["success"] is True, f"list_directory failed: {result}"
        names = [e["name"] for e in result["entries"]]
        assert "index.html" in names, f"index.html not found; got {names}"

    # -- Step 4: Read the login page and validate HTML content -----------------

    def test_step4_read_and_validate_login_page(self):
        """Use ReadFileTool to read the login page and check key HTML elements."""
        from coderAI.tools.filesystem import ReadFileTool

        # Ensure file exists (in case tests run individually)
        os.makedirs(PROJECT_DIR, exist_ok=True)
        index_path = os.path.join(PROJECT_DIR, "index.html")
        if not os.path.exists(index_path):
            with open(index_path, "w") as f:
                f.write(LOGIN_PAGE_HTML)

        tool = ReadFileTool()
        result = asyncio.run(tool.execute(path=index_path))
        assert result["success"] is True, f"read_file failed: {result}"

        content = result["content"]

        # Validate essential HTML structure
        assert "<!DOCTYPE html>" in content
        assert "<title>Login Page</title>" in content

        # Validate form elements
        assert '<form id="loginForm">' in content
        assert 'id="username"' in content
        assert 'type="password"' in content
        assert 'class="login-btn"' in content
        assert "Log In" in content

        # Validate CSS is present
        assert "linear-gradient" in content
        assert ".login-container" in content

        # Validate JavaScript is present
        assert "addEventListener" in content


# ============================================================================
# Execution Loop Recovery
# ============================================================================


class TestExecutionLoopRecovery:
    """Tests for tool-call transcript recovery in the main loop."""

    def test_repairs_unpaired_assistant_tool_calls(self):
        from types import SimpleNamespace

        from coderAI.agent_loop import ExecutionLoop
        from coderAI.history import Session

        session = Session(session_id="session_1234567890_deadbeef")
        session.add_message(
            "assistant",
            "I will run a tool",
            tool_calls=[
                {
                    "id": "call_missing_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        )

        agent = SimpleNamespace(session=session, hooks_manager=None)
        loop = ExecutionLoop(agent)
        loop._repair_unpaired_tool_calls()

        msgs = session.get_messages_for_api()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "call_missing_1"
        assert "internal error" in msgs[1]["content"].lower()

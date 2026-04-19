"""Tests for ApplyDiffTool."""

import asyncio
import pytest

from coderAI.tools.filesystem import ApplyDiffTool


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "code.py"
    f.write_text(
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
    )
    return f


class TestApplyDiffTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ApplyDiffTool()

    def test_apply_simple_hunk(self, sample_file):
        diff = (
            "@@ -1,2 +1,2 @@\n"
            "-def foo():\n"
            "-    return 1\n"
            "+def foo():\n"
            "+    return 42\n"
        )
        result = asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert result["success"]
        assert result["hunks_applied"] == 1
        content = sample_file.read_text()
        assert "return 42" in content

    def test_strips_markdown_code_block(self, sample_file):
        diff = (
            "```diff\n"
            "@@ -1,2 +1,2 @@\n"
            "-def foo():\n"
            "-    return 1\n"
            "+def foo():\n"
            "+    return 99\n"
            "```"
        )
        result = asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert result["success"]
        assert "return 99" in sample_file.read_text()

    def test_file_not_found(self, tmp_path):
        result = asyncio.run(
            self.tool.execute(path=str(tmp_path / "nope.py"), diff="@@ -1,1 +1,1 @@\n-old\n+new\n")
        )
        assert not result["success"]
        assert "not found" in result["error"].lower()

    def test_invalid_diff_no_hunks(self, sample_file):
        result = asyncio.run(
            self.tool.execute(path=str(sample_file), diff="not a valid diff at all")
        )
        assert not result["success"]
        assert "hunk" in result["error"].lower()

    def test_hunk_mismatch_returns_error(self, sample_file):
        diff = (
            "@@ -1,2 +1,2 @@\n"
            "-def totally_wrong_name():\n"
            "-    return 999\n"
            "+def totally_wrong_name():\n"
            "+    return 0\n"
        )
        result = asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert not result["success"]
        assert "hunk" in result["error"].lower() or "match" in result["error"].lower()

    def test_reports_line_counts(self, sample_file):
        diff = (
            "@@ -1,2 +1,3 @@\n"
            "-def foo():\n"
            "-    return 1\n"
            "+def foo():\n"
            "+    x = 10\n"
            "+    return x\n"
        )
        result = asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert result["success"]
        assert result["lines_after"] > result["lines_before"]

    def test_pure_insertion(self, sample_file):
        """A hunk with no removed lines should insert at the target position."""
        diff = (
            "@@ -1,0 +1,1 @@\n"
            "+# inserted comment\n"
        )
        result = asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert result["success"]
        assert "# inserted comment" in sample_file.read_text()

    def test_creates_backup(self, sample_file, monkeypatch):
        backups = []
        from coderAI.tools import filesystem as fs_mod
        original_backup = fs_mod.backup_store.backup_file
        monkeypatch.setattr(
            fs_mod.backup_store,
            "backup_file",
            lambda path, op="modify": backups.append(path) or original_backup(path, op),
        )
        diff = (
            "@@ -1,2 +1,2 @@\n"
            "-def foo():\n"
            "-    return 1\n"
            "+def foo():\n"
            "+    return 7\n"
        )
        asyncio.run(self.tool.execute(path=str(sample_file), diff=diff))
        assert len(backups) >= 1

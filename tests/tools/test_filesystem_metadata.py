"""Behavioral coverage for the filesystem metadata tools (finding 12).

file_stat / file_chmod / file_readlink had zero tests. These
exercise the happy paths plus the security guards added in the remediation:
symlink-leaf refusal (TOCTOU), protected-path refusal, and the broken-symlink
readlink fix.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from coderAI.tools.filesystem.metadata import (
    FileChmodTool,
    FileReadlinkTool,
    FileStatTool,
)


def _run(coro):
    return asyncio.run(coro)


class TestFileStat:
    def test_stat_regular_file(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("hello")
        result = _run(FileStatTool().execute(path=str(f)))
        assert result["success"] is True
        assert result["size"] == 5
        assert result["is_file"] is True
        assert result["is_dir"] is False
        assert result["is_symlink"] is False

    def test_stat_missing_path(self, tmp_path):
        result = _run(FileStatTool().execute(path=str(tmp_path / "nope")))
        assert result["success"] is False
        assert "does not exist" in result["error"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
    def test_stat_reports_symlink(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = _run(FileStatTool().execute(path=str(link)))
        assert result["success"] is True
        assert result["is_symlink"] is True


class TestFileReadlink:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
    def test_reads_symlink_target(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = _run(FileReadlinkTool().execute(path=str(link)))
        assert result["success"] is True
        assert result["target"] == str(target)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
    def test_reads_broken_symlink(self, tmp_path):
        # A broken symlink has exists()==False but is still a valid readlink
        # target — the tool must not misreport it as "Path does not exist".
        missing = tmp_path / "gone.txt"
        link = tmp_path / "dangling.txt"
        link.symlink_to(missing)
        result = _run(FileReadlinkTool().execute(path=str(link)))
        assert result["success"] is True, result
        assert result["target"] == str(missing)

    def test_rejects_non_symlink(self, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_text("x")
        result = _run(FileReadlinkTool().execute(path=str(f)))
        assert result["success"] is False
        assert "Not a symlink" in result["error"]

    def test_rejects_missing_path(self, tmp_path):
        result = _run(FileReadlinkTool().execute(path=str(tmp_path / "nope")))
        assert result["success"] is False
        assert "does not exist" in result["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
class TestFileChmod:
    def test_chmod_regular_file(self, tmp_path):
        f = tmp_path / "s.sh"
        f.write_text("echo hi")
        result = _run(FileChmodTool().execute(path=str(f), mode="600"))
        assert result["success"] is True
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_rejects_symlink_leaf(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = _run(FileChmodTool().execute(path=str(link), mode="600"))
        assert result["success"] is False
        assert result.get("error_code") == "symlink", result

    def test_rejects_protected_path(self, tmp_path, monkeypatch):
        # Monkeypatch home so tmp_path/.ssh is treated as the protected store.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_rsa"
        key.write_text("secret")
        result = _run(FileChmodTool().execute(path=str(key), mode="600"))
        assert result["success"] is False
        assert "protected" in result["error"].lower()

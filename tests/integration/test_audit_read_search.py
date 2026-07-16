"""Focused regressions for bounded filesystem and search traversal."""

import asyncio
import io
from pathlib import Path

from coderAI.tools.filesystem import manage, read_write
from coderAI.tools.filesystem.manage import GlobSearchTool
from coderAI.tools.filesystem.read_write import ReadFileTool
from coderAI.tools.search import GrepTool, SymbolSearchTool


class _BoundedReader(io.StringIO):
    def __init__(self, value: str, max_reads: int):
        super().__init__(value)
        self.max_reads = max_reads
        self.reads = 0

    def __next__(self):
        self.reads += 1
        if self.reads > self.max_reads:
            raise AssertionError("read past requested line range")
        return super().__next__()

    def readlines(self, *args, **kwargs):
        raise AssertionError("partial reads must not materialize all lines")


def test_oversized_file_allows_bounded_streaming_read(tmp_path, monkeypatch):
    target = tmp_path / "large.txt"
    content = "".join(f"line {number}\n" for number in range(1, 11))
    target.write_text(content, encoding="utf-8")
    reader = _BoundedReader(content, max_reads=3)
    monkeypatch.setattr(read_write, "_get_max_file_size", lambda: 1)
    monkeypatch.setattr(read_write, "_safe_open_no_symlink", lambda *_args: reader)

    result = asyncio.run(ReadFileTool().execute(str(target), start_line=2, end_line=3))

    assert result["success"] is True
    assert result["content"] == "line 2\nline 3\n"
    assert reader.reads == 3


def test_oversized_file_rejects_unbounded_start_only_range(tmp_path, monkeypatch):
    target = tmp_path / "large.txt"
    target.write_text("line\n" * 10, encoding="utf-8")
    monkeypatch.setattr(read_write, "_get_max_file_size", lambda: 1)

    result = asyncio.run(ReadFileTool().execute(str(target), start_line=2))

    assert result["success"] is False
    assert result["error_code"] == "too_large"


def test_read_range_rejects_invalid_bounds(tmp_path):
    target = tmp_path / "small.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")

    below_one = asyncio.run(ReadFileTool().execute(str(target), start_line=0, end_line=1))
    reversed_range = asyncio.run(ReadFileTool().execute(str(target), start_line=2, end_line=1))

    assert below_one["error_code"] == "validation_error"
    assert reversed_range["error_code"] == "validation_error"


def test_glob_stops_after_one_extra_match(tmp_path, monkeypatch):
    paths = []
    for number in range(3):
        path = tmp_path / f"{number}.py"
        path.write_text("", encoding="utf-8")
        paths.append(path)

    def bounded_glob(_self, _pattern):
        yield from paths
        raise AssertionError("glob traversal continued after truncation was known")

    monkeypatch.setattr(manage, "_get_max_glob_results", lambda: 2)
    monkeypatch.setattr(Path, "glob", bounded_glob)

    result = asyncio.run(GlobSearchTool().execute("*.py", str(tmp_path)))

    assert result["matches"] == ["0.py", "1.py"]
    assert result["was_truncated"] is True


def test_symbol_search_stops_after_one_extra_match(tmp_path, monkeypatch):
    paths = []
    for number in range(3):
        path = tmp_path / f"{number}.py"
        path.write_text("def target():\n    pass\n", encoding="utf-8")
        paths.append(path)

    def bounded_rglob(_self, _pattern):
        yield from paths
        raise AssertionError("symbol traversal continued after truncation was known")

    monkeypatch.setattr(Path, "rglob", bounded_rglob)

    result = asyncio.run(
        SymbolSearchTool().execute("target", kind="function", path=str(tmp_path), max_results=2)
    )

    assert result["count"] == 2
    assert result["was_truncated"] is True


def test_python_grep_uses_one_extra_match_to_detect_truncation(tmp_path, monkeypatch):
    target = tmp_path / "matches.txt"
    target.write_text("needle\nneedle\n", encoding="utf-8")
    monkeypatch.setattr("coderAI.tools.search.shutil.which", lambda _name: None)

    result = asyncio.run(GrepTool().execute("needle", str(target), max_results=1))

    assert result["count"] == 1
    assert result["was_truncated"] is True
    assert result["next_offset"] == 1


def test_python_grep_exact_cap_is_not_reported_truncated(tmp_path, monkeypatch):
    target = tmp_path / "match.txt"
    target.write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr("coderAI.tools.search.shutil.which", lambda _name: None)

    result = asyncio.run(GrepTool().execute("needle", str(target), max_results=1))

    assert result["count"] == 1
    assert result["was_truncated"] is False

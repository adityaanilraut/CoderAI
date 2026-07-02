"""Approval-diff preview (Phase 4.3).

The editing semantics now live on each Tool via ``Tool.preview`` — the executor
only resolves the tool, reads the current file (mtime-cached), renders the
unified diff, and truncates. These tests exercise that plumbing against the
*real* filesystem tools (a MagicMock tool can't supply real ``preview`` output),
and pin the invariant that the preview equals the content ``execute`` produces.
"""

import asyncio
from types import SimpleNamespace

from coderAI.core.tool_executor import ToolExecutor
from coderAI.tools.base import ToolRegistry
from coderAI.tools.filesystem.edit import ApplyDiffTool, SearchReplaceTool
from coderAI.tools.filesystem.read_write import WriteFileTool
from coderAI.tools.multi_edit import MultiEditTool


def _executor() -> ToolExecutor:
    """Executor over a registry of the real file-editing tools.

    ``config=None`` makes ``_compute_preview_diff`` skip the project-scope check,
    so previews can target ``tmp_path`` files outside the repo root.
    """
    reg = ToolRegistry()
    for tool in (WriteFileTool(), SearchReplaceTool(), ApplyDiffTool(), MultiEditTool()):
        reg.register(tool)
    agent = SimpleNamespace(tools=reg, config=None)
    return ToolExecutor(agent)


def test_compute_preview_diff_write_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello\n", encoding="utf-8")
    executor = _executor()

    # Overwrite
    diff = executor._compute_preview_diff("write_file", {"path": str(f), "content": "world\n"})
    assert "-hello" in diff
    assert "+world" in diff

    # Append
    diff2 = executor._compute_preview_diff(
        "write_file", {"path": str(f), "content": "world\n", "append": True}
    )
    assert " hello" in diff2
    assert "+world" in diff2


def test_compute_preview_diff_search_replace(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello world\n", encoding="utf-8")
    executor = _executor()

    diff = executor._compute_preview_diff(
        "search_replace", {"path": str(f), "search": "world", "replace": "there"}
    )
    assert "-hello world" in diff
    assert "+hello there" in diff


def test_compute_preview_diff_apply_diff(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello\n", encoding="utf-8")
    executor = _executor()

    # apply_diff surfaces the model's own patch verbatim.
    raw_diff = "--- a/test.txt\n+++ b/test.txt\n@@ -1 +1 @@\n-hello\n+world"
    diff = executor._compute_preview_diff("apply_diff", {"path": str(f), "diff": raw_diff})
    assert diff == raw_diff


def test_compute_preview_diff_multi_edit(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    executor = _executor()

    diff = executor._compute_preview_diff(
        "multi_edit",
        {
            "path": str(f),
            "edits": [{"search": "a", "replace": "A"}, {"search": "b", "replace": "B"}],
        },
    )
    assert "-a" in diff
    assert "+A" in diff
    assert "-b" in diff
    assert "+B" in diff


def test_compute_preview_diff_truncation(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("a\n", encoding="utf-8")
    executor = _executor()

    large_content = "X" * 40000
    diff = executor._compute_preview_diff("write_file", {"path": str(f), "content": large_content})

    assert len(diff) <= 32768 + 50  # 32768 + length of truncation message
    assert "(diff truncated)" in diff


def test_compute_preview_diff_missing_file_for_edit_returns_none(tmp_path):
    """search_replace on a nonexistent file can't be previewed → None."""
    executor = _executor()
    missing = tmp_path / "nope.txt"
    diff = executor._compute_preview_diff(
        "search_replace", {"path": str(missing), "search": "x", "replace": "y"}
    )
    assert diff is None


def test_preview_matches_actual_search_replace_first_only(tmp_path):
    """Regression: the preview must equal the content execute() actually writes.

    A file with two matches and ``replace_all=False`` must replace only the
    first — the exact behaviour the approval diff has to reflect (no drift
    between the previewed and applied edit).
    """
    f = tmp_path / "dup.txt"
    original = "foo bar foo baz\n"
    f.write_text(original, encoding="utf-8")

    tool = SearchReplaceTool()
    args = {"path": str(f), "search": "foo", "replace": "QUX", "replace_all": False}

    preview = tool.preview(args, original)
    assert preview is not None
    # First-only replacement: second "foo" is untouched.
    assert preview.new_content == "QUX bar foo baz\n"

    # Execute for real and confirm the file matches what the preview showed.
    asyncio.run(tool.execute(**args))
    assert f.read_text(encoding="utf-8") == preview.new_content

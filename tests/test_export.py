"""Coverage for coderAI/tui/export.py."""

from coderAI.tui.export import timeline_to_markdown


def test_timeline_to_markdown_header_and_empty():
    md = timeline_to_markdown([])
    assert md.startswith("# CoderAI Session")
    assert "Exported:" in md


def test_timeline_to_markdown_renders_each_kind():
    items = [
        {"kind": "user", "text": "hello"},
        {"kind": "assistant", "content": "hi there", "reasoning": "  some thoughts  "},
        {"kind": "tool", "name": "read_file", "ok": True, "preview": "line1\nline2"},
        {"kind": "tool", "name": "run", "ok": False, "error": "boom"},
        {"kind": "tool", "name": "wait", "ok": None},
        {"kind": "diff", "path": "f.py", "diff": "+added"},
        {"kind": "error", "message": "bad thing", "details": "traceback"},
        {"kind": "unknown_kind", "foo": "bar"},  # ignored
    ]
    md = timeline_to_markdown(items)

    assert "**You:**" in md
    assert "hello" in md
    assert "**Assistant:**" in md
    assert "hi there" in md
    # Reasoning collapses into a <details> block with a char count.
    assert "<details><summary>Reasoning" in md
    assert "some thoughts" in md
    # Tool status marks for ok True/False/None.
    assert "`read_file` — ✓" in md
    assert "`run` — ✗" in md
    assert "`wait` — …" in md
    # Tool preview is blockquoted; error line present.
    assert "> line1" in md
    assert "> boom" in md
    # Diff fence.
    assert "```diff" in md
    assert "+added" in md
    # Error with details fence.
    assert "**Error:** bad thing" in md
    assert "traceback" in md


def test_timeline_to_markdown_assistant_without_reasoning():
    md = timeline_to_markdown([{"kind": "assistant", "content": "plain"}])
    assert "plain" in md
    assert "<details>" not in md

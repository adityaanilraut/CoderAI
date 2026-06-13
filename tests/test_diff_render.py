"""Coverage for coderAI/tui/diff_render.py."""

from coderAI.tui import diff_render as dr


def test_parse_unified_diff_classifies_line_types():
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-removed\n+added\n unchanged\nbare"
    parsed = dr.parse_unified_diff(diff)
    kinds = [k for k, _ in parsed]
    assert kinds == ["meta", "meta", "hunk", "del", "add", "ctx", "ctx"]
    # The leading marker is stripped from add/del/ctx text.
    assert ("del", "removed") in parsed
    assert ("add", "added") in parsed
    assert ("ctx", "unchanged") in parsed
    # A context line with no leading space keeps its text.
    assert ("ctx", "bare") in parsed


def test_window_lines_no_truncation():
    parsed = [("ctx", str(i)) for i in range(5)]
    assert dr._window_lines(parsed, 10) == parsed


def test_window_lines_truncates_with_ellipsis():
    parsed = [("ctx", str(i)) for i in range(20)]
    windowed = dr._window_lines(parsed, 6)
    # head(3) + ellipsis(1) + tail(3)
    assert len(windowed) == 7
    assert windowed[3][0] == "ctx"
    assert "lines elided" in windowed[3][1]


def test_format_diff_compact_returns_text_lines():
    diff = "@@ -1 +1 @@\n-a\n+b\n c"
    out = dr.format_diff_compact(diff)
    assert "a" in out and "b" in out and "c" in out


def test_format_diff_gutter_empty_returns_empty_string():
    assert dr.format_diff_gutter("") == ""


def test_format_diff_gutter_renders_all_kinds():
    diff = "--- a\n+++ b\n@@ -1,2 +1,2 @@\n-old\n+new\n keep"
    out = dr.format_diff_gutter(diff)
    # Markup for added/removed/context lines plus meta/hunk headers.
    assert "old" in out and "new" in out and "keep" in out
    assert "+" in out  # add prefix
    assert "−" in out  # minus prefix for deletions


def test_format_diff_gutter_truncates_long_diff():
    diff = "\n".join(f"+line{i}" for i in range(50))
    out = dr.format_diff_gutter(diff, max_lines=6)
    assert "lines elided" in out

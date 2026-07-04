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


def test_format_diff_gutter_uses_solid_hex_backgrounds():
    """Rich markup drops rgba() colors silently — diff line backgrounds must
    be pre-blended hex applied with 'on', or the tint never renders."""
    out = dr.format_diff_gutter("@@ -1 +1 @@\n-old\n+new")
    from coderAI.tui.theme import Styles

    assert f"[on {Styles.DIFF_ADD_BG}]" in out
    assert f"[on {Styles.DIFF_REMOVE_BG}]" in out
    assert "rgba" not in out


def test_word_spans_emphasizes_only_changed_tokens():
    spans = dr._word_spans("return compute(x, old=True)", "return compute(x, new=False)")
    assert spans is not None
    old_spans, new_spans = spans
    old = "return compute(x, old=True)"
    new = "return compute(x, new=False)"
    assert [old[a:b] for a, b in old_spans] == ["old", "True"]
    assert [new[a:b] for a, b in new_spans] == ["new", "False"]


def test_word_spans_rejects_dissimilar_lines():
    assert dr._word_spans("import os", "totally different content here") is None


def test_word_spans_rejects_blank_lines():
    assert dr._word_spans("", "something") is None
    assert dr._word_spans("   ", "something") is None


def test_paired_emphasis_pairs_del_add_runs_index_wise():
    window = [
        ("ctx", "keep"),
        ("del", "alpha beta gamma"),
        ("del", "unpaired removed line"),
        ("add", "alpha BETA gamma"),
    ]
    emph = dr._paired_emphasis(window)
    # First del pairs with the add; the leftover del keeps whole-line styling.
    assert 1 in emph and 3 in emph
    assert 2 not in emph


def test_paired_emphasis_skips_unpaired_add():
    window = [("ctx", "keep"), ("add", "brand new line")]
    assert dr._paired_emphasis(window) == {}


def test_format_diff_gutter_emphasizes_changed_words():
    from coderAI.tui.theme import Styles

    out = dr.format_diff_gutter("@@ -1 +1 @@\n-x = old_value\n+x = new_value")
    assert f"[{Styles.DIFF_REMOVE_EMPH}]old_value[/]" in out
    assert f"[{Styles.DIFF_ADD_EMPH}]new_value[/]" in out
    # The unchanged prefix stays in the base gutter style, unemphasized.
    assert f"[{Styles.GUTTER_ADD}]x = [/]" in out


def test_format_diff_gutter_full_rewrite_keeps_whole_line_style():
    from coderAI.tui.theme import Styles

    out = dr.format_diff_gutter("@@ -1 +1 @@\n-import os\n+totally different content here")
    assert Styles.DIFF_ADD_EMPH not in out
    assert Styles.DIFF_REMOVE_EMPH not in out


def test_emphasized_body_escapes_markup_in_all_segments():
    out = dr._emphasized_body("a [b] c", [(2, 5)], "base", "emph")
    assert "\\[b]" in out

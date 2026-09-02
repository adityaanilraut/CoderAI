"""Tests for Phase 3: Visual Streaming, Theming & Markup Safety."""

from __future__ import annotations

import io

from rich.console import Console

from coderai.cli.diff_render import format_diff_text
from coderai.cli.plan_render import format_plan_content
from coderai.cli.thinking import (
    LiveThinkingStreamer,
    render_thinking_block,
)
from coderai.cli.welcome import render_welcome_screen


# ==========================================
# 1. Tests for Reasoning Markup Escaping & Safety
# ==========================================


def test_render_thinking_block_escapes_brackets():
    """Verify thinking block safely handles bracketed tags without Rich MarkupError."""
    buf = io.StringIO()
    console = Console(file=buf)

    # Thinking text with unclosed or special markdown/bracket tags
    thinking_with_tags = "Planning [Step 1]: Query database for [user_id] and check [bold] tags."

    # Should not raise MarkupError
    render_thinking_block(console, thinking_with_tags, elapsed_seconds=2.5, expanded=False)
    output = buf.getvalue()
    assert "Planning [Step 1]" in output
    assert "2.5s" in output


def test_render_thinking_block_expanded_view():
    """Verify expanded thinking trace renders multiline trace safely."""
    buf = io.StringIO()
    console = Console(file=buf)

    trace = "Line 1: Analyze AST\nLine 2: Identify [call_site]\nLine 3: Plan fix"
    render_thinking_block(console, trace, elapsed_seconds=1.2, expanded=True)
    output = buf.getvalue()
    assert "Reasoning Trace" in output
    assert "Line 2: Identify [call_site]" in output


def test_live_thinking_streamer_finalize_line_clearing():
    """Verify finalize cleanly stops streamer and renders block."""
    buf = io.StringIO()
    console = Console(file=buf)

    streamer = LiveThinkingStreamer(console=console)
    streamer.on_chunk("Thinking about ")
    streamer.on_chunk("the solution...")

    assert streamer.is_active is True
    res = streamer.finalize(console=console, expanded=False)
    assert "Thinking about the solution..." in res
    assert streamer.is_active is False


# ==========================================
# 2. Tests for Theme-Adaptive Renderers
# ==========================================


def test_welcome_screen_adaptive_rendering():
    """Verify welcome screen renders without hardcoded white style crashes on any console."""
    buf = io.StringIO()
    console = Console(file=buf)

    render_welcome_screen(
        console=console,
        project_root="/test/project",
        active_model="gpt-5.6-sol",
        plan_mode=True,
        mcp_servers_count=2,
        skills_count=1,
    )
    output = buf.getvalue()
    assert "CoderAI" in output
    assert "gpt-5.6-sol" in output
    assert "Engine:" in output
    assert "Shortcuts:" in output


def test_diff_render_adaptive_styles():
    """Verify diff context lines format cleanly."""
    diff_sample = (
        "--- a/test.py\n+++ b/test.py\n@@ -1,3 +1,3 @@\n context line\n-old line\n+new line\n"
    )
    text = format_diff_text(diff_sample)
    plain = text.plain
    assert "context line" in plain
    assert "+new line" in plain
    assert "-old line" in plain


def test_plan_render_adaptive_styles():
    """Verify plan items render checkboxes and descriptions cleanly."""
    plan_sample = (
        "# Implementation Plan\n"
        "- [x] Step 1: Initialize database\n"
        "- [>] Step 2: Run migrations\n"
        "- [ ] Step 3: Verify endpoints\n"
        "- Note: Keep backups handy\n"
    )
    text = format_plan_content(plan_sample)
    plain = text.plain
    assert "Step 1: Initialize database" in plain
    assert "Step 2: Run migrations" in plain
    assert "Step 3: Verify endpoints" in plain

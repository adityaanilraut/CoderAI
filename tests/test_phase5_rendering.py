"""Behavioral contracts for streaming and fallback terminal renderers."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console
from rich.theme import Theme

from coderai.soul.session.models import SessionMessage
from coderai.ui.shell.visualize import _blocks as blocks
from coderai.ui.shell.visualize import _markdown_boundary as boundaries
from coderai.ui.shell.visualize import _markdown_stream as fallback
from coderai.utils.rich.markdown import Markdown


@pytest.mark.parametrize("width", [24, 40, 80])
def test_streamed_content_commits_complete_blocks_and_keeps_tail(width, monkeypatch):
    console = Console(file=io.StringIO(), width=width, record=True)
    monkeypatch.setattr(blocks, "console", console)
    block = blocks.ContentBlock(False)
    first = "# Heading\n\nParagraph with **bold** and `inline`.\n\n"
    fence = "```python\nprint('hello')\n\n"
    block.append(first + fence)
    assert block._pending_text() == fence
    block.append("```\n\nTail text\n")
    assert block._pending_text() == "Tail text\n"
    console.print(block.compose_final())
    rendered = console.export_text()
    assert rendered.count("Heading") == 1
    assert rendered.count("hello") == 1
    assert rendered.count("Tail text") == 1


@pytest.mark.parametrize("width", [24, 40, 80])
def test_fallback_renderer_preserves_full_text_and_can_restart(width, monkeypatch):
    monkeypatch.setattr(blocks, "_install_sigwinch", lambda handler: None)
    console = Console(file=io.StringIO(), width=width, record=True)
    renderer = blocks.MarkdownStreamRenderer(console)
    content = "# Heading\n\n```python\nprint('hello')\n```\n\nTail text\n"
    for chunk in (content[:12], content[12:28], content[28:]):
        renderer.on_chunk(chunk)
    assert renderer.finalize() == content
    assert not renderer._is_active
    renderer.on_chunk("Second answer")
    assert renderer.finalize() == "Second answer"
    rendered = console.export_text()
    assert "Heading" in rendered
    assert "hello" in rendered
    assert "Tail text" in rendered
    assert "Second answer" in rendered


def test_tool_cards_escape_untrusted_output_and_render_todos():
    console = Console(file=io.StringIO(), width=60, record=True)
    message = SessionMessage(
        id="tool-result",
        session_id="session",
        role="tool",
        content=json.dumps(
            {
                "name": "bash",
                "ok": True,
                "output": "[red]literal[/red]",
                "metadata": {"command": "echo literal"},
            }
        ),
    )
    blocks.render_tool_card(console, message)
    assert "[red]literal[/red]" in console.export_text()
    blocks.render_todo_list(console, "- [x] Done\n  - [ ] Next\n", title="Tasks")
    rendered = console.export_text()
    assert "Tasks" in rendered and "Done" in rendered and "Next" in rendered
    assert blocks.parse_plan_stats("- [x] Done\n- [ ] Next") == (2, 1)


def test_thinking_fallback_does_not_write_non_tty_frames(capsys):
    console = Console(file=io.StringIO(), width=30, record=True)
    renderer = blocks.LiveThinkingStreamer(console)
    renderer.on_chunk("Reasoning text")
    assert renderer.finalize() == "Reasoning text"
    assert "\r" not in capsys.readouterr().out
    assert not renderer.is_active and not renderer.thinking_chunks


@pytest.mark.parametrize("width", [12, 24, 80])
@pytest.mark.parametrize(
    "content",
    [
        "# Heading\n\n## Subheading\n\n### Detail\n",
        "Inline `value`\n\n```python\nprint('hello')\n```\n",
    ],
)
def test_headings_and_code_have_no_background(width, content):
    console = Console(
        width=width,
        theme=Theme(
            {
                "markdown.h1": "bold white on blue",
                "markdown.h2": "bold white on blue",
                "markdown.h3": "bold white on blue",
                "markdown.code": "yellow on red",
                "markdown.code_block": "white on green",
            }
        ),
    )
    segments = list(console.render(Markdown(content)))
    assert any(segment.text.strip() for segment in segments)
    assert all(segment.style is None or segment.style.bgcolor is None for segment in segments)


@pytest.mark.parametrize(
    "content",
    [
        "A single paragraph\n",
        "```python\nfirst\n\nstill inside fence\n",
        "- first\n  - nested\n- last\n",
        "| First | Second |\n| --- | --- |\n| cell | cell |\n",
    ],
)
def test_last_block_is_never_committed_prematurely(content):
    assert boundaries.find_parser_boundary(content) is None
    assert boundaries.find_committed_boundary(content) == 0


def test_parser_failure_keeps_buffer_uncommitted(monkeypatch):
    class BrokenParser:
        def parse(self, text):
            raise ValueError("invalid parser state")

    monkeypatch.setattr(boundaries, "_get_md_parser", BrokenParser)
    assert boundaries.find_committed_boundary("First\n\nSecond\n") == 0


def test_fallback_without_live_or_markdown_still_returns_and_prints_text(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("rendering unavailable")

    monkeypatch.setattr(fallback, "Live", unavailable)
    monkeypatch.setattr(fallback, "Markdown", unavailable)
    console = Console(file=io.StringIO(), record=True)
    renderer = blocks.MarkdownStreamRenderer(console)
    content = "First paragraph\n\nSecond paragraph\n"
    renderer.on_chunk(content)
    assert renderer.finalize() == content
    rendered = console.export_text()
    assert "First paragraph" in rendered and "Second paragraph" in rendered

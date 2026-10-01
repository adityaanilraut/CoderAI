"""Search output, context, literal markup, and existing terminal link policy."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from coderai.soul.session.models import SessionMessage
from coderai.tools.file.grep import handle_grep_tool
from coderai.tools.legacy.types import ToolExecutionContext
from coderai.ui.shell.visualize._blocks import render_tool_card


def render(result, **metadata):
    console = Console(file=io.StringIO(), width=160, record=True)
    render_tool_card(
        console,
        SessionMessage(
            id="result",
            session_id="search",
            role="tool",
            content=json.dumps(
                {
                    "name": "grep",
                    "ok": result.ok,
                    "output": result.output,
                    "metadata": {**result.metadata, **metadata},
                }
            ),
        ),
    )
    return console.export_text(), console.export_html(clear=False)


@pytest.mark.parametrize("mode", ["content", "files_with_matches", "count_matches"])
def test_search_card_uses_result_count_with_context_and_unusual_paths(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    path = tmp_path / "file with space:colon.txt"
    path.write_text(
        "before [red]literal[/red]\nneedle [link=https://invalid.test]literal[/link]\nafter\n"
    )
    result = handle_grep_tool(
        {"pattern": "needle", "output_mode": mode, **({"context": 1} if mode == "content" else {})},
        ToolExecutionContext("search", str(tmp_path)),
    )
    assert result.ok and result.metadata["count"] == 1
    text, html = render(result, pattern="needle")
    assert "(1 matches)" in text
    assert path.name in text
    assert 'href="https://invalid.test"' not in html
    if mode == "content":
        assert "Line 1- before [red]literal[/red]" in text
        assert "Line 2: needle [link=https://invalid.test]literal[/link]" in text
        assert "Line 3- after" in text


def test_empty_search_keeps_zero_count(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    result = handle_grep_tool({"pattern": "absent"}, ToolExecutionContext("search", str(tmp_path)))
    text, _ = render(result)
    assert "(0 matches)" in text and "No matches found" in text


def test_search_truncation_keeps_total_and_literal_spill_locator(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path / "share"))
    (tmp_path / "long.txt").write_text("needle " + "é" * 2200 + "\nneedle next\n")
    result = handle_grep_tool(
        {"pattern": "needle", "head_limit": 1}, ToolExecutionContext("search", str(tmp_path))
    )
    assert result.metadata["count"] == 2 and result.metadata["truncated"]
    text, html = render(result)
    assert "(2 matches)" in text
    assert "line truncated" in result.output
    assert "Full grep result stored at:" in result.output
    assert "<a href=" not in html


def test_legacy_flat_search_lines_remain_literal():
    from coderai.tools.legacy.types import ToolResult

    output = "file with space:colon.py:42:[red]literal[/red]\nother.py-43-context"
    text, html = render(
        ToolResult(ok=True, name="grep", output=output, metadata={"matches_count": 1})
    )
    assert output.splitlines()[0] in text
    assert output.splitlines()[1] in text
    assert "<a href=" not in html

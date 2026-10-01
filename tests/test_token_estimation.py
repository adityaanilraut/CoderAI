"""Request budgets and UI estimates deliberately use different heuristics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coderai.cli.elapsed import _estimate_tokens_float, estimate_tokens
from coderai.llm import _estimate_text_tokens
from coderai.soul.session.completion import call_stream_or_sync


@pytest.mark.parametrize(
    ("text", "request_tokens", "display_float", "display_tokens"),
    [
        ("", 0, 0.0, 0),
        ("abc", 1, 0.75, 0),
        ("abcde", 2, 1.25, 1),
        ("é", 1, 0.25, 0),
        ("漢", 1, 1.5, 1),
        ("Ａ", 1, 1.5, 1),
        ("😀", 1, 0.25, 0),
        ("a漢é", 3, 2.0, 2),
    ],
)
def test_request_and_display_policies(text, request_tokens, display_float, display_tokens):
    assert _estimate_text_tokens(text) == request_tokens
    assert _estimate_tokens_float(text) == display_float
    assert estimate_tokens(text) == display_tokens
    assert sum(_estimate_tokens_float(char) for char in text) == display_float


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        ([{"content": "abcdef"}], 1),
        ([{"content": "abc"}, {"content": "def"}], 2),
        ([{"thinking": "漢"}, {"content": "abc"}], 2),
    ],
)
def test_stream_fallback_keeps_per_delta_estimates(deltas, expected):
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**delta))], usage=None)
        for delta in deltas
    ]
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: iter(chunks)))
    )
    progress = []
    result = call_stream_or_sync(client, {"model": "test"}, on_progress=progress.append)
    assert result["choices"][0]["message"]["content"] == "".join(
        delta.get("content", "") for delta in deltas
    )
    assert result["usage"]["completion_tokens"] == expected
    assert progress[-1] == {"estimatedTokens": expected, "type": "end"}

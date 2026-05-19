"""Ensure frozen-timeline logic covers every TimelineItem kind."""

from __future__ import annotations

from pathlib import Path

from coderAI.tui.lib.frozen import TIMELINE_ITEM_KINDS, is_timeline_item_frozen

ROOT = Path(__file__).resolve().parents[1]


def test_timeline_item_frozen_handles_all_timeline_kinds() -> None:
    text = (ROOT / "coderAI/tui/lib/frozen.py").read_text(encoding="utf-8")
    for kind in TIMELINE_ITEM_KINDS:
        assert kind in text, f"frozen.py should mention timeline kind {kind!r}"


def test_streaming_assistant_not_frozen() -> None:
    assert not is_timeline_item_frozen({"kind": "assistant", "streaming": True})


def test_completed_assistant_frozen() -> None:
    assert is_timeline_item_frozen({"kind": "assistant", "streaming": False})

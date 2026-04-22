"""Ensure `isTimelineItemFrozen` covers every `TimelineItem` kind (see `ui/src/timelineItemFrozen.ts`)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Must match the `TimelineItem` union in `ui/src/hooks/agentStateTypes.ts`.
_TIMELINE_ITEM_KINDS = frozenset(
    {
        "user",
        "assistant",
        "tool",
        "diff",
        "error",
        "toast",
        "approval",
        "agent",
    }
)


def _case_kinds_from_frozen_module() -> set[str]:
    text = (ROOT / "ui/src/timelineItemFrozen.ts").read_text(encoding="utf-8")
    return set(re.findall(r'case\s+"([a-z_]+)"\s*:', text))


def test_timeline_item_frozen_handles_all_timeline_kinds() -> None:
    cases = _case_kinds_from_frozen_module()
    assert (
        cases == _TIMELINE_ITEM_KINDS
    ), f"isTimelineItemFrozen must handle each TimelineItem kind: missing {_TIMELINE_ITEM_KINDS - cases} extra {cases - _TIMELINE_ITEM_KINDS}"


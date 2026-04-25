"""Ensure `isTimelineItemFrozen` covers every `TimelineItem` kind (see `ui/src/lib/timelineItemFrozen.ts`)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# Must match the `TimelineItem` union in `ui/src/hooks/agentStateTypes.ts`.
# Agents render in the dedicated `AgentTree` panel above the prompt, not in
# the scrolling transcript, so there is no `agent` timeline kind.
_TIMELINE_ITEM_KINDS = frozenset(
    {
        "user",
        "assistant",
        "tool",
        "diff",
        "error",
        "toast",
        "approval",
    }
)


import pytest

def _case_kinds_from_frozen_module() -> set[str]:
    path = ROOT / "ui/src/lib/timelineItemFrozen.ts"
    if not path.exists():
        pytest.skip(f"timelineItemFrozen.ts missing at {path}")
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r'case\s+"([a-z_]+)"\s*:', text))


def test_timeline_item_frozen_handles_all_timeline_kinds() -> None:
    cases = _case_kinds_from_frozen_module()
    assert (
        cases == _TIMELINE_ITEM_KINDS
    ), f"isTimelineItemFrozen must handle each TimelineItem kind: missing {_TIMELINE_ITEM_KINDS - cases} extra {cases - _TIMELINE_ITEM_KINDS}"


"""Pilot coverage for the timeline render path.

Two regressions are guarded here:

* A "full" refresh (triggered on every tool result) must reuse cached rendered
  Strips for unchanged items instead of re-parsing every Markdown bubble. This
  is what stops the chat area from getting slow / dropping keystrokes during
  long multi-round agent runs.
* The live streaming tail overlays the bottom of the timeline (its own layer)
  rather than shrinking it, so the chat area does not visibly jump every turn.
"""

from __future__ import annotations

import pytest

import coderAI.tui.app as appmod
from coderAI.tui.app import CoderAIApp
from coderAI.tui.widgets import SelectableRichLog

# Textual dispatches ``on_mount`` to every handler in the MRO, so the base
# app still schedules its background scan worker even though we stub it below.
# On fast test teardown that coroutine can be GC'd before its worker runs —
# a harmless resource warning we silence here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine '.*_scan_project_files' was never awaited:RuntimeWarning"
)


class _Harness(CoderAIApp):
    """CoderAIApp that stubs the real agent loop and project scan.

    Widgets still come from ``compose`` and ``on_mount`` still runs, so the
    timeline behaves exactly as in production; we just avoid starting a live
    agent session and the test drives ``_refresh_ui`` directly.
    """

    async def _run_agent(self) -> None:  # type: ignore[override]
        return

    async def _scan_project_files(self) -> None:  # type: ignore[override]
        return


async def test_full_refresh_reuses_cached_strips(monkeypatch):
    calls = {"n": 0}
    real = appmod.write_timeline_item

    def counting(log, it, *, verbose):
        calls["n"] += 1
        return real(log, it, verbose=verbose)

    monkeypatch.setattr(appmod, "write_timeline_item", counting)

    # 140 cols: no responsive auto-hide, so the render width (part of the
    # Strip-cache key) cannot shift under the write-count assertions below.
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        log = app.query_one("#timeline", SelectableRichLog)
        assert log._size_known, "RichLog should be sized inside run_test"

        r = app.reducer
        for i in range(5):
            r._push(
                {
                    "kind": "assistant",
                    "id": r.next_id(),
                    "content": f"# heading {i}\n\nbody **bold** number {i}",
                    "reasoning": "",
                    "streaming": False,
                }
            )
        r._push(
            {
                "kind": "tool",
                "id": "tool-1",
                "name": "read_file",
                "category": "fs",
                "args": {"path": "x.py"},
                "risk": "low",
                "ok": None,
                "preview": None,
                "error": None,
            }
        )

        # First full render: nothing is cached yet, so every item renders once.
        app._refresh_ui("full")
        await pilot.pause()
        assert calls["n"] == 6

        # A tool finishing flips one item's state and triggers another "full".
        # Only that item should re-render; the 5 Markdown bubbles are blitted
        # from the Strip cache.
        calls["n"] = 0
        for it in r.timeline:
            if it.get("id") == "tool-1":
                it["ok"] = True
                it["preview"] = "done"
                break
        app._refresh_ui("full")
        await pilot.pause()
        assert calls["n"] == 1

        # The rebuilt log still holds every line (5 bubbles + spacers + tool).
        assert len(log.lines) > 6


async def test_stream_tail_overlay_does_not_resize_timeline():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        timeline = app.query_one("#timeline")
        tail = app.query_one("#stream-tail")

        height_idle = timeline.size.height

        tail.update("\n".join(f"streamed line {i}" for i in range(15)))
        tail.display = True
        await pilot.pause()

        assert timeline.size.height == height_idle


async def test_append_does_not_yank_scrollback():
    """New items must not steal the view while the user reads scrollback.

    RichLog.write() auto-scrolls by default, which bypassed the sticky-follow
    check in _refresh_ui — the timeline is built with auto_scroll=False and
    _refresh_ui pins to the bottom only when the user was already there.

    140 cols keeps both side panes visible (no responsive auto-hide), so the
    timeline width — and therefore scroll offsets — stay stable however late
    the initial Resize event lands.
    """
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        r = app.reducer
        for i in range(60):
            r._push(
                {
                    "kind": "assistant",
                    "id": r.next_id(),
                    "content": f"message {i}",
                    "reasoning": "",
                    "streaming": False,
                }
            )
        app._refresh_ui("full")
        await pilot.pause()
        log = app.query_one("#timeline", SelectableRichLog)
        assert log.is_vertical_scroll_end  # full refresh pins to bottom

        log.scroll_page_up(animate=False)
        await pilot.pause()
        y_reading = log.scroll_offset.y
        assert not log.is_vertical_scroll_end

        r._push(
            {
                "kind": "assistant",
                "id": r.next_id(),
                "content": "arrives while reading",
                "reasoning": "",
                "streaming": False,
            }
        )
        app._refresh_ui("append")
        await pilot.pause()
        assert log.scroll_offset.y == y_reading  # view stayed put

        log.scroll_end(animate=False)
        await pilot.pause()
        r._push(
            {
                "kind": "assistant",
                "id": r.next_id(),
                "content": "arrives while pinned",
                "reasoning": "",
                "streaming": False,
            }
        )
        app._refresh_ui("append")
        await pilot.pause()
        assert log.is_vertical_scroll_end  # auto-follow resumes at bottom

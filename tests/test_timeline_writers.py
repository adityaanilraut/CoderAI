"""Coverage for the timeline row writers in coderAI/tui/timeline_render.py.

Each writer takes a ``SupportsWrite`` sink and pushes Rich renderables onto it.
``RecordingLog`` is a structural stand-in that just records what was written,
mirroring the RecordingLog used for render caching in the real app.
"""

from typing import Any, List

from coderAI.tui import timeline_render as tr


class RecordingLog:
    """Minimal SupportsWrite sink that records every renderable written."""

    def __init__(self) -> None:
        self.writes: List[Any] = []

    def write(self, renderable: Any) -> Any:
        self.writes.append(renderable)
        return None


def _wrote(log: RecordingLog) -> bool:
    return len(log.writes) > 0


def test_write_user_variants():
    from coderAI.tui.theme import Tokens

    # Non-collapsed body + timestamp: one cyan rail-wrapped block + trailing blank.
    log = RecordingLog()
    tr.write_user(log, {"text": "hello\nworld", "ts": 1_700_000_000})
    assert len(log.writes) == 2
    assert isinstance(log.writes[0], tr._RailBlock)
    assert log.writes[0].color == Tokens.INFO

    # Collapsed body uses the truncated dim renderer, still inside one rail block.
    log = RecordingLog()
    tr.write_user(log, {"text": "a\nb\nc\nd", "collapsed": True})
    assert len(log.writes) == 2
    assert isinstance(log.writes[0], tr._RailBlock)

    # Empty body: rail block (header only) + trailing blank.
    log = RecordingLog()
    tr.write_user(log, {"text": ""})
    assert len(log.writes) == 2
    assert isinstance(log.writes[0], tr._RailBlock)


def test_write_assistant_variants():
    # Verbose with reasoning, not collapsed, streaming on.
    log = RecordingLog()
    tr.write_assistant(
        log,
        {
            "reasoning": "step1\nstep2",
            "content": "answer line",
            "streaming": True,
            "ts": 1_700_000_000,
        },
        True,
    )
    assert _wrote(log)

    # Collapsed truncates content and omits reasoning + cursor.
    log = RecordingLog()
    tr.write_assistant(
        log,
        {"reasoning": "r", "content": "l1\nl2\nl3\nl4\nl5", "collapsed": True},
        True,
    )
    assert _wrote(log)

    # Non-verbose with no content still writes the assistant header + blank,
    # wrapped in a single green (AGENT) rail block.
    log = RecordingLog()
    tr.write_assistant(log, {"content": ""}, False)
    assert _wrote(log)
    from coderAI.tui.theme import Tokens

    assert isinstance(log.writes[0], tr._RailBlock)
    assert log.writes[0].color == Tokens.AGENT


def test_write_tool_variants():
    # ok=True with a recognized arg key + preview.
    log = RecordingLog()
    tr.write_tool(
        log,
        {
            "name": "read_file",
            "ok": True,
            "args": {"path": "a/b/c.py", "ignored": "x"},
            "preview": "file contents preview",
            "ts": 1_700_000_000,
        },
    )
    assert _wrote(log)

    # ok=False with an error line, not collapsed.
    log = RecordingLog()
    tr.write_tool(log, {"name": "run", "ok": False, "error": "boom"})
    assert len(log.writes) == 2  # row + error line

    # ok=None (running) with non-dict args.
    log = RecordingLog()
    tr.write_tool(log, {"name": "x", "ok": None, "args": "raw string args"})
    assert _wrote(log)

    # Collapsed hides args/preview but appends the ellipsis marker.
    log = RecordingLog()
    tr.write_tool(
        log,
        {"name": "x", "ok": True, "args": {"query": "q"}, "preview": "p", "collapsed": True},
    )
    assert _wrote(log)


def test_write_diff_variants():
    diff = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n ctx"
    # Collapsed shows a line count.
    log = RecordingLog()
    tr.write_diff(log, {"path": "f.py", "diff": diff, "collapsed": True}, False)
    assert len(log.writes) == 2

    # Non-collapsed verbose renders the gutter body.
    log = RecordingLog()
    tr.write_diff(log, {"path": "f.py", "diff": diff}, True)
    assert _wrote(log)


def test_write_error_with_and_without_hint():
    log = RecordingLog()
    tr.write_error(log, {"message": "bad", "hint": "try again", "ts": 1_700_000_000})
    assert len(log.writes) == 3  # header + message + hint

    log = RecordingLog()
    tr.write_error(log, {"message": "bad"})
    assert len(log.writes) == 2


def test_write_toast_known_and_unknown_level():
    for level in ("info", "success", "warning", "error", "mystery"):
        log = RecordingLog()
        tr.write_toast(log, {"level": level, "message": "m"})
        assert len(log.writes) == 1


def test_write_approval():
    log = RecordingLog()
    tr.write_approval(log, {"tool": "delete_file", "decided": "approved"})
    assert len(log.writes) == 1


def test_write_skill_card_with_and_without_steps():
    log = RecordingLog()
    tr.write_skill_card(
        log,
        {
            "name": "deploy",
            "description": "deploy the app",
            "steps": [{"index": i, "label": f"step {i}"} for i in range(15)],
        },
    )
    assert _wrote(log)

    log = RecordingLog()
    tr.write_skill_card(log, {"name": "noop"})
    assert _wrote(log)


def test_write_plan_card_statuses():
    log = RecordingLog()
    tr.write_plan_card(
        log,
        {
            "title": "Ship it",
            "completed": 1,
            "total": 3,
            "currentIdx": 0,
            "steps": [
                {"index": 1, "status": "done", "description": "did it"},
                {"index": 2, "status": "pending", "description": "current step"},
                {"index": 3, "status": "pending", "description": "later"},
            ],
        },
    )
    assert _wrote(log)


def test_write_timeline_item_dispatch_all_kinds():
    items = [
        {"kind": "user", "text": "hi"},
        {"kind": "assistant", "content": "yo"},
        {"kind": "tool", "name": "t", "ok": True},
        {"kind": "diff", "diff": "+a"},
        {"kind": "error", "message": "e"},
        {"kind": "toast", "message": "t"},
        {"kind": "separator", "message": "sep"},
        {"kind": "approval", "tool": "x"},
        {"kind": "skill_card", "name": "s"},
        {"kind": "plan_card", "title": "p"},
    ]
    for it in items:
        log = RecordingLog()
        tr.write_timeline_item(log, it, verbose=True)
        assert _wrote(log), it["kind"]


def test_write_timeline_item_unknown_kind_logs_and_writes_nothing():
    log = RecordingLog()
    tr.write_timeline_item(log, {"kind": "bogus"}, verbose=False)
    assert log.writes == []


def test_build_stream_tail_markup():
    out = tr.build_stream_tail_markup(
        {"reasoning": "thinking", "content": "partial", "ts": 1_700_000_000},
        verbose=True,
    )
    assert "assistant" in out
    assert "partial" in out

    # Non-verbose with no content still produces the assistant tail + cursor.
    out = tr.build_stream_tail_markup({"content": ""}, verbose=False)
    assert "assistant" in out

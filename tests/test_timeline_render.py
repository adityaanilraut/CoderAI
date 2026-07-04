from coderAI.tui.timeline_render import calculate_item_lines


def test_calculate_item_lines_user():
    # Uncollapsed user
    item = {"kind": "user", "text": "hello\nworld\nfoo"}
    assert calculate_item_lines(item, verbose=False) == 5  # 1 (header) + 3 (body) + 1 (empty)

    # Collapsed user (truncated)
    item = {"kind": "user", "text": "hello\nworld\nfoo\nbar", "collapsed": True}
    assert (
        calculate_item_lines(item, verbose=False) == 5
    )  # 1 (header) + 3 (2 content lines + 1 ellipsis line) + 1 (empty)


def test_calculate_item_lines_assistant():
    # Uncollapsed, verbose, with reasoning
    item = {
        "kind": "assistant",
        "reasoning": "thought 1\nthought 2",
        "content": "hello\nworld",
        "streaming": False,
        "collapsed": False,
    }
    # verbose=True:
    # Reasoning header: 1
    # Reasoning body: 2
    # Empty line: 1
    # Assistant header: 1
    # Content body: 2
    # Empty line: 1
    # Total = 1 + 2 + 1 + 1 + 2 + 1 = 8
    assert calculate_item_lines(item, verbose=True) == 8

    # verbose=False (should omit reasoning)
    # Assistant header: 1
    # Content body: 2
    # Empty line: 1
    # Total = 1 + 2 + 1 = 4
    assert calculate_item_lines(item, verbose=False) == 4

    # Collapsed assistant (not truncated, content is 2 lines)
    item["collapsed"] = True
    # Assistant header: 1
    # Content body: 2
    # Empty line: 1
    # Total = 1 + 2 + 1 = 4
    assert calculate_item_lines(item, verbose=True) == 4

    # Collapsed assistant (truncated, content is 5 lines)
    item["content"] = "line1\nline2\nline3\nline4\nline5"
    # Assistant header: 1
    # Content body: 4 (3 content lines + 1 ellipsis line)
    # Empty line: 1
    # Total = 1 + 4 + 1 = 6
    assert calculate_item_lines(item, verbose=True) == 6


def test_calculate_item_lines_tool():
    item = {"kind": "tool", "name": "run_command", "collapsed": False}
    assert calculate_item_lines(item, verbose=False) == 1

    item["error"] = "Command failed"
    assert calculate_item_lines(item, verbose=False) == 2

    item["collapsed"] = True
    assert calculate_item_lines(item, verbose=False) == 1


def test_calculate_item_lines_diff():
    item = {"kind": "diff", "diff": "line1\nline2\nline3", "collapsed": False}
    assert calculate_item_lines(item, verbose=False) == 4  # 1 + 3

    item["collapsed"] = True
    assert calculate_item_lines(item, verbose=False) == 2


def test_calculate_item_lines_welcome_matches_render():
    item = {"kind": "welcome", "model": "m1", "provider": "P", "cwd": "/proj"}
    assert calculate_item_lines(item, verbose=False) == 7

    # Parity guard: the writer must emit exactly 6 rail lines (+ the trailing
    # blank write) so the fixed height above stays in sync with the render.
    from rich.console import Console

    from coderAI.tui import timeline_render as tr

    class _Log:
        def __init__(self):
            self.writes = []

        def write(self, renderable):
            self.writes.append(renderable)

    log = _Log()
    tr.write_welcome(log, item)
    assert len(log.writes) == 2  # rail block + trailing blank
    console = Console(width=100)
    rail_lines = console.render_lines(log.writes[0], console.options, pad=False)
    assert len(rail_lines) == 6

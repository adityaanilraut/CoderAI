"""Pilot coverage for PromptArea history recall and @-mention triggering."""

from textual.app import App, ComposeResult

from coderAI.tui.screens import PromptArea


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.mention_calls = 0

    def compose(self) -> ComposeResult:
        yield PromptArea(id="prompt-area")

    def action_file_mention(self) -> None:
        self.mention_calls += 1


async def _submit(pilot, pa: PromptArea, text: str) -> None:
    """Type ``text`` and press Enter, then clear like the real app does."""
    pa.text = text
    await pilot.press("enter")
    pa.text = ""


async def test_up_down_recall_cycles_prompts():
    app = _Harness()
    async with app.run_test() as pilot:
        pa = app.query_one(PromptArea)
        pa.focus()
        await _submit(pilot, pa, "first")
        await _submit(pilot, pa, "second")

        await pilot.press("up")
        assert pa.text == "second"
        await pilot.press("up")
        assert pa.text == "first"
        await pilot.press("up")  # clamps at oldest
        assert pa.text == "first"

        await pilot.press("down")
        assert pa.text == "second"
        await pilot.press("down")  # back to the (empty) live draft
        assert pa.text == ""


async def test_up_with_no_history_is_inert():
    app = _Harness()
    async with app.run_test() as pilot:
        pa = app.query_one(PromptArea)
        pa.focus()
        await pilot.press("up")
        assert pa.text == ""


async def test_at_word_boundary_triggers_mention():
    app = _Harness()
    async with app.run_test() as pilot:
        pa = app.query_one(PromptArea)
        pa.focus()

        # Empty composer: @ is at a boundary -> mention fires, @ not inserted.
        await pilot.press("@")
        assert app.mention_calls == 1
        assert pa.text == ""

        # After whitespace: still a boundary.
        pa.text = "hello "
        pa.move_cursor(pa.document.end)
        await pilot.press("@")
        assert app.mention_calls == 2
        assert pa.text == "hello "


async def test_at_mid_word_inserts_literal_at():
    app = _Harness()
    async with app.run_test() as pilot:
        pa = app.query_one(PromptArea)
        pa.focus()
        pa.text = "foo"
        pa.move_cursor(pa.document.end)
        await pilot.press("@")
        # Not a boundary -> mention does not fire, @ is typed normally.
        assert app.mention_calls == 0
        assert pa.text == "foo@"

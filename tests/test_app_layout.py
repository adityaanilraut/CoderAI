"""Pilot coverage for the responsive pane layout and attention notifications.

The side panes cost 67 columns; on narrow terminals they must yield that
width to the conversation (right pane first, then left), while Ctrl+G /
Ctrl+B let the user override either pane until the override matches the
auto state again. Notifications (bell + OSC 9) fire only when the terminal
is unfocused and the config flag allows them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coderAI.tui.app import CoderAIApp
from coderAI.tui.screens import AgentEventMsg

pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine '.*_scan_project_files' was never awaited:RuntimeWarning"
)


class _Harness(CoderAIApp):
    """CoderAIApp with the real widget tree but no live agent session."""

    async def _run_agent(self) -> None:  # type: ignore[override]
        return

    async def _scan_project_files(self) -> None:  # type: ignore[override]
        return


async def test_panes_visible_on_wide_terminal():
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#left-pane").display
        assert app.query_one("#right-pane").display
        assert len(app.query("#tasks-pane")) == 1
        assert len(app.query("#plan-pane")) == 0


async def test_right_pane_hides_first_at_medium_width():
    app = _Harness()
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#left-pane").display
        assert not app.query_one("#right-pane").display


async def test_both_panes_hide_on_narrow_terminal():
    app = _Harness()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert not app.query_one("#left-pane").display
        assert not app.query_one("#right-pane").display


async def test_panes_react_to_live_resize():
    """Regression: on_resize must use the event's size — self.size still
    reports the pre-resize value while the handler runs, which left the
    layout one resize behind."""
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#right-pane").display

        await pilot.resize_terminal(110, 40)
        await pilot.pause()
        assert app.query_one("#left-pane").display
        assert not app.query_one("#right-pane").display

        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        assert not app.query_one("#left-pane").display

        await pilot.resize_terminal(140, 40)
        await pilot.pause()
        assert app.query_one("#left-pane").display
        assert app.query_one("#right-pane").display


async def test_ctrl_b_toggles_left_pane_while_composer_focused():
    app = _Harness()
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        assert not app.query_one("#left-pane").display

        # The composer holds focus, so this proves the binding reaches the app.
        await pilot.press("ctrl+b")
        assert app.query_one("#left-pane").display
        assert app._left_pane_pref is True  # override recorded (auto = hidden)

        # Toggling back matches the auto state again → override dropped.
        await pilot.press("ctrl+b")
        assert not app.query_one("#left-pane").display
        assert app._left_pane_pref is None


async def test_ctrl_g_override_survives_resize():
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        assert not app.query_one("#right-pane").display
        assert app._right_pane_pref is False

        # A re-layout pass must respect the user override, not the auto state.
        app._apply_responsive_layout()
        assert not app.query_one("#right-pane").display


async def test_notify_attention_only_when_unfocused_and_enabled(monkeypatch):
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bells = {"n": 0}
        monkeypatch.setattr(app, "bell", lambda: bells.__setitem__("n", bells["n"] + 1))

        app.app_focus = True
        app._notify_attention("focused: no-op")
        assert bells["n"] == 0

        app.app_focus = False
        app._notify_attention("unfocused: rings")
        assert bells["n"] == 1

        app.agent = SimpleNamespace(config=SimpleNamespace(tui_notifications=False))
        app._notify_attention("disabled by config: no-op")
        assert bells["n"] == 1


async def test_ready_after_submit_triggers_attention_notification(monkeypatch):
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        calls: list[str] = []
        monkeypatch.setattr(app, "_notify_attention", calls.append)

        # Bootstrap ready (no turn in flight) stays silent.
        app.post_message(AgentEventMsg("ready", {}))
        await pilot.pause()
        assert calls == []

        app._awaiting_response = True  # as set by _submit on send_message
        app.post_message(AgentEventMsg("ready", {}))
        await pilot.pause()
        assert len(calls) == 1
        assert not app._awaiting_response

        # Approval requests notify too.
        app.post_message(
            AgentEventMsg(
                "tool",
                {"id": "t1", "phase": "awaiting_approval", "payload": {"name": "run_command"}},
            )
        )
        await pilot.pause()
        assert len(calls) == 2
        assert "run_command" in calls[1]

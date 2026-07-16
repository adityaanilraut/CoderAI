"""Pilot coverage for /resume: SessionPickerScreen and the agent swap.

The picker lists saved sessions (newest first, current one disabled) and
dismisses with the chosen session id; _start_resumed_agent then retires the
live controller (suppressing its goodbye toast) and restarts the agent
worker with the resume id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from textual.app import App
from textual.widgets import OptionList

from coderAI.tui.app import CoderAIApp
from coderAI.tui.screens import AgentEventMsg, SessionPickerScreen

pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine '.*_scan_project_files' was never awaited:RuntimeWarning"
)

SESSIONS = [
    {
        "session_id": "session_200_bbb",
        "created_at": "2026-07-03 09:00:00",
        "updated_at": "2026-07-03 10:00:00",
        "messages": 12,
        "model": "deepseek-chat",
    },
    {
        "session_id": "session_100_aaa",
        "created_at": "2026-07-01 08:00:00",
        "updated_at": "2026-07-02 09:00:00",
        "messages": 3,
        "model": "gpt-4",
    },
]


class _PickerHost(App):
    """Minimal app hosting a SessionPickerScreen and capturing its result."""

    def __init__(self, sessions, current_id=None):
        super().__init__()
        self._sessions = sessions
        self._current_id = current_id
        self.result = "unset"

    def on_mount(self) -> None:
        self.push_screen(
            SessionPickerScreen(self._sessions, current_id=self._current_id),
            lambda res: setattr(self, "result", res),
        )


class _Harness(CoderAIApp):
    """CoderAIApp with the real widget tree but no live agent session."""

    async def _run_agent(self) -> None:  # type: ignore[override]
        return

    async def _scan_project_files(self) -> None:  # type: ignore[override]
        return


class _FakeController:
    def __init__(self):
        self.commands = []

    def enqueue_command(self, name, **kwargs):
        self.commands.append((name, kwargs))


async def test_enter_returns_highlighted_session_id():
    app = _PickerHost(SESSIONS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "session_200_bbb"


async def test_escape_dismisses_with_none():
    app = _PickerHost(SESSIONS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None


async def test_typing_filters_sessions():
    app = _PickerHost(SESSIONS)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for ch in "gpt":
            await pilot.press(ch)
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "session_100_aaa"


async def test_current_session_disabled_and_skipped():
    app = _PickerHost(SESSIONS, current_id="session_200_bbb")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(0).disabled
        # Highlight starts on the first selectable session instead.
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "session_100_aaa"


async def test_no_sessions_shows_disabled_placeholder():
    app = _PickerHost([])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.option_count == 1
        assert options.get_option_at_index(0).disabled
        # Enter on a disabled row must not dismiss with a bogus id.
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == "unset"


async def test_resume_command_opens_picker_and_hands_off_choice(monkeypatch):
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            "coderAI.tui.app.history_manager",
            SimpleNamespace(list_sessions=lambda: SESSIONS),
        )
        resumed: list[str] = []
        monkeypatch.setattr(app, "_start_resumed_agent", resumed.append)

        app._resume_session(None)
        await pilot.pause()
        assert isinstance(app.screen, SessionPickerScreen)

        await pilot.press("enter")
        await pilot.pause()
        assert resumed == ["session_200_bbb"]


async def test_start_resumed_agent_swaps_session_state():
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.controller = _FakeController()
        app.agent = SimpleNamespace(session=SimpleNamespace(session_id="session_100_aaa"))
        app.reducer.timeline.append({"kind": "user", "id": "u1", "text": "old turn"})
        app.reducer.session.agents["agent_old"] = SimpleNamespace(name="main", status="idle")
        app.reducer.session.ready = True

        app._start_resumed_agent("session_200_bbb")
        await pilot.pause()

        assert ("exit", {}) in app.controller.commands
        assert app._suppress_goodbye
        assert app._resume == "session_200_bbb"
        assert not app.reducer.session.ready
        assert app.reducer.session.agents == {}  # stale tree entries retired
        kinds = [it.get("kind") for it in app.reducer.timeline]
        assert "user" not in kinds  # old timeline cleared
        assert any(
            "Resuming session session_200_bbb" in str(it.get("message", ""))
            for it in app.reducer.timeline
        )


async def test_start_resumed_agent_rejects_current_session():
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.controller = _FakeController()
        app.agent = SimpleNamespace(session=SimpleNamespace(session_id="session_100_aaa"))
        app.reducer.session.ready = True

        app._start_resumed_agent("session_100_aaa")
        await pilot.pause()

        assert app.controller.commands == []
        assert app.reducer.session.ready
        assert app._resume is None


async def test_goodbye_suppressed_exactly_once():
    app = _Harness()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()

        app._suppress_goodbye = True
        app.post_message(AgentEventMsg("goodbye", {}))
        await pilot.pause()
        assert not any(
            "session ended" in str(it.get("message", "")).lower() for it in app.reducer.timeline
        )
        assert not app._suppress_goodbye

        # The next goodbye (a real shutdown) surfaces normally.
        app.post_message(AgentEventMsg("goodbye", {}))
        await pilot.pause()
        assert any(
            "session ended" in str(it.get("message", "")).lower() for it in app.reducer.timeline
        )

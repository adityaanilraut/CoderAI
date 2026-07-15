"""Keyboard and state coverage for the trust-sensitive approval modal."""

from __future__ import annotations

from textual.app import App

from coderAI.tui.app import CoderAIApp
from coderAI.tui.screens import ApprovalScreen, CommandPaletteScreen
from textual.widgets import Button


class _ScreenHost(App):
    def __init__(self, approval):
        super().__init__()
        self.approval = approval
        self.result = "pending"

    def on_mount(self) -> None:
        self.push_screen(ApprovalScreen(self.approval), self._capture)

    def _capture(self, result) -> None:
        self.result = result


class _AppHarness(CoderAIApp):
    async def _run_agent(self) -> None:  # type: ignore[override]
        return

    async def _scan_project_files(self) -> None:  # type: ignore[override]
        return


class _Controller:
    def __init__(self) -> None:
        self.commands = []

    async def submit_command(self, name, **fields) -> None:
        self.commands.append((name, fields))

    def enqueue_command(self, name, **fields) -> None:
        if name != "exit":
            self.commands.append((name, fields))


async def test_command_approval_offers_scoped_memory_not_yolo():
    app = _ScreenHost(
        {
            "id": "t1",
            "tool": "run_command",
            "risk": "high",
            "args": {"command": "pytest tests/auth -q"},
            "rememberMode": "scope",
            "rememberScope": "pytest tests/auth -q",
            "rememberLabel": "Allow this command prefix",
        }
    )
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        labels = [str(button.label) for button in app.screen.query(Button)]
        assert any("Run once" in label for label in labels)
        assert any("command prefix" in label for label in labels)
        assert all("YOLO" not in label for label in labels)

        await pilot.press("a")
        await pilot.pause()
        assert app.result == (True, True)


async def test_workspace_trust_has_no_always_action():
    app = _ScreenHost(
        {
            "id": "trust1",
            "tool": "workspace_trust",
            "risk": "high",
            "args": {"path": "/tmp/project"},
        }
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        labels = [str(button.label) for button in app.screen.query(Button)]
        assert any("Trust workspace" in label for label in labels)
        assert len(list(app.screen.query("#approve-a"))) == 0

        await pilot.press("a")
        await pilot.pause()
        assert app.result == "pending"


async def test_cancelled_backend_approval_closes_stale_modal():
    app = _AppHarness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        result = "pending"

        def capture(value):
            nonlocal result
            result = value

        app.push_screen(
            ApprovalScreen({"id": "t-timeout", "tool": "run_command", "risk": "high"}),
            capture,
        )
        await pilot.pause()
        app._dismiss_cancelled_approval("t-timeout", "timeout")
        await pilot.pause()
        assert result is None


async def test_remember_action_records_scope_without_enabling_yolo():
    app = _AppHarness()
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        controller = _Controller()
        app.controller = controller
        app.reducer.handle(
            "tool",
            {
                "id": "t-scoped",
                "phase": "awaiting_approval",
                "payload": {
                    "name": "run_command",
                    "risk": "high",
                    "args": {"command": "pytest tests/auth -q"},
                    "rememberMode": "scope",
                    "rememberScope": "pytest tests/auth -q",
                    "rememberLabel": "Allow this command prefix",
                },
            },
        )
        app.run_worker(app._maybe_show_approval())
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

        assert controller.commands == [
            (
                "allow_tool",
                {"tool": "run_command", "scope": "pytest tests/auth -q"},
            ),
            ("tool_approval_resp", {"toolId": "t-scoped", "approve": True}),
        ]


async def test_super_k_opens_command_palette():
    app = _AppHarness()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("super+k")
        await pilot.pause()
        assert isinstance(app.screen, CommandPaletteScreen)

"""Focused contracts for the TUI adapter/application-service boundary."""

from coderAI.application import tui_plan_service, tui_session_service
from coderAI.tui.app import CoderAIApp
from coderAI.tui.app_events import AppEventController
from coderAI.tui.app_input import AppInputController
from coderAI.tui.app_layout import AppLayoutController
from coderAI.tui.app_timeline import AppTimelineController
from coderAI.tui.commands import _COMMAND_HANDLERS, _cmd_send_message, _cmd_start_plan


def test_command_adapter_dispatches_to_application_services() -> None:
    assert _cmd_send_message is tui_session_service._cmd_send_message
    assert _cmd_start_plan is tui_plan_service._cmd_start_plan
    assert _COMMAND_HANDLERS["send_message"] is tui_session_service._cmd_send_message
    assert _COMMAND_HANDLERS["start_plan"] is tui_plan_service._cmd_start_plan


def test_textual_app_composes_responsibility_controllers() -> None:
    assert issubclass(CoderAIApp, AppLayoutController)
    assert issubclass(CoderAIApp, AppTimelineController)
    assert issubclass(CoderAIApp, AppEventController)
    assert issubclass(CoderAIApp, AppInputController)

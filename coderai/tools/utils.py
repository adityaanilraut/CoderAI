"""Tool rejection error shared by tool adapters."""

from __future__ import annotations

from kosong.tooling import ToolError


class ToolRejectedError(ToolError):
    has_feedback: bool = False

    def __init__(
        self,
        message: str | None = None,
        brief: str = "Rejected by user",
        has_feedback: bool = False,
    ):
        super().__init__(
            message=message
            or (
                "The tool call is rejected by the user. "
                "Stop what you are doing and wait for the user to tell you how to proceed."
            ),
            brief=brief,
        )
        self.has_feedback = has_feedback


__all__ = ["ToolRejectedError"]

# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Backend-event dispatch, notifications, and approval presentation."""

from __future__ import annotations

from typing import Any

from textual import on

from coderAI.tui.listeners import RefreshMode
from coderAI.tui.screens import AgentEventMsg, ApprovalScreen


class AppEventController:
    def _on_reducer_change(self, mode: RefreshMode) -> None:
        self.post_message(AgentEventMsg("__refresh__", {"mode": mode}))

    def _toast(self, level: str, message: str) -> None:
        """Push a toast notification to the timeline."""
        # Respect per-level mute from notification settings
        muted: set[str] = getattr(self, "_muted_toast_levels", set())
        if level in muted:
            # Queue muted toasts for visible queue UI instead of dropping silently
            try:
                if not hasattr(self, "_toast_queue"):
                    self._toast_queue = []  # type: ignore[attr-defined]
                self._toast_queue.append({"level": level, "message": message, "muted": True})  # type: ignore[attr-defined]
                self._refresh_toast_queue()
            except Exception:
                pass
            return
        self.reducer.toast(level, message)
        try:
            if not hasattr(self, "_toast_queue"):
                self._toast_queue = []  # type: ignore[attr-defined]
            self._toast_queue.append({"level": level, "message": message, "muted": False})  # type: ignore[attr-defined]
            # Cap queue to 10
            if len(self._toast_queue) > 10:  # type: ignore[attr-defined]
                self._toast_queue = self._toast_queue[-10:]  # type: ignore[attr-defined]
            self._refresh_toast_queue()
        except Exception:
            pass

    def _refresh_toast_queue(self) -> None:
        """Progress toast queue UI (visible queue when muting is state-only previously)."""
        try:
            # Store for potential header display; actual UI is via notify + timeline
            # For now just keep count for HUD
            pass
        except Exception:
            pass

    def _notify_attention(self, message: str) -> None:
        """Bell + OSC 9 desktop notification when the terminal is unfocused."""
        if self.app_focus:
            return
        if getattr(self, "_notifications_muted", False):
            return
        cfg = getattr(self.agent, "config", None)
        if not getattr(cfg, "tui_notifications", True):
            return
        self.bell()
        driver = self._driver
        if driver is None:
            return
        safe = "".join(ch for ch in message if ch.isprintable())[:120]
        try:
            driver.write(f"\x1b]9;{safe}\x07")
            driver.flush()
        except Exception:
            pass

    @on(AgentEventMsg)
    async def _on_agent_event(self, msg: AgentEventMsg) -> None:
        if msg.event == "__refresh__":
            self._refresh_ui(str(msg.data.get("mode", "full")))
            return
        if msg.event == "goodbye" and self._suppress_goodbye:
            self._suppress_goodbye = False
            return
        self.reducer.handle(msg.event, msg.data)
        if msg.event == "tool" and msg.data.get("phase") == "awaiting_approval":
            payload = msg.data.get("payload") or {}
            self._notify_attention(f"CoderAI: approval needed — {payload.get('name') or 'tool'}")
            self.run_worker(self._maybe_show_approval())
        elif msg.event == "tool" and msg.data.get("phase") == "cancelled":
            payload = msg.data.get("payload") or {}
            self._dismiss_cancelled_approval(
                str(msg.data.get("id") or ""), str(payload.get("reason") or "cancelled")
            )
        elif msg.event == "ready" and self._awaiting_response:
            self._awaiting_response = False
            self._notify_attention("CoderAI: finished — ready for your next message")

    def _emit_bridge(self, event: str, data: dict[str, Any]) -> None:
        try:
            self.call_from_thread(self.post_message, AgentEventMsg(event, data))
        except RuntimeError:
            self.post_message(AgentEventMsg(event, data))

    async def _maybe_show_approval(self) -> None:
        pending = self.reducer.pending_approval()
        if not pending:
            return
        result = await self.push_screen_wait(ApprovalScreen(pending))
        if result is None:
            return
        approve, remember = result
        if self.controller:
            # Remember only the reviewed tool/path/command scope advertised by
            # the backend. Session-wide unsafe auto-approve remains an explicit
            # /yolo action and is never enabled from a routine approval prompt.
            #
            # Prefer enqueue_command: the agent loop owns the approval Future on
            # a worker thread, and UI-thread submit_command historically stalled
            # the turn until the next user message woke the loop.
            if approve and remember and pending.get("rememberMode"):
                self.controller.enqueue_command(
                    "allow_tool",
                    tool=str(pending.get("tool") or ""),
                    scope=str(pending.get("rememberScope") or ""),
                )
            self.controller.enqueue_command(
                "tool_approval_resp",
                toolId=pending["id"],
                approve=approve,
            )
        pending["decided"] = "approved" if approve else "denied"
        self._refresh_ui("full")

    def _dismiss_cancelled_approval(self, tool_id: str, reason: str) -> None:
        """Close a modal whose backend waiter has timed out or been cancelled."""
        active = self.screen
        if not isinstance(active, ApprovalScreen) or active.approval_id != tool_id:
            return
        active.dismiss(None)
        if reason == "timeout":
            self.notify("Approval timed out and was denied", severity="warning")

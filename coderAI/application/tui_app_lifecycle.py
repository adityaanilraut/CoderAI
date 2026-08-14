# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Agent/session lifecycle and persisted UI preferences for the Textual adapter."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from coderAI.tui.screens import SessionPickerScreen

MIN_PANE_WIDTH = 20
MAX_PANE_WIDTH = 60


class AppLifecycleService:
    async def _run_agent(self) -> None:
        from coderAI.tui import app as app_module

        try:
            self.agent, self.controller = app_module.create_agent_session(
                model=self._model,
                resume=self._resume,
                continue_=self._continue,
                auto_approve=self._auto_approve,
                persona=self._persona,
                on_event=self._emit_bridge,
            )
        except Exception as exc:
            self._emit_bridge(
                "error",
                {
                    "category": "internal",
                    "message": f"Failed to start agent: {exc}",
                    "hint": "Run `coderAI doctor` and `coderAI setup` to verify config.",
                },
            )
            self._emit_bridge("goodbye", {"reason": "startup_failed"})
            return
        try:
            await self.controller.start()
        except Exception as exc:
            self._emit_bridge(
                "error",
                {"category": "internal", "message": f"Agent loop crashed: {exc}"},
            )
            if self._agent_retry_count < 1:
                self._agent_retry_count += 1
                self._emit_bridge(
                    "info",
                    {"message": "Auto-restarting agent…"},
                )
                self._start_agent_worker()
            else:
                self._emit_bridge(
                    "info",
                    {"message": "Agent crashed. Type /retry to restart."},
                )
                self._emit_bridge("goodbye", {"reason": "loop_crashed"})

    async def _scan_project_files(self) -> None:
        from coderAI.tui import app as app_module

        if self._scan_in_progress:
            return
        self._scan_in_progress = True
        try:
            root = getattr(self.agent.config, "project_root", None) if self.agent else None
            if not root:
                root = self.reducer.session.cwd or os.getcwd()
            self.project_files = await app_module.async_scan_project_files(root)
            self._scan_error = None  # type: ignore[attr-defined]
        except Exception as e:
            import logging as _log

            _log.getLogger(__name__).warning("Project file scan failed: %s", e)
            self._scan_error = str(e)  # type: ignore[attr-defined]
        finally:
            self._scan_in_progress = False

    def _start_agent_worker(self) -> None:
        """Run the backend agent loop on the exclusive background worker thread."""
        self.run_worker(
            self._run_agent,  # type: ignore[arg-type]
            exclusive=True,
            thread=True,
            name="agent-loop",
        )

    def _retry_agent(self) -> None:
        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        if self.controller:
            self._suppress_goodbye = True
            self.controller.enqueue_command("exit")
        if current_id:
            self._resume = current_id
            self._continue = False
        self._agent_retry_count = 0
        self.reducer.session.ready = False
        self._toast("info", "Restarting agent…")
        self._start_agent_worker()

    def _resume_session(self, session_id: Optional[str]) -> None:
        """/resume entry point: with an id, resume it; without, open the picker."""
        if session_id:
            self._start_resumed_agent(session_id)
        else:
            self.run_worker(self._show_session_picker(), exclusive=True)

    async def _show_session_picker(self) -> None:
        from coderAI.tui import app as app_module

        # list_sessions hits the filesystem (index rebuild, expiry cleanup) —
        # keep it off the UI loop.
        sessions = await asyncio.to_thread(app_module.history_manager.list_sessions)
        if not sessions:
            self._toast("info", "No saved sessions to resume.")
            return
        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        result = await self.push_screen_wait(SessionPickerScreen(sessions, current_id=current_id))
        if result:
            self._start_resumed_agent(result)

    def _start_resumed_agent(self, session_id: str) -> None:
        """Swap the live agent for one resumed from a saved session.

        The old controller is asked to exit (its goodbye is suppressed), the
        local timeline is cleared, and the agent worker restarts with the
        resume id — same lifecycle as /retry, plus session selection.
        """
        from coderAI.tui import app as app_module

        current_id = getattr(getattr(self.agent, "session", None), "session_id", None)
        if session_id == current_id:
            self._toast("info", "That session is already active.")
            return
        if self.controller:
            self._suppress_goodbye = True
            self.controller.enqueue_command("exit")
        # Retire the old session's agents from the tracker before the new
        # agent registers — the new controller's bootstrap re-emits every
        # tracked agent, so stale entries would reappear in the tree.
        app_module.agent_tracker.clear_except()
        self.reducer.session.agents.clear()
        self.reducer.timeline.clear()
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._resume = session_id
        self._continue = False
        self._agent_retry_count = 0
        self.reducer.session.ready = False
        self._awaiting_response = False
        self._toast("info", f"Resuming session {session_id}…")
        self._refresh_ui("full")
        self._start_agent_worker()

    def _persist_ui_state(self) -> None:
        """Persist pane widths + theme to config.json."""
        try:
            import json
            import pathlib

            cfg_path = pathlib.Path.home() / ".coderAI" / "config.json"
            data: dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data["tui"] = {
                "left_width": self._left_width,
                "right_width": self._right_width,
                "theme": self._theme_mode,
            }
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_persisted_ui(self) -> None:
        try:
            import json
            import pathlib

            cfg_path = pathlib.Path.home() / ".coderAI" / "config.json"
            if not cfg_path.exists():
                return
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            tui = data.get("tui") or {}
            if "left_width" in tui:
                self._left_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, int(tui["left_width"])))
            if "right_width" in tui:
                self._right_width = max(
                    MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, int(tui["right_width"]))
                )
            if "theme" in tui and tui["theme"] in ("dark", "high-contrast"):
                self._theme_mode = str(tui["theme"])
        except Exception:
            pass

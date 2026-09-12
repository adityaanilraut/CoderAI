"""Per-turn soul view over ``SessionManager``.

``SessionSoul`` is a thin adapter, not a second loop: it exposes the small
surface injection providers and subagent builders need (afk/yolo,
plan-mode paths, pending-activation flag, checkpoint counting) while
``SessionManager`` + ``AgentLoop`` keep owning execution. One instance per
``(manager, session_id, is_subagent)``.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Literal

from coderai.soul.dynamic_injection import InjectionRegistry, SoulView, default_registry


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltinSystemPromptArgs:
    """Builtin system prompt arguments."""

    CODERAI_NOW: str = ""
    CODERAI_WORK_DIR: Any = None
    CODERAI_WORK_DIR_LS: str = ""
    CODERAI_AGENTS_MD: str = ""
    CODERAI_SKILLS: str = ""
    CODERAI_ADDITIONAL_DIRS_INFO: str = ""
    CODERAI_OS: str = ""
    CODERAI_SHELL: str = ""


@dataclass(slots=True, kw_only=True)
class Runtime:
    """Agent runtime."""

    config: Any = None
    oauth: Any = None
    llm: Any = None
    session: Any = None
    builtin_args: Any = None
    denwa_renji: Any = None
    approval: Any = None
    labor_market: Any = None
    environment: Any = None
    notifications: Any = None
    background_tasks: Any = None
    skills: dict[str, Any] = field(default_factory=dict)
    additional_dirs: list[Any] = field(default_factory=list)
    skills_dirs: list[Any] = field(default_factory=list)
    subagent_store: Any = None
    approval_runtime: Any = None
    root_wire_hub: Any = None
    subagent_id: str | None = None
    subagent_type: str | None = None
    role: str = "root"
    ui_mode: str = "shell"
    resumed: bool = False
    hook_engine: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Agent:
    """The loaded agent."""

    name: str
    system_prompt: str
    toolset: Any
    runtime: Runtime


class SessionSoul(SoulView):
    """Adapter exposing soul state for injections and subagent builders."""

    def __init__(
        self,
        manager: Any,
        session_id: str,
        *,
        is_subagent: bool = False,
        injections: InjectionRegistry | None = None,
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._is_subagent = is_subagent
        self.injections = injections or default_registry()
        self._pending_plan_activation = False

    # -- identity -------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def manager(self) -> Any:
        return self._manager

    @property
    def is_subagent(self) -> bool:
        return self._is_subagent

    @property
    def is_root(self) -> bool:
        return not self._is_subagent

    # -- approval -------------------------------------------------------
    @property
    def is_yolo(self) -> bool:
        try:
            return bool(self._manager.is_yolo())
        except Exception:
            return False

    @property
    def is_afk(self) -> bool:
        try:
            return bool(self._manager.is_afk())
        except Exception:
            return False

    @property
    def is_auto_approve(self) -> bool:
        try:
            return bool(self._manager.is_auto_approve())
        except Exception:
            return self.is_yolo or self.is_afk

    async def notify_afk_changed(self, enabled: bool) -> None:
        """Re-arm injection providers after an afk toggle."""
        await self.injections.notify_afk_changed(enabled)

    async def notify_compacted(self) -> None:
        """Reset provider throttling after compaction rewrites history."""
        await self.injections.notify_compacted()

    # -- plan mode ------------------------------------------------------
    @property
    def plan_mode(self) -> bool:
        try:
            entry = self._manager._get_entry(self._session_id)
        except Exception:
            entry = None
        return bool((entry or {}).get("planMode"))

    def get_plan_file_path(self) -> str | None:
        """Plan file for this session (``.coderai/plans/<id>.md``)."""
        try:
            root = self._manager.project_root
        except Exception:
            return None
        return str(pathlib.Path(root) / ".coderai" / "plans" / f"{self._session_id}.md")

    def read_current_plan(self) -> str | None:
        """Current plan content, or None when no plan file exists."""
        path = self.get_plan_file_path()
        if not path:
            return None
        try:
            candidate = pathlib.Path(path)
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        return None

    def clear_current_plan(self) -> None:
        """Delete the plan file (best-effort)."""
        path = self.get_plan_file_path()
        if not path:
            return
        try:
            pathlib.Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def schedule_plan_activation_reminder(self) -> None:
        """Schedule the one-shot activation reminder for the next LLM step."""
        self._pending_plan_activation = True

    def consume_pending_plan_activation_injection(self) -> bool:
        """Take the pending activation flag (True once per schedule)."""
        pending, self._pending_plan_activation = self._pending_plan_activation, False
        return pending

    async def set_plan_mode(self, enabled: bool) -> bool:
        """Set plan mode via the manager; schedules the activation reminder."""
        try:
            await self._manager.reply_session(self._session_id, plan_mode=bool(enabled))
        except Exception:
            return self.plan_mode
        if enabled:
            self.schedule_plan_activation_reminder()
        return self.plan_mode

    async def toggle_plan_mode(self) -> bool:
        """Flip plan mode; returns the new state."""
        return await self.set_plan_mode(not self.plan_mode)

    # -- history / checkpoints ------------------------------------------
    def history_for_injections(self) -> list[dict[str, Any]]:
        """Recent messages as plain dicts for provider throttling scans."""
        try:
            messages = self._manager.list_session_messages(self._session_id)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for m in messages[-60:]:
            role = getattr(m, "role", "")
            content = getattr(m, "content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
        return out

    def checkpoint_count(self) -> int:
        """Number of recorded file-history checkpoints (= D-Mail id space)."""
        try:
            history = self._manager.file_history
            session = getattr(history, "_sessions", {}).get(self._session_id)
            checkpoints = getattr(session, "checkpoints", None)
            if checkpoints is not None:
                return len(checkpoints)
        except Exception:
            pass
        return 0

    async def collect_injections(self) -> list:
        """Run providers against recent history (never raises)."""
        try:
            return await self.injections.collect(self.history_for_injections(), self)
        except Exception:
            return []


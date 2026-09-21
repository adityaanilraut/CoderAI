from coderai.soul.agent import (
    SessionSoul,
)
from coderai.soul.coderaisoul import (
    AgentLoop,
)
from coderai.soul.compaction import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    CompactionResult,
    ToolResultPruner,
    prune_tool_results_for_compaction,
    evaluate_compaction_trigger,
    should_auto_compact,
    CompactionEngine,
    BasicCompaction,
)
from coderai.soul.denwarenji import (
    DenwaRenjiError,
    DMail,
    DenwaRenji,
)
from coderai.soul.btw import (
    BTW_SYSTEM,
    SIDE_QUESTION_SYSTEM_REMINDER,
    DMAIL_SYSTEM_PREFIX,
    build_side_messages,
    run_side_question,
    inject_dmail_and_continue,
)
from coderai.soul.approval import (
    ASK_SCOPES,
    BASH_SIDE_EFFECTS,
    PERMISSION_DESCRIBED_TOOLS,
    PERMISSION_EXEMPT_REGISTERED_TOOLS,
    PLAN_MODE_FORCE_ASK_SCOPES,
    Decision,
    get_scope_risk_level,
    get_request_risk_badge,
    PermissionTicket,
    PermissionTicketRegistry,
    get_permission_ticket_registry,
    parse_tool_call_for_permissions,
    permission_coverage_gaps,
    parse_tool_arguments,
    is_path_in_project,
    parse_bash_side_effects,
    describe_tool_permission_request,
    DEFAULT_PERMISSION_SETTINGS,
    evaluate_permission_scopes,
    get_scopes_requiring_ask,
    compute_tool_call_permissions,
    resolve_tool_call_permission,
    build_synthetic_tool_execution,
    build_permission_tool_execution,
    normalize_ask_permissions,
    append_project_permission_allows,
    VALID_WRITE_SCOPES,
    resolve_snippet_file_path,
)
from coderai.soul.dynamic_injection import (
    DynamicInjection,
    SoulView,
    DynamicInjectionProvider,
    AFK_INJECTION_TYPE,
    AFK_PROMPT_ROOT,
    AFK_DISABLED_REMINDER,
    AfkModeInjectionProvider,
    plan_full_reminder,
    plan_sparse_reminder,
    plan_reentry_reminder,
    PlanModeInjectionProvider,
    InjectionRegistry,
    default_registry,
    wrap_as_reminder,
)

__all__ = [
    "SessionSoul",
    "AgentLoop",
    "DEFAULT_MAX_TOOL_RESULT_CHARS",
    "CompactionResult",
    "ToolResultPruner",
    "prune_tool_results_for_compaction",
    "evaluate_compaction_trigger",
    "should_auto_compact",
    "CompactionEngine",
    "BasicCompaction",
    "DenwaRenjiError",
    "DMail",
    "DenwaRenji",
    "BTW_SYSTEM",
    "SIDE_QUESTION_SYSTEM_REMINDER",
    "DMAIL_SYSTEM_PREFIX",
    "build_side_messages",
    "run_side_question",
    "inject_dmail_and_continue",
    "ASK_SCOPES",
    "BASH_SIDE_EFFECTS",
    "PERMISSION_DESCRIBED_TOOLS",
    "PERMISSION_EXEMPT_REGISTERED_TOOLS",
    "PLAN_MODE_FORCE_ASK_SCOPES",
    "Decision",
    "get_scope_risk_level",
    "get_request_risk_badge",
    "PermissionTicket",
    "PermissionTicketRegistry",
    "get_permission_ticket_registry",
    "parse_tool_call_for_permissions",
    "permission_coverage_gaps",
    "parse_tool_arguments",
    "is_path_in_project",
    "parse_bash_side_effects",
    "describe_tool_permission_request",
    "DEFAULT_PERMISSION_SETTINGS",
    "evaluate_permission_scopes",
    "get_scopes_requiring_ask",
    "compute_tool_call_permissions",
    "resolve_tool_call_permission",
    "build_synthetic_tool_execution",
    "build_permission_tool_execution",
    "normalize_ask_permissions",
    "append_project_permission_allows",
    "VALID_WRITE_SCOPES",
    "resolve_snippet_file_path",
    "DynamicInjection",
    "SoulView",
    "DynamicInjectionProvider",
    "AFK_INJECTION_TYPE",
    "AFK_PROMPT_ROOT",
    "AFK_DISABLED_REMINDER",
    "AfkModeInjectionProvider",
    "plan_full_reminder",
    "plan_sparse_reminder",
    "plan_reentry_reminder",
    "PlanModeInjectionProvider",
    "InjectionRegistry",
    "default_registry",
    "wrap_as_reminder",
    "StatusSnapshot",
    "format_context_status",
    "Soul",
    "get_wire_or_none",
    "wire_send",
]

from coderai.cli.elapsed import format_context_status

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from coderai.wire.types import MCPStatusSnapshot
from coderai.wire import Wire


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    context_usage: float
    yolo_enabled: bool = False
    afk_enabled: bool = False
    plan_mode: bool = False
    context_tokens: int = 0
    max_context_tokens: int = 0
    mcp_status: MCPStatusSnapshot | None = None


@runtime_checkable
class Soul(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def model_name(self) -> str: ...


_current_wire: ContextVar[Any | None] = ContextVar("current_wire", default=None)


def get_wire_or_none() -> Any | None:
    """Get the current wire or None."""
    return _current_wire.get()


def wire_send(msg: Any) -> None:
    """Send a wire message to the current wire."""
    wire = get_wire_or_none()
    if wire is not None:
        wire.soul_side.send(msg)


class LLMNotSet(Exception):
    """Raised when the LLM is not set."""

    def __init__(self) -> None:
        super().__init__("LLM not set")


class LLMNotSupported(Exception):
    """Raised when the LLM does not have required capabilities."""

    def __init__(self, llm: Any = None, capabilities: list[Any] | None = None):
        name = getattr(llm, "model_name", "unknown") if llm else "unknown"
        caps = capabilities or []
        super().__init__(
            f"LLM model '{name}' does not support required capabilities: {', '.join(str(c) for c in caps)}."
        )


class MaxStepsReached(Exception):
    """Raised when the maximum number of steps is reached."""

    def __init__(self, n_steps: int):
        super().__init__(f"Max number of steps reached: {n_steps}")
        self.n_steps = n_steps


class RunCancelled(Exception):
    """The run was cancelled by the cancel event."""


try:
    from asyncio import QueueShutDown  # Python 3.13+
except ImportError:

    class QueueShutDown(Exception):  # type: ignore[no-redef]
        pass


import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from coderai.wire.file import WireFile
from coderai.wire.types import ContentPart
from coderai.utils.logging import logger

UILoopFn = Callable[[Wire], Coroutine[Any, Any, None]]


async def run_soul(
    soul: Soul,
    user_input: str | list[ContentPart],
    ui_loop_fn: UILoopFn,
    cancel_event: asyncio.Event,
    wire_file: WireFile | None = None,
    runtime: Any | None = None,
    *,
    skip_user_prompt_hook: bool = False,
) -> None:
    """Run the soul with the given user input, connecting it to the UI loop with a Wire."""
    wire = Wire(file_backend=wire_file)
    wire_token = _current_wire.set(wire)

    logger.debug("Starting UI loop with function: {ui_loop_fn}", ui_loop_fn=ui_loop_fn)
    ui_task = asyncio.create_task(ui_loop_fn(wire))

    logger.debug("Starting soul run")
    soul_task = asyncio.create_task(
        soul.run(user_input, skip_user_prompt_hook=skip_user_prompt_hook)
    )
    notification_task = asyncio.create_task(_pump_notifications_to_wire(runtime, wire))

    cancel_event_task = asyncio.create_task(cancel_event.wait())
    await asyncio.wait(
        [soul_task, cancel_event_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    try:
        if cancel_event.is_set():
            logger.debug("Cancelling the run task")
            soul_task.cancel()
            try:
                await soul_task
            except asyncio.CancelledError:
                raise RunCancelled from None
        else:
            assert soul_task.done()
            cancel_event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_event_task
            soul_task.result()
    finally:
        notification_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await notification_task
        try:
            await _deliver_notifications_to_wire_once(runtime, wire)
        except Exception:
            logger.exception("Failed to flush notifications to wire during shutdown")
        logger.debug("Shutting down the UI loop")
        wire.shutdown()
        await wire.join()
        try:
            await asyncio.wait_for(ui_task, timeout=0.5)
        except QueueShutDown:
            logger.debug("UI loop shut down")
            pass
        except TimeoutError:
            logger.warning("UI loop timed out")
        finally:
            _current_wire.reset(wire_token)


async def _pump_notifications_to_wire(runtime: Any | None, wire: Wire) -> None:
    while True:
        try:
            await _deliver_notifications_to_wire_once(runtime, wire)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification wire pump failed")
        await asyncio.sleep(1.0)


async def _deliver_notifications_to_wire_once(runtime: Any | None, wire: Wire) -> None:
    if runtime is None or getattr(runtime, "role", "") != "root":
        return

    from coderai.notifications import NotificationView, to_wire_notification

    def _send_notification(view: NotificationView) -> None:
        wire.soul_side.send(to_wire_notification(view))

    notifications = getattr(runtime, "notifications", None)
    bg_tasks = getattr(runtime, "background_tasks", None)
    if notifications is not None and hasattr(notifications, "deliver_pending"):
        await notifications.deliver_pending(
            "wire",
            limit=8,
            before_claim=bg_tasks.reconcile if bg_tasks else None,
            on_notification=_send_notification,
        )


__all__.extend(
    [
        "run_soul",
        "UILoopFn",
        "LLMNotSet",
        "LLMNotSupported",
        "MaxStepsReached",
        "RunCancelled",
    ]
)

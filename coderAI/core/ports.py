"""Host ports so core does not import the TUI.

``ApprovalPort`` is the confirmation seam between the agent runtime and the
embedding host. ``UIBridge`` implements it for ``coderAI chat``;
``DenyByDefaultApprovalPort`` implements it for ``coderAI run`` / ``plan``.
``AgentRuntime`` is the typed surface ``ExecutionLoop`` and ``ToolExecutor``
read instead of dumping agent state or unstructured attribute lookups.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from coderAI.context.context_controller import ContextController
    from coderAI.core.agent_tracker import AgentInfo
    from coderAI.core.execution_context import RunContext
    from coderAI.core.objective import ObjectiveState
    from coderAI.core.permissions import ApprovalRules
    from coderAI.llm.base import LLMProvider
    from coderAI.system.config import Config
    from coderAI.system.cost import CostTracker
    from coderAI.system.history import Session
    from coderAI.system.hooks_manager import HooksManager
    from coderAI.system.read_cache import FileReadCache
    from coderAI.tools import ToolRegistry

logger = logging.getLogger(__name__)

ConfirmationOverride = Callable[[str, dict[str, Any]], Awaitable[bool]]
ProgressCallback = Callable[[list[dict[str, Any]], bool], None]


class StreamingHandler(Protocol):
    """Consume a provider stream and return one normalized response."""

    async def handle_stream(
        self,
        stream: AsyncIterator[dict[str, Any]],
        initial_content: str = "",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class AsyncClosePort(Protocol):
    """Optional async cleanup surface implemented by some stream handlers."""

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Approval:
    """Decision returned by :meth:`ApprovalPort.request`."""

    allowed: bool


class ApprovalPort(Protocol):
    """Ask the embedding host whether a gated action may proceed."""

    async def request(
        self,
        tool: str,
        args: dict[str, Any],
        preview: Optional[str] = None,
    ) -> Approval:
        """Return whether ``tool`` may run with ``args``.

        ``preview`` is a human-readable diff or command listing. Interactive
        hosts own any request-correlation identifiers needed by their UI.
        """
        ...


class DenyByDefaultApprovalPort:
    """Fail-closed port for headless ``coderAI run`` / ``coderAI plan``.

    Every request is denied. Denied tool names are recorded so the CLI can
    report them; ``on_denied`` is invoked with the tool name when provided.
    """

    def __init__(self, on_denied: Optional[Callable[[str], None]] = None) -> None:
        self._on_denied = on_denied
        self.denied: list[str] = []

    async def request(
        self,
        tool: str,
        args: dict[str, Any],
        preview: Optional[str] = None,
    ) -> Approval:
        del args, preview
        self.denied.append(tool)
        if self._on_denied is not None:
            self._on_denied(tool)
        return Approval(allowed=False)


async def await_approval(
    port: ApprovalPort,
    tool: str,
    args: dict[str, Any],
    preview: Optional[str] = None,
    *,
    timeout_s: int = 0,
) -> bool:
    """Call ``port.request`` and deny if ``timeout_s`` elapses."""
    request = port.request(tool, args, preview)
    try:
        if timeout_s > 0:
            result = await asyncio.wait_for(request, timeout=timeout_s)
        else:
            result = await request
    except asyncio.TimeoutError:
        logger.warning(
            "Approval timed out after %ss for '%s' — denying.",
            timeout_s,
            tool,
        )
        return False
    return bool(result.allowed)


class AgentRuntime(Protocol):
    """Typed Agent surface for ``ExecutionLoop`` and ``ToolExecutor``.

    Production :class:`~coderAI.core.agent.Agent` provides this complete
    structural surface. Tests should supply a conforming fake rather than rely
    on ``getattr`` defaults in orchestration code.
    """

    config: Config
    session: Optional[Session]
    tools: ToolRegistry
    provider: LLMProvider
    hooks_manager: HooksManager
    cost_tracker: CostTracker
    auto_approve: bool
    plan_mode: bool
    active_plan_id: Optional[str]
    active_plan_revision: Optional[int]
    _plan_execution_ready: bool
    _allow_dynamic_mcp: bool
    _allowed_native_tool_names: Optional[frozenset[str]]
    _capability_domain: Optional[str]
    approval_port: Optional[ApprovalPort]
    confirmation_override: Optional[ConfirmationOverride]
    context_controller: ContextController
    read_cache: FileReadCache
    run_context: RunContext
    last_objective_state: Optional[ObjectiveState]
    tracker_info: Optional[AgentInfo]
    streaming: bool
    streaming_handler: Optional[StreamingHandler]
    model: str
    _mcp_initialized: bool
    _workspace_trust_checked: bool
    _workspace_trusted: bool
    _mcp_health_check_counter: int
    _tool_schemas_dirty: bool
    _mcp_health_task: Optional[asyncio.Task[None]]
    _active_skill_context: list[str]
    _last_mcp_schema_names: frozenset[str]
    _tool_approval_allowlist: ApprovalRules
    _cached_system_prompt: Optional[str]
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cache_creation_tokens: int
    total_cache_read_tokens: int

    def save_session(self) -> None: ...

    def _finish_tracker(self, error: bool = False) -> None: ...

    def _sync_tracker(self) -> None: ...

    def tracker_update(
        self,
        *,
        status: Any = ...,
        current_tool: Any = ...,
        current_task: Any = ...,
    ) -> None: ...

    def _refresh_session_system_prompt(self) -> None: ...

    def _register_tracker(
        self,
        task: str,
        role: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> AgentInfo: ...

    def create_session(self, *, reset_accounting: bool = True) -> Session: ...


@dataclass(frozen=True)
class RuntimeView:
    """Explicit compatibility view over the declared Agent runtime.

    Production ``Agent`` instances provide every protocol field. Narrow unit
    fakes may exercise one orchestration method with only the fields it needs;
    this view owns those documented defaults so ``ExecutionLoop`` and
    ``ToolExecutor`` never scatter ``getattr``/``vars`` probes through core.
    """

    agent: AgentRuntime

    def _field(self, name: str, default: Any = None) -> Any:
        try:
            inspect.getattr_static(self.agent, name)
        except AttributeError:
            return default
        return getattr(self.agent, name)

    @property
    def config(self) -> Optional[Config]:
        return cast("Optional[Config]", self._field("config"))

    @property
    def tools(self) -> Optional[ToolRegistry]:
        return cast("Optional[ToolRegistry]", self._field("tools"))

    @property
    def read_cache(self) -> Optional[FileReadCache]:
        return cast("Optional[FileReadCache]", self._field("read_cache"))

    @property
    def plan_mode(self) -> bool:
        return self._field("plan_mode", False) is True

    @property
    def active_plan_id(self) -> Optional[str]:
        return cast(Optional[str], self._field("active_plan_id"))

    @property
    def active_plan_revision(self) -> Optional[int]:
        return cast(Optional[int], self._field("active_plan_revision"))

    @property
    def plan_execution_ready(self) -> bool:
        return self._field("_plan_execution_ready", False) is True

    @property
    def allow_dynamic_mcp(self) -> bool:
        return self._field("_allow_dynamic_mcp", True) is not False

    @property
    def allowed_native_tool_names(self) -> Optional[frozenset[str]]:
        return cast(Optional[frozenset[str]], self._field("_allowed_native_tool_names"))

    @property
    def capability_domain(self) -> str:
        value = self._field("_capability_domain")
        return str(value) if value else "restricted"

    @property
    def approval_port(self) -> Optional[ApprovalPort]:
        return cast(Optional[ApprovalPort], self._field("approval_port"))

    @property
    def confirmation_override(
        self,
    ) -> Optional[ConfirmationOverride]:
        return cast(
            Optional[ConfirmationOverride],
            self._field("confirmation_override"),
        )

    @property
    def auto_approve(self) -> bool:
        return self._field("auto_approve", False) is True

    @property
    def tool_approval_allowlist(self) -> Optional[ApprovalRules]:
        return cast("Optional[ApprovalRules]", self._field("_tool_approval_allowlist"))

    @property
    def session(self) -> Optional[Session]:
        return cast("Optional[Session]", self._field("session"))

    @property
    def provider(self) -> Optional[LLMProvider]:
        return cast("Optional[LLMProvider]", self._field("provider"))

    @property
    def hooks_manager(self) -> Optional[HooksManager]:
        return cast("Optional[HooksManager]", self._field("hooks_manager"))

    @property
    def delegation_depth(self) -> int:
        value = self._field("delegation_depth", 0)
        return value if isinstance(value, int) else 0

    @property
    def run_context(self) -> Optional[RunContext]:
        return cast("Optional[RunContext]", self._field("run_context"))

    @property
    def workspace_trusted(self) -> bool:
        return self._field("_workspace_trusted", False) is True

    @property
    def context_controller(self) -> Optional[ContextController]:
        return cast("Optional[ContextController]", self._field("context_controller"))

    @property
    def tracker_info(self) -> Optional[AgentInfo]:
        return cast("Optional[AgentInfo]", self._field("tracker_info"))

    @property
    def active_skill_context(self) -> list[str]:
        value = self._field("_active_skill_context", [])
        return value if isinstance(value, list) else []

    @property
    def mcp_health_check_counter(self) -> int:
        value = self._field("_mcp_health_check_counter", 0)
        return int(value) if isinstance(value, int) else 0

    @property
    def tool_schemas_dirty(self) -> bool:
        return self._field("_tool_schemas_dirty", False) is True

    @property
    def mcp_health_task(self) -> Optional[asyncio.Task[None]]:
        return cast("Optional[asyncio.Task[None]]", self._field("_mcp_health_task"))

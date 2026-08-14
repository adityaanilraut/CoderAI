"""Main agent orchestrator for CoderAI."""

import logging
import os
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional


from coderAI.system.config import config_manager
from coderAI.system.cost import CostTracker
from coderAI.system.error_policy import BudgetExceededError
from coderAI.system.history import Session
from coderAI.context.context_controller import ContextController
from coderAI.tools import ToolRegistry
from coderAI.core.personas import AgentPersona
from coderAI.core.agent_tracker import AgentInfo
from coderAI.core.permissions import ApprovalRules
from coderAI.core.execution_context import PermissionPolicySnapshot, create_run_context
from coderAI.core.services import get_services
from coderAI.system.hooks_manager import HooksManager
from coderAI.system.read_cache import FileReadCache
from coderAI.core.objective import ObjectiveState
from coderAI.core.agent_session import AgentSessionMixin
from coderAI.core.agent_capabilities import AgentCapabilitiesMixin
from coderAI.core.ports import (
    ApprovalPort,
    AsyncClosePort,
    ConfirmationOverride,
    ProgressCallback,
    StreamingHandler,
)

logger = logging.getLogger(__name__)


class Agent(AgentCapabilitiesMixin, AgentSessionMixin):
    """Main agent orchestrator that coordinates LLM and tools.

    Behavior is split across two mixins: ``AgentCapabilitiesMixin`` (tools,
    persona, skills, system prompt) and ``AgentSessionMixin`` (session
    lifecycle, token/cost accounting, tracker, checkpoints). This class owns
    the state and the orchestration entry points (``process_message``).
    """

    def __init__(
        self,
        model: Optional[str] = None,
        streaming: bool = True,
        auto_approve: bool = False,
        persona_name: Optional[str] = None,
        is_subagent: bool = False,
        delegation_depth: int = 0,
        project_root: Optional[str] = None,
        workspace_trusted: Optional[bool] = None,
    ) -> None:
        """Initialize the agent.

        Args:
            model: Model name to use
            streaming: Enable streaming responses
            auto_approve: Skip tool confirmation prompts
                (--auto-approve / --yolo)
            persona_name: Name of a specific persona to load from
                .coderAI/agents/
            is_subagent: True when constructed by ``DelegateTaskTool``.
            delegation_depth: How deep this agent sits in the delegation
                tree. Root agent = 0, sub-agent of root = 1, etc. The tool
                registry's ``delegate_task`` reads this from the agent's
                ``SubagentContext`` to enforce ``MAX_DELEGATION_DEPTH``.
            project_root: Explicit workspace root for isolated delegated agents.
            workspace_trusted: Pinned parent trust decision for that explicit
                workspace. Ordinary root agents resolve trust normally.
        """
        resolved_project_root = str(Path(project_root or ".").resolve())
        from coderAI.system.trust import workspace_trust

        # Pin one trust decision for the full Agent lifetime. A grant made by
        # the first-turn prompt is persisted for the next Agent launch but must
        # not partially activate project-controlled surfaces in this instance.
        self._workspace_trusted = (
            bool(workspace_trusted)
            if workspace_trusted is not None
            else workspace_trust.is_trusted(resolved_project_root)
        )
        if self._workspace_trusted:
            with workspace_trust.pinned_decision(resolved_project_root, self._workspace_trusted):
                self.config = config_manager.load_project_config(project_root or ".")
        else:
            self.config = config_manager.load().model_copy(deep=True)
            self.config.project_root = resolved_project_root

        # Load custom persona if requested
        self.persona: Optional[AgentPersona] = None
        self.init_persona(persona_name, is_subagent)
        if self.persona and self.persona.model:
            model = self.persona.model

        self.model = model or self.config.default_model
        self.streaming = streaming and self.config.streaming
        self.auto_approve = auto_approve  # Can be toggled via /auto-approve
        self.is_subagent = is_subagent
        self.delegation_depth = int(delegation_depth)
        # Plan Mode is an enforced runtime capability boundary, not a prompt
        # label. TUI plan commands toggle it only for the planning turn.
        self.plan_mode: bool = False
        self.active_plan_id: Optional[str] = None
        self.active_plan_revision: Optional[int] = None
        self._plan_execution_ready: bool = False

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize context controller
        # (via private attribute to support lazy property)
        self._context_controller = ContextController(
            config=self.config,
            provider=self.provider,
            allow_project_instructions=self._workspace_trusted,
        )

        # Per-session file-read dedup cache (created before tool registry
        # build so the read_file tool picks it up via _wire_read_cache below).
        self.read_cache = FileReadCache()

        # Initialize tool registry
        # (optionally filtered by persona tools)
        self.tools = ToolRegistry()
        self.cost_tracker = CostTracker()
        self._context_controller.cost_tracker = self.cost_tracker
        self._context_controller._on_summary_tokens = self._add_summary_tokens
        self._rebuild_tool_registry()

        # One immutable identity/policy carrier per Agent run. Session and
        # tracker identities are added only at their explicit lifecycle edges.
        self.run_context = create_run_context(
            workspace_root=self.config.project_root,
            permission_policy=PermissionPolicySnapshot(
                auto_approve=self.auto_approve,
                workspace_trusted=self._workspace_trusted,
                allowed_tools=frozenset(self.tools.tools),
            ),
        )

        # Token accounting snapshot for the current tracking step
        self._tracker_start_completion = 0
        self._tracker_start_tokens = 0
        self._tracker_start_cost = 0.0

        # Streaming handler is provided by the surrounding UI (the TUI
        # injects one that emits protocol events). When absent,
        # ``_stream_response`` falls back to a non-streaming call.
        self.streaming_handler: Optional[StreamingHandler] = None

        # Host approval seam (TUI UIBridge, headless DenyByDefault, or tests).
        self.approval_port: Optional[ApprovalPort] = None

        # Optional confirmation override for tests and force-confirm fallthrough.
        # Headless ``coderAI run`` / ``plan`` use ``approval_port`` instead.
        # When set, the tool executor consults this callable —
        # ``async (tool_name, arguments) -> bool`` — before the approval port;
        # returning False denies the tool. An override allow is not a human
        # confirmation: ``force_confirm`` still falls through to the port.
        self.confirmation_override: Optional[ConfirmationOverride] = None

        # Sub-agent capability ceiling. Root agents leave these at defaults;
        # ``DelegateTaskTool`` pins them on isolated children.
        self._allow_dynamic_mcp: bool = True
        self._allowed_native_tool_names: Optional[frozenset[str]] = None
        self._capability_domain: Optional[str] = None

        # Session management
        self.session: Optional[Session] = None
        # Most recent objective state is restored from its independent ledger
        # when a session is resumed. It is not derived from transcript text.
        self.last_objective_state: Optional[ObjectiveState] = None

        # Background session-save machinery. ``save_session`` snapshots on the
        # caller thread and offloads the blocking disk write to a single worker
        # so the agent loop never stalls on file I/O. One worker keeps writes
        # serialized; ``_pending_saves`` holds in-flight futures so they aren't
        # GC'd and can be drained on ``close()``. Created lazily on first save.
        self._save_executor: Optional[ThreadPoolExecutor] = None
        self._pending_saves: set["Future[Any]"] = set()
        self._closed = False

        # Cumulative token usage tracking (#13). The Agent is the source of
        # truth for session totals; each turn adds the LLM response's per-call
        # ``usage`` here (see ExecutionLoop._call_llm_with_retry).
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0
        self.total_cache_creation_tokens: int = 0
        self.total_cache_read_tokens: int = 0

        # Register with global agent tracker for observability / cancellation
        self.tracker_info: Optional[AgentInfo] = None

        # Agent-lifetime flags managed by ExecutionLoop but owned here as real
        # declared attributes (Phase 4.1) rather than attributes conjured on the
        # agent via getattr/setattr from the loop.
        self._mcp_initialized: bool = False
        self._workspace_trust_checked: bool = False
        # MCP health / schema-refresh bookkeeping must outlive the per-message
        # ExecutionLoop instance, otherwise a reconnect that finishes after the
        # turn ends never rebuilds tool schemas on the next turn.
        self._mcp_health_check_counter: int = 0
        self._tool_schemas_dirty: bool = False
        self._mcp_health_task: Optional[asyncio.Task[None]] = None
        self._last_mcp_schema_names: frozenset[str] = frozenset()

        # Per-command approval cache for project hooks. Keyed by command string
        # so new or changed hooks re-prompt instead of inheriting an approval.
        self._hooks_approved: dict[str, bool] = {}
        # Argument-scoped "always allow" rules (Phase 4.2). High-risk tools
        # (run_command/write_file/…) cannot be blanket-allowed by name; they may
        # only be scoped to a reviewed command-prefix / path. The resolver reads
        # each tool's declared ``high_risk_no_blanket`` / ``approval_scope`` from
        # the live registry, so a new high-risk tool needs no edit here.
        # Initialized eagerly so the tool executor never races on first access.
        self._tool_approval_allowlist: ApprovalRules = ApprovalRules(
            resolver=lambda name: self.tools.get(name)
        )

        self.hooks_manager = HooksManager(self)

        # Skill auto-detection manager
        self.init_skills(self.config.project_root)
        self._active_skill_context: list[str] = []

        # Memoization for _get_system_prompt() — only rebuild when rules,
        # tools, or persona change.
        self._cached_system_prompt: Optional[str] = None
        self._system_prompt_cache_key: Optional[str] = None

        if not self.is_subagent:
            self._emit_project_sanity_warning()
            self._warn_if_outside_project_allowed()

    def _warn_if_outside_project_allowed(self) -> None:
        """Surface a visible warning while the project-sandbox opt-out is on.

        ``allow_outside_project`` (config flag or ``CODERAI_ALLOW_OUTSIDE_PROJECT``
        env) lets file tools escape the project root, so its being active should
        never be silent (Phase 2.5).
        """
        active = self.config.allow_outside_project or (
            os.environ.get("CODERAI_ALLOW_OUTSIDE_PROJECT") == "1"
        )
        if active:
            get_services().events.emit(
                "agent_warning",
                message=(
                    "allow_outside_project is ON — file tools may read/write outside "
                    "the project root. This is not persisted; it stays on only for this session."
                ),
            )

    def _emit_project_sanity_warning(self) -> None:
        """Warn when the project root does not look like a real codebase."""
        from coderAI.system.safeguards import project_sanity_check

        result = project_sanity_check(self.config.project_root)
        if result.get("is_valid_project"):
            return
        reasons = result.get("reasons") or []
        if not reasons:
            return
        get_services().events.emit(
            "agent_warning",
            message="Project sanity check: " + "; ".join(reasons),
        )

    @property
    def context_controller(self) -> ContextController:
        """Context controller (always initialized in __init__)."""
        return self._context_controller

    @context_controller.setter
    def context_controller(self, value: ContextController) -> None:
        self._context_controller = value

    async def process_message(
        self,
        user_message: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> dict[str, Any]:
        """Process a user message using ExecutionLoop.

        Before running the main loop, auto-detects and injects relevant
        skill instructions if ``auto_detect_skills`` is enabled.
        """
        from coderAI.core.agent_loop import ExecutionLoop

        self._refresh_session_system_prompt()
        self._active_skill_context = []

        # Capture a rewind point *before* skill injection / the user message
        # are appended, so the stored index is the clean pre-turn boundary.
        self._record_checkpoint(user_message)

        if self.config.auto_detect_skills and not self.is_subagent:
            try:
                skills = await self.skill_manager.get_top_skills(user_message)
                if skills and self.session:
                    self._inject_skill_context(skills)
            except BudgetExceededError as e:
                # The execution loop will return the normal budget-blocked
                # response; persist the classifier call before that early exit.
                logger.warning("Skill auto-detection exhausted the session budget: %s", e)
                self.save_session()
            except Exception as e:
                logger.warning("Skill auto-detection failed: %s", e)

        return await ExecutionLoop(self, progress_callback=progress_callback).run(user_message)

    async def process_single_shot(
        self,
        user_message: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> str:
        """Process a single message and return the assistant's text response."""
        result = await self.process_message(user_message, progress_callback=progress_callback)
        return str(result.get("content", "") or "")

    async def close(self) -> None:
        """Clean up resources (HTTP sessions, background processes, etc.)."""
        if self._closed:
            return
        self._closed = True
        self.save_session()
        await super().close()
        if isinstance(self.streaming_handler, AsyncClosePort):
            await self.streaming_handler.close()
        await self.provider.close()

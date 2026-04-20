"""Main agent orchestrator for CoderAI."""

import logging
import time as _time
from typing import Any, Dict, List, Optional, Tuple


from .config import config_manager
from .context import ContextManager
from .cost import CostTracker
from .history import Message, Session, history_manager
from .context_controller import (
    ContextController,
    RESPONSE_TOKEN_RESERVE,
    TOOL_OVERHEAD_TOKENS,
)
from .llm.factory import create_provider
from .system_prompt import (
    SYSTEM_PROMPT_INTRO,
    SYSTEM_PROMPT_TAIL,
    compose_default_system_prompt,
    format_tools_markdown,
)
from .tools import ToolRegistry
from .tools.discovery import discover_tools
from .tools.context_manage import ManageContextTool
from .events import event_emitter
from .agents import load_agent_persona, AgentPersona, expand_persona_tools
from .agent_tracker import agent_tracker, AgentStatus, AgentInfo

logger = logging.getLogger(__name__)


class Agent:
    """Main agent orchestrator that coordinates LLM and tools."""

    def __init__(
        self,
        model: Optional[str] = None,
        streaming: bool = True,
        auto_approve: bool = True,
        persona_name: Optional[str] = None,
        is_subagent: bool = False,
    ):
        """Initialize the agent.

        Args:
            model: Model name to use
            streaming: Enable streaming responses
            auto_approve: Skip tool confirmation prompts (--auto-approve / --yolo)
            persona_name: Name of a specific persona to load from .coderAI/agents/
        """
        self.config = config_manager.load_project_config(".")

        # Load custom persona if requested
        self.persona: Optional[AgentPersona] = None
        if persona_name:
            self.persona = load_agent_persona(persona_name, self.config.project_root)
            if self.persona and self.persona.model:
                model = self.persona.model

        self.model = model or self.config.default_model
        self.streaming = streaming and self.config.streaming
        self.auto_approve = auto_approve  # Can be toggled via /auto-approve
        self.is_subagent = is_subagent

        # Initialize context manager
        self.context_manager = ContextManager(config=self.config)

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize context controller (via private attribute to support lazy property)
        self._context_controller = ContextController(config=self.config, provider=self.provider)

        # Initialize tool registry (optionally filtered by persona tools)
        self.tools = ToolRegistry()
        self.cost_tracker = CostTracker()
        self._rebuild_tool_registry()

        # Token accounting snapshot for the current tracking step
        self._tracker_start_completion = 0
        self._tracker_start_tokens = 0
        self._tracker_start_cost = 0.0

        # Streaming handler is provided by the surrounding UI (the IPC entry
        # point injects one that emits protocol events). When absent,
        # ``_stream_response`` falls back to a non-streaming call.
        self.streaming_handler = None

        # Session management
        self.session: Optional[Session] = None

        # Cumulative token usage tracking (#13)
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0

        # Non-session scratch buffer: assistant text from each LLM turn while
        # handling one user message (tool loops). Used so IPC ``assistant_end``
        # can carry the full reply, not only the last model turn.
        self._assistant_reply_parts: List[str] = []

        # Register with global agent tracker for observability / cancellation
        self.tracker_info: Optional[AgentInfo] = None

        # Per-command approval cache for project hooks. Keyed by command string
        # so new or changed hooks re-prompt instead of inheriting an approval.
        self._hooks_approved: Dict[str, bool] = {}

    @property
    def context_controller(self) -> ContextController:
        """Context controller (always initialized in __init__)."""
        return self._context_controller

    @context_controller.setter
    def context_controller(self, value: ContextController):
        self._context_controller = value

    def _create_provider(self):
        """Create LLM provider using the centralized factory."""
        return create_provider(self.model, self.config)

    def _create_tool_registry(self) -> ToolRegistry:
        """Create and populate tool registry using dynamic discovery."""
        registry = ToolRegistry()

        # Discover all tools in the tools package
        discover_tools(registry)

        # Manually register tools that require specific initialization arguments
        registry.register(ManageContextTool(self.context_manager))

        # Filter web tools if not allowed for main agent
        if not (self.is_subagent or self.config.web_tools_in_main):
            to_remove = ["web_search", "read_url", "download_file"]
            for name in to_remove:
                if name in registry.tools:
                    del registry.tools[name]

        return registry

    def _configure_delegate_tool_context(self) -> None:
        """Keep the delegation tool aligned with the current agent state."""
        delegate_tool = self.tools.get("delegate_task")
        if delegate_tool is None:
            return
        tracker_info = getattr(self, "tracker_info", None)
        delegate_tool._parent_model = self.model
        delegate_tool._parent_context_manager = self.context_manager
        delegate_tool._parent_cost_tracker = self.cost_tracker
        # Sub-agents should ALWAYS default to False for security, unless explicitly
        # overridden in the delegate_task parameters (though currently not exposed).
        delegate_tool._parent_auto_approve = False
        delegate_tool._parent_agent_id = tracker_info.agent_id if tracker_info else None

    def _rebuild_tool_registry(self) -> None:
        """Rebuild the registry so persona changes take effect immediately."""
        self.tools = self._create_tool_registry()
        if self.persona and self.persona.tools:
            self._filter_tools_for_persona(self.persona.tools)
        self._configure_delegate_tool_context()

    def _refresh_session_system_prompt(self) -> None:
        """Update the live session's primary system prompt after persona changes."""
        if not self.session:
            return

        prompt = self._get_system_prompt()
        if self.session.messages and self.session.messages[0].role == "system":
            self.session.messages[0].content = prompt
        else:
            self.session.messages.insert(0, Message(role="system", content=prompt))
        self.session.updated_at = _time.time()

    def apply_persona(
        self, persona: Optional[AgentPersona], update_model: bool = True
    ) -> Optional[str]:
        """Apply a persona and refresh model, tools, and session prompt."""
        old_model = self.model
        self.persona = persona

        if persona and persona.model and update_model:
            self.model = persona.model

        if self.model != old_model:
            self.provider = self._create_provider()
            if self.session:
                self.session.model = self.model

        self._rebuild_tool_registry()
        self._refresh_session_system_prompt()

        if self.tracker_info:
            self.tracker_info.name = self.persona.name if self.persona else "main"
            self.tracker_info.role = (
                self.persona.description if self.persona else None
            )

        return old_model if self.model != old_model else None

    def set_persona(
        self, persona_name: Optional[str], update_model: bool = True
    ) -> Optional[AgentPersona]:
        """Load and apply a persona by name. Pass None to return to default mode."""
        persona = None
        if persona_name:
            persona = load_agent_persona(persona_name, self.config.project_root)
            if persona is None:
                return None
        self.apply_persona(persona, update_model=update_model)
        return persona

    def _filter_tools_for_persona(self, allowed_tools: list) -> None:
        """Apply the persona's mutating-tool policy.

        Persona frontmatter uses high-level tool labels like `Read` and `Edit`.
        These are expanded into concrete tool IDs, but read-only tools remain
        available so specialist personas can still inspect the codebase.

        ``delegate_task`` is always kept available so personas can still
        orchestrate further sub-agents — it's foundational to the multi-agent
        workflow rather than a persona-specific mutation.
        """
        allowed_set = expand_persona_tools(allowed_tools)
        # Tools that must remain available regardless of persona frontmatter.
        always_available = {"delegate_task"}
        to_remove = [
            name
            for name, tool in self.tools.tools.items()
            if name not in allowed_set
            and name not in always_available
            and not tool.is_read_only
        ]
        for name in to_remove:
            del self.tools.tools[name]

    def _get_system_prompt(self) -> str:
        """Get the base system prompt (or persona) and append any rules from .coderAI/rules/."""
        if self.persona:
            # Keep core principles, strategy, and safety — not only persona text + tool names.
            prompt = (
                f"{SYSTEM_PROMPT_INTRO}\n\n"
                f"{self.persona.instructions}\n\n"
                f"{format_tools_markdown(self.tools)}\n\n"
                f"{SYSTEM_PROMPT_TAIL}"
            )
        else:
            prompt = compose_default_system_prompt(self.tools)

        # Look for project rules and append them
        try:
            from pathlib import Path

            rules_dir = Path(self.config.project_root, ".coderAI", "rules")
            if rules_dir.exists() and rules_dir.is_dir():
                rules = []
                for rule_file in sorted(rules_dir.glob("*.md")):
                    try:
                        content = rule_file.read_text().strip()
                        if content:
                            rules.append(f"### Rule: {rule_file.name}\n{content}")
                    except Exception as e:
                        logger.warning(f"Failed to read rule file {rule_file.name}: {e}")

                if rules:
                    prompt += "\n\n## Project Specific Rules\n\n"
                    prompt += (
                        "The following rules are specific to this project and MUST be followed:\n\n"
                    )
                    prompt += "\n\n".join(rules)
        except Exception as e:
            logger.warning(f"Error loading project rules: {e}")

        return prompt

    def _inject_context_message(
        self, messages: List[Dict[str, Any]], query: str
    ) -> List[Dict[str, Any]]:
        """Inject the pinned-context system message after the last system message."""
        return self.context_controller.inject_context(
            messages=messages,
            context_manager=self.context_manager,
            query=query
        )

    def create_session(self) -> Session:
        """Create a new conversation session."""
        self.session = history_manager.create_session(model=self.model)
        # Add system prompt as the first message
        self.session.add_message("system", self._get_system_prompt())
        return self.session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load an existing session."""
        self.session = history_manager.load_session(session_id)
        return self.session

    def save_session(self):
        """Save current session."""
        if self.session and self.config.save_history:
            history_manager.save_session(self.session)

    def get_context_usage(self) -> Tuple[int, int]:
        """Get the current context window usage and limit."""
        messages = self.session.get_messages_for_api() if self.session else []

        # Inject system message if exists to get an accurate count
        messages = self.context_controller.inject_context(messages, self.context_manager)

        used_tokens = self.context_controller.estimate_tokens(messages)
        limit = self.config.context_window
        return used_tokens, limit

    async def compact_context(self) -> bool:
        """Manually force the context to be compacted by summarizing the history."""
        if not self.session:
            return False

        event_emitter.emit(
            "agent_status", message="[bold cyan]Force compacting context...[/bold cyan]"
        )

        # Use a local override instead of mutating the shared config object
        compact_limit = RESPONSE_TOKEN_RESERVE + TOOL_OVERHEAD_TOKENS + 1500

        try:
            messages = self.session.get_messages_for_api()

            compacted_messages = await self.context_controller.manage_context_window(
                messages, context_limit_override=compact_limit
            )

            for msg in compacted_messages:
                if (
                    msg.get("role") == "system"
                    and isinstance(msg.get("content"), str)
                    and (
                        "[Prior Conversation Summary]:" in msg.get("content")
                        or "were removed to fit" in msg.get("content")
                    )
                ):
                    new_messages = []
                    for i, m in enumerate(compacted_messages):
                        msg_args = {
                            k: v
                            for k, v in m.items()
                            if k in ["role", "content", "tool_calls", "tool_call_id", "name"]
                        }
                        # Preserve original timestamp if we have a corresponding message
                        if i < len(self.session.messages):
                            msg_args["timestamp"] = self.session.messages[i].timestamp
                        new_messages.append(Message(**msg_args))
                    self.session.messages = new_messages
                    self.session.updated_at = _time.time()
                    event_emitter.emit(
                        "agent_status",
                        message="[bold green]Context compacted successfully![/bold green]",
                    )
                    return True

            event_emitter.emit(
                "agent_status",
                message="[dim]Context already compact or could not be compacted.[/dim]",
            )
            return False

        except Exception as e:
            logger.error(f"Error during manual context compaction: {e}")
            return False

    def _register_tracker(self, task: str, role: str = None, parent_id: str = None) -> AgentInfo:
        """Register this agent with the global tracker."""
        self.tracker_info = agent_tracker.register(
            name=self.persona.name if self.persona else "main",
            role=role or (self.persona.description if self.persona else None),
            model=self.model,
            parent_id=parent_id,
            context_limit=self.config.context_window,
        )

        # Snapshot token baseline so `_sync_tracker` records only this turn's usage
        self._tracker_start_completion = self.total_completion_tokens
        self._tracker_start_tokens = self.total_tokens
        self._tracker_start_cost = self.cost_tracker.get_total_cost()
        self.tracker_info.current_task = task
        self.tracker_info.status = AgentStatus.THINKING
        event_emitter.emit("agent_lifecycle", action="started", info=self.tracker_info)

        # Keep DelegateTaskTool aware of who the parent agent is so
        # sub-agents inherit the model and link correctly in the tracker.
        self._configure_delegate_tool_context()

        return self.tracker_info

    def _sync_tracker(self):
        """Sync internal token counters to the tracker info."""
        info = self.tracker_info
        if not info:
            return
        info.completion_tokens = self.total_completion_tokens - self._tracker_start_completion
        info.total_tokens = self.total_tokens - self._tracker_start_tokens
        info.cost_usd = self.cost_tracker.get_total_cost() - self._tracker_start_cost
        if self.session:
            msgs = self.session.get_messages_for_api()
            info.context_used_tokens = self.context_controller.estimate_tokens(msgs)
        # Let UIs (Ink IPC) refresh the agents table between lifecycle start/end.
        event_emitter.emit("agent_tracker_sync", info=info)

    def _finish_tracker(self, error: bool = False):
        """Mark this agent as done in the tracker and emit completion event."""
        info = self.tracker_info
        if not info:
            return
        self._sync_tracker()
        # Preserve CANCELLED status if it was already set by request_cancel()
        if info.status != AgentStatus.CANCELLED:
            info.status = AgentStatus.ERROR if error else AgentStatus.DONE
        info.finished_at = _time.time()
        event_emitter.emit("agent_lifecycle", action="finished", info=info)


    async def process_message(self, user_message: str) -> Dict[str, Any]:
        """Process a user message using ExecutionLoop."""
        from .agent_loop import ExecutionLoop

        return await ExecutionLoop(self).run(user_message)

    async def process_single_shot(self, user_message: str) -> str:
        """Process a single message and return the assistant's text response."""
        result = await self.process_message(user_message)
        return result.get("content", "")

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about current model."""
        return self.provider.get_model_info()

    async def close(self) -> None:
        """Clean up resources (HTTP sessions, background processes, etc.)."""
        if hasattr(self.provider, "close"):
            await self.provider.close()


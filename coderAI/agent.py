"""Main agent orchestrator for CoderAI."""

import hashlib
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
    SYSTEM_PROMPT_OUTPUT_STYLE,
    build_environment_section,
    compose_default_system_prompt,
    format_tools_markdown,
)
from .tools import ToolRegistry
from .tools.discovery import discover_tools
from .tools.context_manage import ManageContextTool
from .events import event_emitter
from .agents import load_agent_persona, AgentPersona, expand_persona_tools
from .agent_tracker import agent_tracker, AgentStatus, AgentInfo
from .hooks_manager import HooksManager
from .read_cache import FileReadCache

logger = logging.getLogger(__name__)


def _freeze(value: Any) -> Any:
    """Return a hashable snapshot of ``value`` for identity comparison.

    Used by ``compact_context`` to look up preserved messages by content and
    tool-call identity across re-ordering. ``tool_calls`` is a list of dicts
    (unhashable), so convert nested lists/dicts into tuples.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


class Agent:
    """Main agent orchestrator that coordinates LLM and tools."""

    def __init__(
        self,
        model: Optional[str] = None,
        streaming: bool = True,
        auto_approve: bool = True,
        persona_name: Optional[str] = None,
        is_subagent: bool = False,
        delegation_depth: int = 0,
    ):
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
        """
        self.config = config_manager.load_project_config(".")

        # Load custom persona if requested
        self.persona: Optional[AgentPersona] = None
        if persona_name:
            self.persona = load_agent_persona(
                persona_name, self.config.project_root
            )
            if self.persona and self.persona.model:
                model = self.persona.model

        self.model = model or self.config.default_model
        self.streaming = streaming and self.config.streaming
        self.auto_approve = auto_approve  # Can be toggled via /auto-approve
        self.is_subagent = is_subagent
        self.delegation_depth = int(delegation_depth)

        # Initialize context manager
        self.context_manager = ContextManager(config=self.config)

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize context controller
        # (via private attribute to support lazy property)
        self._context_controller = ContextController(
            config=self.config, provider=self.provider
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
        # Initialized eagerly here so the tool executor's _approval_allowlist
        # property never races on first access.
        self._tool_approval_allowlist: set[str] = set()

        self.hooks_manager = HooksManager(self)

        # Per-session file-read dedup cache. Wire onto the read_file tool so
        # repeat reads of unchanged files in the same session collapse to a
        # short placeholder.
        self._wire_read_cache()

        # Memoization for _get_system_prompt() — only rebuild when rules,
        # tools, or persona change.
        self._cached_system_prompt: Optional[str] = None
        self._system_prompt_cache_key: Optional[str] = None

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

        # Manually register tools that require specific initialization
        # arguments
        registry.register(ManageContextTool(self.context_manager))

        # Filter web tools if not allowed for main agent. Sub-agents always
        # keep web tools so research delegations work.
        # ``http_request`` is *also* gated here — it was inadvertently left on
        # the main agent before, giving a back-door to web access even when
        # web_tools_in_main=False.
        if not (self.is_subagent or self.config.web_tools_in_main):
            to_remove = ["web_search", "read_url", "download_file", "http_request"]
            removed = [n for n in to_remove if n in registry.tools]
            for name in removed:
                del registry.tools[name]
            if removed:
                logger.info(
                    "web_tools_in_main=False — removed from main agent: %s",
                    ", ".join(removed),
                )

        return registry

    def _configure_delegate_tool_context(self) -> None:
        """Keep the delegation tool aligned with the current agent state."""
        delegate_tool = self.tools.get("delegate_task")
        if delegate_tool is None:
            return
        from .tools.subagent import SubagentContext
        tracker_info = getattr(self, "tracker_info", None)
        delegate_tool.context = SubagentContext(
            parent_agent_id=tracker_info.agent_id if tracker_info else None,
            parent_model=self.model,
            parent_context_manager=self.context_manager,
            parent_cost_tracker=self.cost_tracker,
            parent_auto_approve=self.auto_approve,
            parent_ipc_server=getattr(self, "ipc_server", None),
            parent_session=getattr(self, "session", None),
            delegation_depth=getattr(self, "delegation_depth", 0),
        )

    def _rebuild_tool_registry(self) -> None:
        """Rebuild registry so persona changes take effect immediately."""
        self.tools = self._create_tool_registry()
        if self.persona and self.persona.tools:
            self._filter_tools_for_persona(self.persona.tools)
        self._configure_delegate_tool_context()
        # Re-attach the read cache; rebuilding the registry creates fresh
        # tool instances that don't carry the per-session attribute.
        self._wire_read_cache()
        self._cached_system_prompt = None  # invalidate

    def _wire_read_cache(self) -> None:
        """Attach the per-session FileReadCache to the read_file tool."""
        cache = getattr(self, "read_cache", None)
        if cache is None:
            return
        read_tool = self.tools.get("read_file")
        if read_tool is not None:
            read_tool.read_cache = cache

    def _refresh_session_system_prompt(self) -> None:
        """Update live session system prompt after persona changes."""
        if not self.session:
            return

        prompt = self._get_system_prompt()
        if self.session.messages and self.session.messages[0].role == "system":
            self.session.messages[0].content = prompt
        else:
            self.session.messages.insert(
                0, Message(role="system", content=prompt)
            )
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
            self.provider.set_cumulative_usage(
                input_tokens=self.total_prompt_tokens,
                output_tokens=self.total_completion_tokens,
                cache_creation_tokens=getattr(self, "total_cache_creation_tokens", 0),
                cache_read_tokens=getattr(self, "total_cache_read_tokens", 0),
            )

        self._cached_system_prompt = None  # invalidate before rebuild
        self._rebuild_tool_registry()
        self._refresh_session_system_prompt()

        if self.tracker_info:
            self.tracker_info.name = (
                self.persona.name if self.persona else "main"
            )
            self.tracker_info.role = (
                self.persona.description if self.persona else None
            )

        return old_model if self.model != old_model else None

    def set_persona(
        self, persona_name: Optional[str], update_model: bool = True
    ) -> Optional[AgentPersona]:
        """Load and apply a persona by name.
        Pass None to return to default mode.
        """
        persona = None
        if persona_name:
            persona = load_agent_persona(
                persona_name, self.config.project_root
            )
            if persona is None:
                return None
        self.apply_persona(persona, update_model=update_model)
        return persona

    def _filter_tools_for_persona(self, allowed_tools: list) -> None:
        """Apply the persona's tool policy.

        Persona frontmatter uses high-level tool labels like `Read` and `Edit`.
        These are expanded into concrete tool IDs, but read-only tools remain
        available so specialist personas can still inspect the codebase.

        When the persona has ``permission`` rules (e.g. ``{"write_file": "deny"}``),
        those are applied on top of the whitelist — a ``"deny"`` entry removes
        the tool even if it would otherwise survive the filter.

        ``delegate_task`` is always kept available so personas can still
        orchestrate further sub-agents — it's foundational to the multi-agent
        workflow rather than a persona-specific mutation.
        """
        # Apply explicit permission rules first (deny takes precedence).
        if self.persona and self.persona.permission:
            for tool_name, action in self.persona.permission.items():
                if action == "deny" and tool_name in self.tools.tools:
                    del self.tools.tools[tool_name]
                elif action == "allow":
                    pass  # tool is already present; nothing to change

        allowed_set = expand_persona_tools(allowed_tools)
        if not allowed_set:
            # No tool whitelist = keep everything
            return

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

    def _compute_system_prompt_cache_key(self) -> str:
        """Build a cache key that changes when rules, tools, or persona change."""
        from pathlib import Path
        parts: List[str] = []
        parts.append(self.model)
        if self.persona:
            parts.append(f"persona:{self.persona.name}")
        parts.append(f"tools:{len(self.tools.tools)}:{','.join(sorted(self.tools.tools.keys()))}")
        rules_dir = Path(self.config.project_root, ".coderAI", "rules")
        if rules_dir.exists() and rules_dir.is_dir():
            for rule_file in sorted(rules_dir.glob("*.md")):
                try:
                    mtime = rule_file.stat().st_mtime
                    parts.append(f"rule:{rule_file.name}:{mtime}")
                except Exception:
                    pass
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _get_system_prompt(self) -> str:
        """Get base prompt and append rules from .coderAI/rules/."""
        cache_key = self._compute_system_prompt_cache_key()
        if (
            self._cached_system_prompt is not None
            and self._system_prompt_cache_key == cache_key
        ):
            return self._cached_system_prompt

        sections: List[str] = []
        seen_hashes: set[str] = set()

        def _append_once(text: str) -> None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                return
            seen_hashes.add(digest)
            sections.append(text)

        import os
        import platform as _platform

        # Build environment section (model ID, working dir, git status, platform, date)
        project_root = getattr(self.config, "project_root", os.getcwd())
        is_git = os.path.isdir(os.path.join(project_root, ".git"))
        env_section = build_environment_section(
            model=self.model,
            working_directory=os.getcwd(),
            workspace_root=project_root,
            is_git_repo=is_git,
            platform=_platform.system(),
        )

        if self.persona:
            # Keep core principles, strategy, and safety — not only persona
            # text + tool names. Environment block goes first.
            _append_once(
                f"{env_section}\n\n"
                f"{SYSTEM_PROMPT_INTRO}\n\n"
                f"{self.persona.instructions}\n\n"
                f"{format_tools_markdown(self.tools)}\n\n"
                f"{SYSTEM_PROMPT_OUTPUT_STYLE}\n\n"
                f"{SYSTEM_PROMPT_TAIL}"
            )
        else:
            _append_once(compose_default_system_prompt(self.tools, env_section=env_section))

        # Look for project rules and append them
        try:
            from pathlib import Path

            rules_dir = Path(self.config.project_root, ".coderAI", "rules")
            if rules_dir.exists() and rules_dir.is_dir():
                rules = []
                for rule_file in sorted(rules_dir.glob("*.md")):
                    try:
                        content = rule_file.read_text(encoding="utf-8").strip()
                        if content:
                            quoted = "\n".join(
                                f"> {line}" if line else ">" for line in content.splitlines()
                            )
                            rules.append(
                                f"### Rule: {rule_file.name}\n"
                                "[BEGIN PROJECT RULE]\n"
                                f"{quoted}\n"
                                "[END PROJECT RULE]"
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to read rule file %s: %s", rule_file.name, e
                        )

                if rules:
                    _append_once(
                        "\n\n## Project Specific Rules\n\n"
                        "The following rules are specific to this project "
                        "and MUST be followed:\n\n"
                        + "\n\n".join(rules)
                    )
        except Exception as e:
            logger.warning(f"Error loading project rules: {e}")

        result = "\n\n".join(sections)
        self._cached_system_prompt = result
        self._system_prompt_cache_key = cache_key
        return result

    def _reset_session_accounting(self) -> None:
        """Reset cost, token, and hook-approval state at session boundaries.

        The ``Agent`` outlives individual sessions (e.g. ``/clear`` creates a
        new one, ``history`` can load another). Without this reset the
        previous session's spend would count against the new session's
        budget, and prior hook approvals would carry over silently.
        """
        self.cost_tracker.reset()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self._hooks_approved.clear()
        allowlist = getattr(self, "_tool_approval_allowlist", None)
        if allowlist is not None:
            allowlist.clear()
        # Swap rather than clear: a concurrent read_file task (asyncio.gather)
        # may still hold the old cache reference; the new session gets a clean one.
        if hasattr(self, "read_cache") and self.read_cache is not None:
            self.read_cache = FileReadCache()
            self._wire_read_cache()
        provider = getattr(self, "provider", None)
        if provider is not None:
            provider.set_cumulative_usage()

    def create_session(self) -> Session:
        """Create a new conversation session."""
        self._reset_session_accounting()
        # Clear any stale plan from a previous session so a new terminal
        # session does not show the old plan on /plan.
        try:
            from .project_layout import find_dot_coderai_subdir
            from pathlib import Path
            pr = str(self.config.project_root)
            dot_dir = find_dot_coderai_subdir("", pr)
            if dot_dir is None:
                dot_dir = Path(pr).resolve() / ".coderAI"
            plan_path = dot_dir / "current_plan.json"
            if plan_path.exists():
                plan_path.unlink()
        except Exception:
            pass
        self.session = history_manager.create_session(model=self.model)
        # Add system prompt as the first message
        self.session.add_message("system", self._get_system_prompt())
        return self.session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load an existing session."""
        self._reset_session_accounting()
        self.session = history_manager.load_session(session_id)
        return self.session

    def realign_provider_usage_counters(self) -> None:
        """Sync provider cumulative counters to the agent's current totals."""
        provider = getattr(self, "provider", None)
        if provider is None:
            return
        provider.set_cumulative_usage(
            input_tokens=self.total_prompt_tokens,
            output_tokens=self.total_completion_tokens,
            cache_creation_tokens=getattr(
                self, "total_cache_creation_tokens", 0
            ),
            cache_read_tokens=getattr(self, "total_cache_read_tokens", 0),
        )

    def save_session(self):
        """Save current session."""
        if self.session and self.config.save_history:
            history_manager.save_session(self.session)

    def get_context_usage(self) -> Tuple[int, int]:
        """Get the current context window usage and limit."""
        messages = self.session.get_messages_for_api() if self.session else []

        # Inject system message if exists to get an accurate count
        messages = self.context_controller.inject_context(
            messages, self.context_manager
        )

        used_tokens = self.context_controller.estimate_tokens(messages)
        limit = self.config.context_window
        return used_tokens, limit

    def _add_summary_tokens(self, input_delta: int, output_delta: int):
        """Update cumulative token counters after a context summarization LLM call."""
        self.total_prompt_tokens += input_delta
        self.total_completion_tokens += output_delta
        self.total_tokens += input_delta + output_delta

    async def compact_context(self) -> bool:
        """Force the context to be compacted by summarizing history."""
        if not self.session:
            return False

        event_emitter.emit(
            "agent_status",
            message="[bold cyan]Force compacting context...[/bold cyan]",
        )

        # Use a local override instead of mutating the shared config object
        # Reserve ~1.5k tokens for the compaction response itself
        compact_limit = RESPONSE_TOKEN_RESERVE + TOOL_OVERHEAD_TOKENS + 1500

        try:
            messages = self.session.get_messages_for_api()

            compacted_messages = (
                await self.context_controller.manage_context_window(
                    messages, context_limit_override=compact_limit
                )
            )

            for compacted_msg in compacted_messages:
                if (
                    compacted_msg.get("role") == "system"
                    and isinstance(compacted_msg.get("content"), str)
                    and (
                        "[Prior Conversation Summary]:" in compacted_msg.get("content")
                        or "were removed to fit" in compacted_msg.get("content")
                    )
                ):
                    # Compaction reorders messages (inserts a summary, drops
                    # old turns), so matching timestamps by positional index
                    # mis-stamps every survivor. Build a lookup of original
                    # messages by identity (role + content + tool_calls) and
                    # copy each survivor's original timestamp across. The new
                    # summary message stamps with the current time.
                    now = _time.time()
                    originals_by_identity: Dict[tuple, List[float]] = {}
                    for orig in self.session.messages:
                        key = (
                            orig.role,
                            orig.content,
                            _freeze(orig.tool_calls),
                            orig.tool_call_id,
                            orig.name,
                        )
                        originals_by_identity.setdefault(key, []).append(
                            orig.timestamp
                        )

                    new_messages = []
                    for cm in compacted_messages:
                        msg_args = {
                            k: v
                            for k, v in cm.items()
                            if k in ["role", "content", "tool_calls", "tool_call_id", "name"]
                        }
                        key = (
                            msg_args.get("role"),
                            msg_args.get("content"),
                            _freeze(msg_args.get("tool_calls")),
                            msg_args.get("tool_call_id"),
                            msg_args.get("name"),
                        )
                        stamps = originals_by_identity.get(key)
                        if stamps:
                            msg_args["timestamp"] = stamps.pop(0)
                        else:
                            msg_args["timestamp"] = now
                        new_messages.append(Message(**msg_args))
                    self.session.messages = new_messages
                    self.session.updated_at = now
                    self.save_session()
                    # Run on_compact hooks
                    hooks_data = self.hooks_manager.load_hooks()
                    if hooks_data:
                        await self.hooks_manager.run_hooks("*", "on_compact", {}, hooks_data)

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


    async def process_message(
        self, user_message: str, progress_callback=None
    ) -> Dict[str, Any]:
        """Process a user message using ExecutionLoop."""
        from .agent_loop import ExecutionLoop

        return await ExecutionLoop(self, progress_callback=progress_callback).run(user_message)

    async def process_single_shot(
        self, user_message: str, progress_callback=None
    ) -> str:
        """Process a single message and return the assistant's text response."""
        result = await self.process_message(user_message, progress_callback=progress_callback)
        return result.get("content", "")

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about current model."""
        return self.provider.get_model_info()

    async def close(self) -> None:
        """Clean up resources (HTTP sessions, background processes, etc.)."""
        if hasattr(self, "streaming_handler") and self.streaming_handler is not None:
            if hasattr(self.streaming_handler, "close"):
                await self.streaming_handler.close()
        if hasattr(self.provider, "close"):
            await self.provider.close()
        if self.tracker_info:
            self._finish_tracker()

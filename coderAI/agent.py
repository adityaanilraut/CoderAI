"""Main agent orchestrator for CoderAI."""

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from .config import config_manager
from .context import ContextManager
from .cost import CostTracker
from .history import Message, Session, history_manager
from .llm import LMStudioProvider, OpenAIProvider, AnthropicProvider, OllamaProvider, GroqProvider, DeepSeekProvider
from .system_prompt import SYSTEM_PROMPT
from .tools import (
    TextSearchTool,
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
    GitBranchTool,
    GitCheckoutTool,
    GitStashTool,
    GlobSearchTool,
    GrepTool,
    ListDirectoryTool,
    ReadFileTool,
    RecallMemoryTool,
    RunBackgroundTool,
    RunCommandTool,
    SaveMemoryTool,
    SearchReplaceTool,
    ToolRegistry,
    WebSearchTool,
    ReadURLTool,
    WriteFileTool,
    DownloadFileTool,
    MCPConnectTool,
    MCPCallTool,
    MCPListTool,
    UndoTool,
    UndoHistoryTool,
    ProjectContextTool,
    ManageContextTool,
    ApplyDiffTool,
    LintTool,
    ManageTasksTool,
    DelegateTaskTool,
    ReadImageTool,
    UseSkillTool,
    PythonREPLTool,
    CreatePlanTool,
    NotepadTool,
)
from .events import event_emitter
from .ui.streaming import StreamingHandler
from .agents import load_agent_persona, AgentPersona
from .agent_tracker import agent_tracker, AgentStatus, AgentInfo

logger = logging.getLogger(__name__)

# Reserve tokens for the response and tool overhead
RESPONSE_TOKEN_RESERVE = 1024  # Further reduced for better context utilization
TOOL_OVERHEAD_TOKENS = 512  # Further reduced for better context utilization

# Retry configuration for transient errors
MAX_RETRIES_PER_ITERATION = 3
RETRY_BASE_DELAY = 1  # seconds
MAX_CONSECUTIVE_ERRORS = 3

# Patterns that indicate transient (retryable) errors
_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "rate limit",
    "rate_limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "server error",
    "internal server error",
    "connection reset",
    "connection error",
    "connect timeout",
    "overloaded",
    "capacity",
    "temporarily unavailable",
)


class Agent:
    """Main agent orchestrator that coordinates LLM and tools."""

    def __init__(
        self,
        model: str = None,
        streaming: bool = True,
        auto_approve: bool = True,
        persona_name: str = None,
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
        self.auto_approve = True  # Default to auto-approve; can be toggled via /auto-approve
        self.is_subagent = is_subagent

        # Set up logging
        logging.basicConfig(
            level=getattr(logging, self.config.log_level, logging.WARNING),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        # Initialize context manager
        self.context_manager = ContextManager()

        # Initialize hook approval tracking
        self._hooks_approved = None

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize tool registry (optionally filtered by persona tools)
        self.tools = self._create_tool_registry()
        if self.persona and self.persona.tools:
            self._filter_tools_for_persona(self.persona.tools)

        # Token accounting snapshot for the current tracking step
        self._tracker_start_prompt = 0
        self._tracker_start_completion = 0
        self._tracker_start_tokens = 0
        self._tracker_start_cost = 0.0

        # Initialize streaming handler
        self.streaming_handler = StreamingHandler()

        # Session management
        self.session: Optional[Session] = None

        # Cumulative token usage tracking (#13)
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0
        self.cost_tracker = CostTracker()

        # Register with global agent tracker for observability / cancellation
        self.tracker_info: Optional[AgentInfo] = None

    def _create_provider(self):
        """Create LLM provider based on model."""
        if self.model == "ollama":
            return OllamaProvider(
                model=self.config.ollama_model,
                endpoint=self.config.ollama_endpoint,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        elif self.model == "lmstudio":
            return LMStudioProvider(
                model=self.config.lmstudio_model,
                endpoint=self.config.lmstudio_endpoint,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        elif self.model.startswith("claude"):
            return AnthropicProvider(
                model=self.model,
                api_key=self.config.anthropic_api_key,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                reasoning_effort=self.config.reasoning_effort,
            )
        elif self.model in GroqProvider.SUPPORTED_MODELS or self.model.startswith("groq/"):
            actual_model = self.model.replace("groq/", "") if self.model.startswith("groq/") else self.model
            # the requested models openai/gpt-oss-120b and openai/gpt-oss-20b will match SUPPORTED_MODELS
            return GroqProvider(
                model=actual_model,
                api_key=self.config.groq_api_key,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        elif self.model in DeepSeekProvider.SUPPORTED_MODELS or self.model.startswith("deepseek/"):
            actual_model = self.model.replace("deepseek/", "") if self.model.startswith("deepseek/") else self.model
            return DeepSeekProvider(
                model=actual_model,
                api_key=self.config.deepseek_api_key,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        else:
            return OpenAIProvider(
                model=self.model,
                api_key=self.config.openai_api_key,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                reasoning_effort=self.config.reasoning_effort,
            )

    def _create_tool_registry(self) -> ToolRegistry:
        """Create and populate tool registry."""
        registry = ToolRegistry()

        # Register filesystem tools
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(SearchReplaceTool())
        registry.register(ListDirectoryTool())
        registry.register(GlobSearchTool())

        # Register terminal tools
        registry.register(RunCommandTool())
        registry.register(RunBackgroundTool())

        # Register git tools
        registry.register(GitAddTool())
        registry.register(GitStatusTool())
        registry.register(GitDiffTool())
        registry.register(GitCommitTool())
        registry.register(GitLogTool())
        registry.register(GitBranchTool())
        registry.register(GitCheckoutTool())
        registry.register(GitStashTool())

        # Register search tools
        registry.register(TextSearchTool())
        registry.register(GrepTool())

        # Register web search & URL tools (subagents always get them;
        # main agent gets them when web_tools_in_main is enabled)
        if self.is_subagent or self.config.web_tools_in_main:
            registry.register(WebSearchTool())
            registry.register(ReadURLTool())
            registry.register(DownloadFileTool())

        # Register memory tools
        registry.register(SaveMemoryTool())
        registry.register(RecallMemoryTool())

        # Register MCP tools
        registry.register(MCPConnectTool())
        registry.register(MCPCallTool())
        registry.register(MCPListTool())

        # Register undo/rollback tools
        registry.register(UndoTool())
        registry.register(UndoHistoryTool())

        # Register project context tool
        registry.register(ProjectContextTool())

        # Register context management tool
        registry.register(ManageContextTool(self.context_manager))

        # Register diff-based editing tool (F2)
        registry.register(ApplyDiffTool())

        # Register linter tool (F18)
        registry.register(LintTool())

        # Register task management tool
        registry.register(ManageTasksTool())

        # Register sub-agent delegation tool
        registry.register(DelegateTaskTool())

        # Register vision tool
        registry.register(ReadImageTool())

        # Register skills tool
        registry.register(UseSkillTool())

        # Register Python REPL tool
        registry.register(PythonREPLTool())

        # Register planning tool
        registry.register(CreatePlanTool())

        # Register notepad tool (inter-agent communication)
        registry.register(NotepadTool())

        return registry

    def _filter_tools_for_persona(self, allowed_tools: list) -> None:
        """Remove tools not listed in the persona's tools whitelist.

        Always keeps read-only tools so the agent can still explore the codebase.
        """
        allowed_set = set(allowed_tools)
        to_remove = [
            name
            for name, tool in self.tools.tools.items()
            if name not in allowed_set and not tool.is_read_only
        ]
        for name in to_remove:
            del self.tools.tools[name]

    def _get_system_prompt(self) -> str:
        """Get the base system prompt (or persona) and append any rules from .coderAI/rules/."""
        # Use persona instructions if loaded, else default
        prompt = self.persona.instructions if self.persona else SYSTEM_PROMPT

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
        """Inject the pinned-context system message after the last system message.

        Extracted so the main loop can call it both before the while loop and
        after every tool-result rebuild (where the transient injection is lost).
        """
        context_msg = self.context_manager.get_system_message(
            query=query,
            messages=messages,
        )
        if not context_msg:
            return messages
        insert_idx = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                insert_idx = i + 1
        messages.insert(insert_idx, {"role": "system", "content": context_msg})
        return messages

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
        context_msg = self.context_manager.get_system_message()
        if context_msg:
            # Insert after the main system prompt (index 0)
            messages.insert(1, {"role": "system", "content": context_msg})

        used_tokens = self._estimate_message_tokens(messages)
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

            compacted_messages = await self._manage_context_window(
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
                    from .history import Message

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

    async def _manage_context_window(
        self,
        messages: List[Dict[str, Any]],
        context_limit_override: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Manage context window by summarizing old messages to fit.

        Keeps the system prompt, pinned context, and the most recent messages.
        Note: Pinned context is already injected into messages as a system
        message before this method is called, so its tokens are accounted for
        via the system_tokens calculation below — no separate deduction needed.
        """
        context_limit = context_limit_override or self.config.context_window

        max_content_tokens = context_limit - RESPONSE_TOKEN_RESERVE - TOOL_OVERHEAD_TOKENS

        if max_content_tokens <= 0:
            max_content_tokens = context_limit // 2

        # Estimate total tokens
        total_tokens = self._estimate_message_tokens(messages)

        if total_tokens <= max_content_tokens:
            return messages

        logger.info(
            f"Context window management: {total_tokens} tokens exceeds limit of "
            f"{max_content_tokens}. Truncating old messages."
        )

        # Keep system messages and the most recent messages
        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Always keep the system prompt + last N messages
        system_tokens = self._estimate_message_tokens(system_messages)
        remaining_budget = max_content_tokens - system_tokens

        # Group messages into atomic units: an assistant message with tool_calls
        # and its corresponding tool result messages must stay together.
        groups = self._group_messages_for_truncation(non_system)

        # Build from the end (most recent groups first)
        kept_groups = []
        running_tokens = 0
        for group in reversed(groups):
            group_tokens = self._estimate_message_tokens(group)
            if running_tokens + group_tokens > remaining_budget:
                break
            kept_groups.insert(0, group)
            running_tokens += group_tokens

        # Flatten kept groups back to messages
        kept_messages = [msg for group in kept_groups for msg in group]

        # Summarize older messages if we had to remove them
        if len(kept_messages) < len(non_system):
            removed_messages = non_system[: -len(kept_messages)] if kept_messages else non_system

            # Extract text to summarize
            text_to_summarize = ""
            for msg in removed_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content")
                if content and isinstance(content, str):
                    text_to_summarize += f"{role.upper()}: {content}\n"

            if text_to_summarize:
                event_emitter.emit(
                    "agent_status",
                    message="\n[dim]Context window filling up. Summarizing older conversations...[/dim]",
                )
                prompt = (
                    "Summarize the following conversation history. This summary will replace "
                    "these messages in our memory, so preserve ALL of the following:\n"
                    "- Tool calls made and their outcomes (file paths, commands run, results)\n"
                    "- Files read, created, or modified (with paths)\n"
                    "- Key decisions made and their rationale\n"
                    "- Errors encountered and how they were resolved\n"
                    "- User preferences or constraints stated\n"
                    "- Current task status and next steps\n"
                    "Be concise but factually complete.\n\n"
                    f"{text_to_summarize}"
                )
                try:
                    response = await self.provider.chat(
                        [{"role": "user", "content": prompt}], tools=None
                    )
                    summary_content = ""
                    if "choices" in response and response["choices"]:
                        summary_content = (
                            response["choices"][0].get("message", {}).get("content", "")
                        )

                    if summary_content:
                        summary_notice = {
                            "role": "system",
                            "content": f"[Prior Conversation Summary]: {summary_content}",
                        }
                        return system_messages + [summary_notice] + kept_messages
                except Exception as e:
                    logger.warning(f"Failed to summarize context: {e}")

            # Fallback to simple truncation
            truncation_notice = {
                "role": "system",
                "content": f"[Note: {len(removed_messages)} earlier messages were removed to fit the context window. The conversation continues from here.]",
            }
            return system_messages + [truncation_notice] + kept_messages

        return system_messages + kept_messages

    def _group_messages_for_truncation(
        self, messages: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """Group messages into atomic units for safe truncation.

        An assistant message with tool_calls and its following tool result
        messages form one atomic group that cannot be split.

        Args:
            messages: Non-system messages in order

        Returns:
            List of message groups
        """
        groups = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            # If this is an assistant message with tool_calls, group it with
            # all following tool result messages
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                group = [msg]
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    group.append(messages[i])
                    i += 1
                groups.append(group)
            else:
                groups.append([msg])
                i += 1
        return groups

    def _estimate_message_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estimate token count for a list of messages."""
        total = 0
        for msg in messages:
            # ~4 tokens per message for formatting overhead (role, separators, etc.)
            total += 4
            content = msg.get("content") or ""
            if isinstance(content, str) and content:
                total += self.provider.count_tokens(content)
            # Tool calls add tokens too
            if msg.get("tool_calls"):
                total += self.provider.count_tokens(json.dumps(msg["tool_calls"]))
            # Tool result metadata
            if msg.get("tool_call_id"):
                total += self.provider.count_tokens(msg["tool_call_id"])
            if msg.get("name"):
                total += self.provider.count_tokens(msg["name"])
        total += 3  # reply priming
        return total

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
        self._tracker_start_prompt = self.total_prompt_tokens
        self._tracker_start_completion = self.total_completion_tokens
        self._tracker_start_tokens = self.total_tokens
        self._tracker_start_cost = self.cost_tracker.get_total_cost()
        self.tracker_info.current_task = task
        self.tracker_info.status = AgentStatus.THINKING
        event_emitter.emit("agent_lifecycle", action="started", info=self.tracker_info)

        # Keep DelegateTaskTool aware of who the parent agent is so
        # sub-agents inherit the model and link correctly in the tracker.
        delegate_tool = self.tools.get("delegate_task")
        if delegate_tool is not None:
            delegate_tool._parent_model = self.model
            delegate_tool._parent_agent_id = self.tracker_info.agent_id
            delegate_tool._parent_context_manager = self.context_manager

        return self.tracker_info

    def _sync_tracker(self):
        """Sync internal token counters to the tracker info."""
        info = self.tracker_info
        if not info:
            return
        info.prompt_tokens = self.total_prompt_tokens - self._tracker_start_prompt
        info.completion_tokens = self.total_completion_tokens - self._tracker_start_completion
        info.total_tokens = self.total_tokens - self._tracker_start_tokens
        info.cost_usd = self.cost_tracker.get_total_cost() - self._tracker_start_cost
        if self.session:
            msgs = self.session.get_messages_for_api()
            info.context_used_tokens = self._estimate_message_tokens(msgs)

    def _finish_tracker(self, error: bool = False):
        """Mark this agent as done in the tracker and emit completion event."""
        info = self.tracker_info
        if not info:
            return
        self._sync_tracker()
        # Preserve CANCELLED status if it was already set by request_cancel()
        if info.status != AgentStatus.CANCELLED:
            info.status = AgentStatus.ERROR if error else AgentStatus.DONE
        import time as _t

        info.finished_at = _t.time()
        event_emitter.emit("agent_lifecycle", action="finished", info=info)

    async def process_message(self, user_message: str) -> Dict[str, Any]:
        """Process a user message and return response.

        Args:
            user_message: User's message

        Returns:
            Dictionary with response content and metadata
        """
        # Create session if not exists
        if self.session is None:
            self.create_session()

        # Register with tracker for observability
        if not self.tracker_info or self.tracker_info.status in (
            AgentStatus.DONE,
            AgentStatus.ERROR,
        ):
            self._register_tracker(task=user_message[:120])
        else:
            self.tracker_info.current_task = user_message[:120]
            self.tracker_info.status = AgentStatus.THINKING

        # Check budget limit before processing
        if (
            self.config.budget_limit > 0
            and self.cost_tracker.get_total_cost() > self.config.budget_limit
        ):
            from .cost import CostTracker

            msg = f"Budget limit of {CostTracker.format_cost(self.config.budget_limit)} exceeded."
            event_emitter.emit("agent_error", message=msg)
            self._finish_tracker(error=True)
            return {
                "content": f"Blocked: {msg}",
                "messages": self.session.messages if self.session else [],
                "model_info": self.provider.get_model_info(),
            }

        # Add user message to session
        self.session.add_message("user", user_message)

        # Get messages for API
        messages = self.session.get_messages_for_api()

        # Inject pinned context / project instructions — filtered by relevance
        # to the current user message so agents only see what they need.
        messages = self._inject_context_message(messages, user_message)

        # Proactive context management: summarize older messages when context
        # usage exceeds 70% so we never slam into the hard limit mid-turn.
        # A single pass with the proactive limit avoids redundant LLM calls.
        proactive_limit = int(
            (self.config.context_window - RESPONSE_TOKEN_RESERVE - TOOL_OVERHEAD_TOKENS) * 0.70
        )
        effective_limit = proactive_limit + RESPONSE_TOKEN_RESERVE + TOOL_OVERHEAD_TOKENS
        messages = await self._manage_context_window(
            messages, context_limit_override=effective_limit
        )

        # Get tool schemas — include MCP discovered tools alongside built-in tools
        tool_schemas = self.tools.get_schemas() if self.provider.supports_tools() else None
        if tool_schemas is not None:
            try:
                from .tools.mcp import mcp_client

                mcp_schemas = mcp_client.get_tools_as_openai_format()
                if mcp_schemas:
                    tool_schemas = tool_schemas + mcp_schemas
            except Exception:
                pass  # MCP not available or no servers connected

        # Load hooks data once per turn — avoids repeated disk reads per tool call.
        _hooks_data: Optional[Dict[str, Any]] = None
        try:
            from pathlib import Path as _HPath
            _hfile = _HPath(self.config.project_root, ".coderAI", "hooks.json")
            if _hfile.exists():
                with open(_hfile, "r") as _hf:
                    _hooks_data = json.load(_hf)
        except Exception:
            pass

        async def _run_hooks(tool_name: str, hook_type: str, arguments: dict) -> list:
            """Run hooks for a tool stage using cached data; parallel-executes multiple hooks."""
            hooks_results: List[str] = []
            if _hooks_data is None:
                return hooks_results
            try:
                import os
                from .tools.terminal import is_command_blocked

                matching_hooks = [
                    h for h in _hooks_data.get("hooks", [])
                    if h.get("type") == hook_type and (
                        h.get("tool") == "*" or h.get("tool") == tool_name
                    )
                ]
                if not matching_hooks:
                    return hooks_results

                if not self.auto_approve and self._hooks_approved is False:
                    return hooks_results

                if not self.auto_approve and self._hooks_approved is None:
                    cmds_preview = ", ".join(
                        h.get("command", "?")[:60] for h in matching_hooks
                    )
                    event_emitter.emit(
                        "agent_status",
                        message=(
                            f"\n[bold yellow]⚠ Project hooks detected in .coderAI/hooks.json[/bold yellow]"
                            f"\n[dim]Commands: {cmds_preview}[/dim]"
                        ),
                    )
                    try:
                        from prompt_toolkit import PromptSession
                        ps = PromptSession()
                        answer = await ps.prompt_async("Allow project hooks to run? (y/n) > ")
                    except (ImportError, EOFError, KeyboardInterrupt):
                        try:
                            loop = asyncio.get_running_loop()
                            answer = await loop.run_in_executor(
                                None,
                                lambda: input("Allow project hooks to run? (y/n) > "),
                            )
                        except (EOFError, KeyboardInterrupt):
                            answer = "n"
                    if answer.strip().lower() not in ("y", "yes"):
                        event_emitter.emit(
                            "agent_status", message="[dim]Hooks denied by user.[/dim]"
                        )
                        self._hooks_approved = False
                        return hooks_results
                    self._hooks_approved = True

                env = os.environ.copy()
                env["CODERAI_TOOL_NAME"] = tool_name
                for i, arg_val in enumerate(arguments.values()):
                    env[f"CODERAI_ARG_{i}"] = str(arg_val)

                # Separate blocked from runnable
                runnable: List[str] = []
                for hook in matching_hooks:
                    cmd = hook.get("command")
                    if not cmd:
                        continue
                    if is_command_blocked(cmd):
                        logger.warning(f"Hook command blocked for safety: {cmd}")
                        hooks_results.append(f"[{hook_type} Hook BLOCKED]: {cmd}")
                    else:
                        runnable.append(cmd)

                # Run all hooks for this stage in parallel
                async def _exec_hook(cmd: str) -> Optional[str]:
                    event_emitter.emit(
                        "agent_status",
                        message=f"[dim]Running {hook_type} hook for {tool_name}...[/dim]",
                    )
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    stdout, stderr = await proc.communicate()
                    if proc.returncode != 0:
                        logger.warning(
                            f"{hook_type} hook '{cmd}' failed. "
                            f"Exit code: {proc.returncode}, Stderr: {stderr.decode('utf-8')}"
                        )
                        return None
                    out = stdout.decode("utf-8").strip()
                    return out if out else None

                if runnable:
                    hook_outputs = await asyncio.gather(
                        *(_exec_hook(cmd) for cmd in runnable), return_exceptions=True
                    )
                    for out in hook_outputs:
                        if isinstance(out, Exception):
                            logger.error(f"Hook execution error: {out}")
                        elif out:
                            hooks_results.append(f"[{hook_type} Hook Output]: {out}")
            except Exception as e:
                logger.error(f"Error running hooks: {e}")
            return hooks_results

        async def _execute_single_tool(pc: Dict[str, Any]) -> Dict[str, Any]:
            if pc["parse_error"]:
                return {"success": False, "error": pc["parse_error"]}
            try:
                tool_name = pc["tool_name"]
                arguments = pc["arguments"]

                pre_hooks_out = await _run_hooks(tool_name, "PreToolUse", arguments)

                if tool_name.startswith("mcp__") and self.tools.get(tool_name) is None:
                    from .tools.mcp import mcp_client
                    parts = tool_name.split("__", 2)  # ["mcp", server, name]
                    if len(parts) >= 3:
                        server = parts[1]
                        remote_tool = parts[2]
                        result = await mcp_client.call_tool(server, remote_tool, arguments)
                    else:
                        result = {
                            "success": False,
                            "error": f"Malformed MCP tool name: {tool_name}",
                        }
                else:
                    result = await self.tools.execute(
                        tool_name,
                        confirmation_callback=self._confirmation_callback
                        if not self.auto_approve
                        else None,
                        **arguments,
                    )

                post_hooks_out = await _run_hooks(tool_name, "PostToolUse", arguments)

                if isinstance(result, dict) and (pre_hooks_out or post_hooks_out):
                    result["_hooks"] = {"pre": pre_hooks_out, "post": post_hooks_out}

                return result
            except Exception as e:
                return {"success": False, "error": str(e)}

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = self.config.max_iterations
        iteration = 0
        consecutive_errors = 0

        while iteration < max_iterations:
            iteration += 1

            # ── Cancellation check ──
            if self.tracker_info and self.tracker_info.is_cancelled:
                self._finish_tracker()
                self.save_session()
                return {
                    "content": "Agent stopped by user.",
                    "messages": self.session.messages,
                    "model_info": self.provider.get_model_info(),
                }

            try:
                if self.tracker_info:
                    self.tracker_info.status = AgentStatus.THINKING
                    self._sync_tracker()

                response_data = await self._call_llm_with_retry(messages, tool_schemas)
                consecutive_errors = 0  # reset on success

                # Handle response
                content = response_data.get("content")
                tool_calls = response_data.get("tool_calls")

                # Add assistant message — keep None for content when only tool_calls
                self.session.add_message(
                    "assistant",
                    content,
                    tool_calls=tool_calls,
                )

                # If no tool calls, we're done
                if not tool_calls:
                    self._finish_tracker()
                    self.save_session()
                    return {
                        "content": content or "",
                        "messages": self.session.messages,
                        "model_info": self.provider.get_model_info(),
                    }

                # Parse all tool calls first
                parsed_calls = []
                for tool_call in tool_calls:
                    tool_id = tool_call.get("id", "")
                    function = tool_call.get("function", {})
                    tool_name = function.get("name", "")

                    try:
                        arguments = json.loads(function.get("arguments", "{}"))
                    except json.JSONDecodeError as e:
                        # Return a clear error to the LLM instead of silently swallowing
                        parsed_calls.append(
                            {
                                "tool_id": tool_id,
                                "tool_name": tool_name,
                                "arguments": None,
                                "parse_error": f"Invalid JSON arguments: {e}",
                            }
                        )
                        continue

                    parsed_calls.append(
                        {
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "arguments": arguments,
                            "parse_error": None,
                        }
                    )

                # Update tracker status to tool execution
                if self.tracker_info:
                    tool_names = [
                        pc["tool_name"] for pc in parsed_calls if pc["arguments"] is not None
                    ]
                    self.tracker_info.status = AgentStatus.TOOL_CALL
                    self.tracker_info.current_tool = ", ".join(tool_names) if tool_names else None

                # Display all tool calls
                for pc in parsed_calls:
                    if pc["arguments"] is not None:
                        event_emitter.emit(
                            "tool_call", tool_name=pc["tool_name"], arguments=pc["arguments"]
                        )
                    else:
                        event_emitter.emit(
                            "tool_error", tool_name=pc["tool_name"], error=pc["parse_error"]
                        )

                # Execute tool calls — parallelize read-only tools, keep
                # mutating tools sequential to avoid write races.
                # (_run_hooks and _execute_single_tool are defined before this loop)

                # Split calls into read-only and mutating groups,
                # keeping track of original indices for ordered reassembly
                ro_indices = []
                mut_indices = []
                for i, pc in enumerate(parsed_calls):
                    tool_obj = self.tools.get(pc["tool_name"])
                    if tool_obj and tool_obj.is_read_only:
                        ro_indices.append(i)
                    else:
                        mut_indices.append(i)

                results: list = [None] * len(parsed_calls)
                total_tools = len(parsed_calls)
                tools_done = 0

                event_emitter.emit(
                    "status_start", message="[bold cyan]Executing tools...[/bold cyan]"
                )

                # Run all read-only tools in parallel
                if ro_indices:
                    ro_results = await asyncio.gather(
                        *(_execute_single_tool(parsed_calls[i]) for i in ro_indices)
                    )
                    for idx, res in zip(ro_indices, ro_results):
                        results[idx] = res
                        tools_done += 1
                        event_emitter.emit(
                            "tool_progress",
                            step=tools_done,
                            total=total_tools,
                            tool_name=parsed_calls[idx]["tool_name"],
                        )

                # Run mutating tools sequentially (order matters)
                for idx in mut_indices:
                    t0 = _time.time()
                    results[idx] = await _execute_single_tool(parsed_calls[idx])
                    elapsed = _time.time() - t0
                    tools_done += 1
                    event_emitter.emit(
                        "tool_progress",
                        step=tools_done,
                        total=total_tools,
                        tool_name=parsed_calls[idx]["tool_name"],
                        elapsed=round(elapsed, 2),
                    )

                event_emitter.emit("status_stop")

                # Process results and add to session
                for pc, result in zip(parsed_calls, results):
                    result = self._summarize_tool_result(result)
                    event_emitter.emit("tool_result", tool_name=pc["tool_name"], result=result)

                    self.session.add_message(
                        "tool",
                        json.dumps(result),
                        tool_call_id=pc["tool_id"],
                        name=pc["tool_name"],
                    )

                # Clear current tool in tracker
                if self.tracker_info:
                    self.tracker_info.current_tool = None
                    self._sync_tracker()

                # Rebuild messages from session (single source of truth)
                messages = self.session.get_messages_for_api()

                # Re-inject pinned context — the earlier injection was into a
                # transient list and is not persisted in the session, so it must
                # be re-applied every time we rebuild from session history.
                messages = self._inject_context_message(messages, user_message)

                # Manage context again before next iteration (tool results can be large)
                messages = await self._manage_context_window(messages)

                # Continue loop to get next response
                event_emitter.emit("agent_status", message="\n[dim]Processing results...[/dim]")

            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                consecutive_errors += 1

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    event_emitter.emit(
                        "agent_error",
                        message=f"Too many consecutive errors ({consecutive_errors}). Last error: {e}",
                    )
                    self._finish_tracker(error=True)
                    self.save_session()
                    return {
                        "content": (
                            f"I encountered {consecutive_errors} consecutive errors. "
                            f"Last error: {str(e)}. Please try again."
                        ),
                        "messages": self.session.messages,
                        "model_info": self.provider.get_model_info(),
                    }

                # Non-fatal: feed the error back to the LLM so it can
                # self-correct (e.g. fix bad tool arguments).
                # NOTE: We inject into the transient messages list, NOT the
                # session, to avoid accumulating system messages across turns.
                event_emitter.emit(
                    "agent_error",
                    message=f"Error (attempt {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}",
                )
                messages = self.session.get_messages_for_api()
                messages = self._inject_context_message(messages, user_message)
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"[Error in previous step: {str(e)}. The assistant should acknowledge this "
                            f"error and try a different approach.]"
                        ),
                    }
                )
                messages = await self._manage_context_window(messages)
                continue

        # Max iterations reached
        event_emitter.emit("agent_warning", message="Maximum iteration limit reached")
        self._finish_tracker(error=True)
        self.save_session()
        return {
            "content": "I've reached the maximum number of iterations. Please try again.",
            "messages": self.session.messages,
            "model_info": self.provider.get_model_info(),
        }

    async def _call_llm_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Call the LLM with retry logic for transient errors.

        Retries up to MAX_RETRIES_PER_ITERATION times with exponential backoff
        for transient failures (timeouts, rate limits, server errors).
        Non-transient errors are raised immediately.
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES_PER_ITERATION + 1):
            try:
                if self.streaming:
                    result = await self._stream_response(messages, tool_schemas)
                else:
                    raw = await self.provider.chat(messages, tools=tool_schemas)
                    result = self._extract_response_data(raw)

                # Update tokens and cost
                model_info = self.provider.get_model_info()
                new_in = model_info.get("total_input_tokens", 0) - self.total_prompt_tokens
                new_out = model_info.get("total_output_tokens", 0) - self.total_completion_tokens

                if new_in > 0 or new_out > 0:
                    self.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                    self.total_completion_tokens = model_info.get("total_output_tokens", 0)
                    self.total_tokens = model_info.get("total_tokens", 0)
                    self.cost_tracker.add_cost(self.model, new_in, new_out)

                    if (
                        self.config.budget_limit > 0
                        and self.cost_tracker.get_total_cost() > self.config.budget_limit
                    ):
                        event_emitter.emit(
                            "agent_warning",
                            message=f"[bold red]BUDGET LIMIT EXCEEDED![/bold red] "
                            f"Limit: {CostTracker.format_cost(self.config.budget_limit)} > "
                            f"Current: {CostTracker.format_cost(self.cost_tracker.get_total_cost())}",
                        )

                return result
            except Exception as e:
                last_error = e
                if not self._is_transient_error(e) or attempt == MAX_RETRIES_PER_ITERATION:
                    raise
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    f"Transient error (attempt {attempt}/{MAX_RETRIES_PER_ITERATION}): "
                    f"{e}. Retrying in {delay}s…"
                )
                event_emitter.emit(
                    "agent_warning",
                    message=f"Transient error, retrying in {delay}s… ({attempt}/{MAX_RETRIES_PER_ITERATION})",
                )
                await asyncio.sleep(delay)

        # Should not reach here, but just in case
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Determine if an exception is transient and worth retrying."""
        msg = str(exc).lower()
        return any(pattern in msg for pattern in _TRANSIENT_PATTERNS)

    def _summarize_tool_result(self, result) -> Dict[str, Any]:
        """Summarize large tool results to prevent context overflow.

        Args:
            result: Tool execution result (should be a dict, but may be None or other types)

        Returns:
            Original or summarized result
        """
        # Guard against non-dict results (e.g. None from unexpected exceptions)
        if not isinstance(result, dict):
            result = {
                "success": False,
                "error": str(result) if result is not None else "No result returned",
            }

        result_str = json.dumps(result)
        if len(result_str) <= self.config.max_tool_output:
            return result

        def truncate_recursive(val, max_len):
            if isinstance(val, str):
                if len(val) > max_len:
                    half = max_len // 2
                    return (
                        val[:half]
                        + f"\n... [{len(val) - 2 * half} chars truncated] ...\n"
                        + val[-half:]
                    )
                return val
            elif isinstance(val, list):
                if len(val) > 50:
                    return [truncate_recursive(v, max_len) for v in val[:50]] + [
                        {"_note": f"Showing 50 of {len(val)} items"}
                    ]
                return [truncate_recursive(v, max_len) for v in val]
            elif isinstance(val, dict):
                return {k: truncate_recursive(v, max_len) for k, v in val.items()}
            return val

        # Truncate strings larger than max_tool_output // 2
        summarized = truncate_recursive(result, self.config.max_tool_output // 2)

        # If still too large, forcefully truncate at top level
        final_str = json.dumps(summarized)
        if len(final_str) > self.config.max_tool_output * 2:
            truncate_len = self.config.max_tool_output * 2
            return {
                "warning": "Tool output was extremely large and was forcefully truncated.",
                "output": final_str[:truncate_len] + "\n... [HARD TRUNCATED]"
            }

        return summarized

    async def _stream_response(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Stream response from LLM."""
        stream = self.provider.stream(messages, tools=tools)
        result = await self.streaming_handler.handle_stream(stream)
        return result

    def _extract_response_data(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Extract content and tool calls from API response."""
        choices = response.get("choices", [])
        if not choices:
            return {"content": None, "tool_calls": None}
        message = choices[0].get("message", {})

        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
        }

    async def process_single_shot(self, user_message: str) -> str:
        """Process a single message and return text response.

        Args:
            user_message: User's message

        Returns:
            Text response from assistant
        """
        result = await self.process_message(user_message)
        return result.get("content", "")

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about current model."""
        return self.provider.get_model_info()

    async def close(self) -> None:
        """Clean up resources (HTTP sessions, background processes, etc.)."""
        if hasattr(self.provider, "close"):
            await self.provider.close()

    async def _confirmation_callback(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Ask the user to confirm a tool execution.

        This is an async method that directly prompts the user in the
        terminal, working correctly within the running event loop.

        Returns:
            True if the user approved, False otherwise.
        """
        import sys

        # Display the confirmation request
        args_preview = json.dumps(arguments, indent=2)
        if len(args_preview) > 300:
            args_preview = args_preview[:300] + "\n  ... (truncated)"

        event_emitter.emit(
            "agent_status",
            message=(
                f"\n[bold yellow]⚠ Tool '{tool_name}' requires confirmation.[/bold yellow]"
                f"\n[dim]{args_preview}[/dim]"
            ),
        )

        # Use prompt_toolkit for async-safe input if available, fall back
        # to a simple thread-based input to avoid blocking the event loop.
        try:
            from prompt_toolkit import PromptSession

            prompt_session = PromptSession()
            answer = await prompt_session.prompt_async(
                "Allow this tool? (y/n) > ",
            )
        except (ImportError, EOFError, KeyboardInterrupt):
            # Fallback: run blocking input() in a thread
            try:
                loop = asyncio.get_running_loop()
                answer = await loop.run_in_executor(
                    None, lambda: input("Allow this tool? (y/n) > ")
                )
            except (EOFError, KeyboardInterrupt):
                answer = "n"

        return answer.strip().lower() in ("y", "yes")

"""Main agent orchestrator for CoderAI."""

import asyncio
import json
import logging
import time as _time
from typing import Any, Dict, List, Optional

from .config import config_manager
from .context import ContextManager
from .history import Message, Session, history_manager
from .llm import LMStudioProvider, OpenAIProvider, AnthropicProvider, OllamaProvider
from .system_prompt import SYSTEM_PROMPT
from .tools import (
    TextSearchTool,
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
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
    WriteFileTool,
    MCPConnectTool,
    MCPCallTool,
    MCPListTool,
    UndoTool,
    UndoHistoryTool,
    ProjectContextTool,
    ManageContextTool,
    ApplyDiffTool,
    LintTool,
)
from .events import event_emitter
from .ui.streaming import StreamingHandler

logger = logging.getLogger(__name__)

# Reserve tokens for the response and tool overhead
RESPONSE_TOKEN_RESERVE = 4096
TOOL_OVERHEAD_TOKENS = 2000

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

    def __init__(self, model: str = None, streaming: bool = True, auto_approve: bool = False):
        """Initialize the agent.

        Args:
            model: Model name to use
            streaming: Enable streaming responses
            auto_approve: Skip tool confirmation prompts (--auto-approve / --yolo)
        """
        self.config = config_manager.load()
        self.config = config_manager.load_project_config(".")
        self.model = model or self.config.default_model
        self.streaming = streaming and self.config.streaming
        self.auto_approve = auto_approve

        # Set up logging
        logging.basicConfig(
            level=getattr(logging, self.config.log_level, logging.WARNING),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        # Initialize context manager
        self.context_manager = ContextManager()

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize tool registry
        self.tools = self._create_tool_registry()

        # Initialize streaming handler
        self.streaming_handler = StreamingHandler()

        # Session management
        self.session: Optional[Session] = None

        # Cumulative token usage tracking (#13)
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tokens: int = 0

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

        # Register search tools
        registry.register(TextSearchTool())
        registry.register(GrepTool())

        # Register web search
        registry.register(WebSearchTool())

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

        return registry

    def create_session(self) -> Session:
        """Create a new conversation session."""
        self.session = history_manager.create_session(model=self.model)
        # Add system prompt as the first message
        self.session.add_message("system", SYSTEM_PROMPT)
        return self.session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load an existing session."""
        self.session = history_manager.load_session(session_id)
        return self.session

    def save_session(self):
        """Save current session."""
        if self.session and self.config.save_history:
            history_manager.save_session(self.session)

    def get_context_usage(self) -> tuple[int, int]:
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

    def _truncate_messages_to_fit(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Truncate old messages to fit within the context window.

        Keeps the system prompt, pinned context, and the most recent messages.
        Note: Pinned context is already injected into messages as a system
        message before this method is called, so its tokens are accounted for
        via the system_tokens calculation below — no separate deduction needed.
        """
        context_limit = self.config.context_window

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

        # Add a truncation notice if we removed messages
        if len(kept_messages) < len(non_system):
            removed_count = len(non_system) - len(kept_messages)
            truncation_notice = {
                "role": "system",
                "content": f"[Note: {removed_count} earlier messages were removed to fit the context window. The conversation continues from here.]",
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

        # Add user message to session
        self.session.add_message("user", user_message)

        # Get messages for API
        messages = self.session.get_messages_for_api()

        # Inject pinned context / project instructions
        # We insert this as a system message right after the main system prompt
        context_msg = self.context_manager.get_system_message()
        if context_msg:
            # Find index of last system message or insert at 1
            insert_idx = 0
            for i, msg in enumerate(messages):
                if msg.get("role") == "system":
                    insert_idx = i + 1
            
            messages.insert(insert_idx, {"role": "system", "content": context_msg})

        # Truncate messages to fit context window
        messages = self._truncate_messages_to_fit(messages)

        # Get tool schemas
        tool_schemas = self.tools.get_schemas() if self.provider.supports_tools() else None

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = self.config.max_iterations
        iteration = 0
        consecutive_errors = 0

        while iteration < max_iterations:
            iteration += 1

            try:
                response_data = await self._call_llm_with_retry(
                    messages, tool_schemas
                )
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
                        parsed_calls.append({
                            "tool_id": tool_id,
                            "tool_name": tool_name,
                            "arguments": None,
                            "parse_error": f"Invalid JSON arguments: {e}",
                        })
                        continue

                    parsed_calls.append({
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "parse_error": None,
                    })

                # Display all tool calls
                for pc in parsed_calls:
                    if pc["arguments"] is not None:
                        event_emitter.emit("tool_call", tool_name=pc["tool_name"], arguments=pc["arguments"])
                    else:
                        event_emitter.emit("tool_error", tool_name=pc["tool_name"], error=pc["parse_error"])

                # Execute tool calls — parallelize read-only tools, keep
                # mutating tools sequential to avoid write races (#9)
                # Uses the is_read_only flag on each Tool (F8)

                async def _execute_single_tool(pc):
                    if pc["parse_error"]:
                        return {"success": False, "error": pc["parse_error"]}
                    try:
                        return await self.tools.execute(
                            pc["tool_name"],
                            confirmation_callback=self._confirmation_callback if not self.auto_approve else None,
                            **pc["arguments"],
                        )
                    except Exception as e:
                        return {"success": False, "error": str(e)}

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
                
                event_emitter.emit("status_start", message="[bold cyan]Executing tools...[/bold cyan]")

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

                # Rebuild messages from session (single source of truth)
                messages = self.session.get_messages_for_api()

                # Truncate again before next iteration (tool results can be large)
                messages = self._truncate_messages_to_fit(messages)

                # Continue loop to get next response
                event_emitter.emit("agent_status", message="\n[dim]Processing results...[/dim]")

            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                consecutive_errors += 1

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    event_emitter.emit(
                        "agent_error",
                        message=f"Too many consecutive errors ({consecutive_errors}). Last error: {e}"
                    )
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
                # self-correct (e.g. fix bad tool arguments)
                event_emitter.emit(
                    "agent_error",
                    message=f"Error (attempt {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}"
                )
                self.session.add_message(
                    "assistant",
                    f"I encountered an error: {str(e)}. Let me try a different approach.",
                )
                messages = self.session.get_messages_for_api()
                messages = self._truncate_messages_to_fit(messages)
                continue

        # Max iterations reached
        event_emitter.emit("agent_warning", message="Maximum iteration limit reached")
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
                    return await self._stream_response(messages, tool_schemas)
                else:
                    raw = await self.provider.chat(messages, tools=tool_schemas)
                    return self._extract_response_data(raw)
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
                    message=f"Transient error, retrying in {delay}s… ({attempt}/{MAX_RETRIES_PER_ITERATION})"
                )
                await asyncio.sleep(delay)

        # Should not reach here, but just in case
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Determine if an exception is transient and worth retrying."""
        msg = str(exc).lower()
        return any(pattern in msg for pattern in _TRANSIENT_PATTERNS)

    def _summarize_tool_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Summarize large tool results to prevent context overflow.

        Args:
            result: Tool execution result

        Returns:
            Original or summarized result
        """
        result_str = json.dumps(result)
        if len(result_str) <= self.config.max_tool_output:
            return result

        # Truncate large string values in the result
        summarized = {}
        for key, value in result.items():
            if isinstance(value, str) and len(value) > self.config.max_tool_output // 2:
                half = self.config.max_tool_output // 4
                summarized[key] = (
                    value[:half]
                    + f"\n\n... [truncated {len(value) - 2 * half} chars] ...\n\n"
                    + value[-half:]
                )
            elif isinstance(value, list) and len(value) > 50:
                summarized[key] = value[:50]
                summarized[f"{key}_note"] = f"Showing first 50 of {len(value)} items"
            else:
                summarized[key] = value
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

    def _confirmation_callback(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Ask the user to confirm a tool execution.

        Emits a 'tool_confirmation' event. The UI layer is expected to
        register a listener that prompts the user and stores the answer
        in ``self._confirmation_result``.

        Returns:
            True if the user approved, False otherwise.
        """
        self._confirmation_result: Optional[bool] = None
        event_emitter.emit(
            "tool_confirmation",
            tool_name=tool_name,
            arguments=arguments,
            agent=self,
        )
        # The event listener sets self._confirmation_result synchronously
        if self._confirmation_result is None:
            # No UI listener registered — default to allow
            return True
        return self._confirmation_result

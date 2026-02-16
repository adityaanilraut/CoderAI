"""Main agent orchestrator for CoderAI."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .config import config_manager
from .history import Message, Session, history_manager
from .llm import LMStudioProvider, OpenAIProvider, AnthropicProvider
from .system_prompt import SYSTEM_PROMPT
from .tools import (
    TextSearchTool,
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
)
from .ui.display import display
from .ui.streaming import StreamingHandler

logger = logging.getLogger(__name__)

# Reserve tokens for the response and tool overhead
RESPONSE_TOKEN_RESERVE = 4096
TOOL_OVERHEAD_TOKENS = 2000


class Agent:
    """Main agent orchestrator that coordinates LLM and tools."""

    def __init__(self, model: str = None, streaming: bool = True):
        """Initialize the agent.

        Args:
            model: Model name to use
            streaming: Enable streaming responses
        """
        self.config = config_manager.load()
        self.model = model or self.config.default_model
        self.streaming = streaming and self.config.streaming

        # Set up logging
        logging.basicConfig(
            level=getattr(logging, self.config.log_level, logging.WARNING),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        # Initialize LLM provider
        self.provider = self._create_provider()

        # Initialize tool registry
        self.tools = self._create_tool_registry()

        # Initialize streaming handler
        self.streaming_handler = StreamingHandler()

        # Session management
        self.session: Optional[Session] = None

    def _create_provider(self):
        """Create LLM provider based on model."""
        if self.model == "lmstudio":
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
            )
        else:
            return OpenAIProvider(
                model=self.model,
                api_key=self.config.openai_api_key,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
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

    def _truncate_messages_to_fit(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Truncate old messages to fit within the context window.

        Keeps the system prompt and the most recent messages,
        removing older messages from the middle when the context is too large.
        Preserves atomic tool_call <-> tool_result message groups to avoid API errors.

        Args:
            messages: Full list of messages

        Returns:
            Truncated list that fits within context window
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

        # Truncate messages to fit context window
        messages = self._truncate_messages_to_fit(messages)

        # Get tool schemas
        tool_schemas = self.tools.get_schemas() if self.provider.supports_tools() else None

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = 10
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            try:
                if self.streaming:
                    # Stream response (works with or without tools)
                    response_data = await self._stream_response(messages, tool_schemas)
                else:
                    # Non-streaming
                    response_data = await self.provider.chat(messages, tools=tool_schemas)
                    response_data = self._extract_response_data(response_data)

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
                        display.print_tool_call(pc["tool_name"], pc["arguments"])
                    else:
                        display.print_error(f"Tool {pc['tool_name']}: {pc['parse_error']}")

                # Execute tool calls sequentially to avoid write-races
                # (e.g. write_file + search_replace on the same file)
                async def _execute_single_tool(pc):
                    if pc["parse_error"]:
                        return {"success": False, "error": pc["parse_error"]}
                    try:
                        return await self.tools.execute(pc["tool_name"], **pc["arguments"])
                    except Exception as e:
                        return {"success": False, "error": str(e)}

                results = []
                for pc in parsed_calls:
                    results.append(await _execute_single_tool(pc))

                # Process results and add to session
                for pc, result in zip(parsed_calls, results):
                    result = self._summarize_tool_result(result)
                    display.print_tool_result(pc["tool_name"], result)

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
                display.print("\n[dim]Processing results...[/dim]\n")

            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                display.print_error(f"Error during processing: {str(e)}")
                self.save_session()
                return {
                    "content": f"I encountered an error: {str(e)}",
                    "messages": self.session.messages,
                    "model_info": self.provider.get_model_info(),
                }

        # Max iterations reached
        display.print_warning("Maximum iteration limit reached")
        self.save_session()
        return {
            "content": "I've reached the maximum number of iterations. Please try again.",
            "messages": self.session.messages,
            "model_info": self.provider.get_model_info(),
        }

    # Maximum size (in characters) of a single tool result before summarization
    MAX_TOOL_RESULT_CHARS = 8000

    def _summarize_tool_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Summarize large tool results to prevent context overflow.

        Args:
            result: Tool execution result

        Returns:
            Original or summarized result
        """
        result_str = json.dumps(result)
        if len(result_str) <= self.MAX_TOOL_RESULT_CHARS:
            return result

        # Truncate large string values in the result
        summarized = {}
        for key, value in result.items():
            if isinstance(value, str) and len(value) > self.MAX_TOOL_RESULT_CHARS // 2:
                half = self.MAX_TOOL_RESULT_CHARS // 4
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

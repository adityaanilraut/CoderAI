"""Main agent orchestrator for CoderAI."""

import asyncio
import json
from typing import Any, Dict, List, Optional

from .config import config_manager
from .history import Message, Session, history_manager
from .llm import LMStudioProvider, OpenAIProvider
from .tools import (
    CodebaseSearchTool,
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
)
from .ui.display import display
from .ui.streaming import StreamingHandler


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
        registry.register(CodebaseSearchTool())
        registry.register(GrepTool())

        # Register web search
        registry.register(WebSearchTool())

        # Register memory tools
        registry.register(SaveMemoryTool())
        registry.register(RecallMemoryTool())

        return registry

    def create_session(self) -> Session:
        """Create a new conversation session."""
        self.session = history_manager.create_session(model=self.model)
        return self.session

    def load_session(self, session_id: str) -> Optional[Session]:
        """Load an existing session."""
        self.session = history_manager.load_session(session_id)
        return self.session

    def save_session(self):
        """Save current session."""
        if self.session and self.config.save_history:
            history_manager.save_session(self.session)

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

        # Get tool schemas
        tool_schemas = self.tools.get_schemas() if self.provider.supports_tools() else None

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = 10
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            try:
                if self.streaming and not tool_schemas:
                    # Stream response without tools
                    response_data = await self._stream_response(messages, tool_schemas)
                else:
                    # Non-streaming or with tools
                    response_data = await self.provider.chat(messages, tools=tool_schemas)
                    response_data = self._extract_response_data(response_data)

                # Handle response
                content = response_data.get("content", "")
                tool_calls = response_data.get("tool_calls")

                # Add assistant message
                self.session.add_message(
                    "assistant",
                    content or "",
                    tool_calls=tool_calls,
                )

                # If no tool calls, we're done
                if not tool_calls:
                    self.save_session()
                    return {
                        "content": content,
                        "messages": self.session.messages,
                        "model_info": self.provider.get_model_info(),
                    }

                # Execute tool calls
                messages = self.session.get_messages_for_api()
                for tool_call in tool_calls:
                    tool_id = tool_call.get("id", "")
                    function = tool_call.get("function", {})
                    tool_name = function.get("name", "")
                    
                    try:
                        arguments = json.loads(function.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {}

                    # Display tool call
                    display.print_tool_call(tool_name, arguments)

                    # Execute tool
                    try:
                        result = await self.tools.execute(tool_name, **arguments)
                    except Exception as e:
                        result = {"success": False, "error": str(e)}

                    # Display result
                    display.print_tool_result(tool_name, result)

                    # Add tool result to messages
                    self.session.add_message(
                        "tool",
                        json.dumps(result),
                        tool_call_id=tool_id,
                        name=tool_name,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "content": json.dumps(result),
                            "tool_call_id": tool_id,
                            "name": tool_name,
                        }
                    )

                # Continue loop to get next response
                display.print("\n[dim]Processing results...[/dim]\n")

            except Exception as e:
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

    async def _stream_response(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Stream response from LLM."""
        stream = self.provider.stream(messages, tools=tools)
        result = await self.streaming_handler.handle_stream(stream)
        return result

    def _extract_response_data(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Extract content and tool calls from API response."""
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})

        return {
            "content": message.get("content", ""),
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


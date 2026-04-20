"""Execution Loop orchestrator for CoderAI agent."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .agent_tracker import AgentStatus
from .cost import CostTracker
from .events import event_emitter
from .tool_executor import ToolExecutor
from .hooks_manager import HooksManager
from .error_policy import (
    is_transient_error,
    MAX_RETRIES_PER_ITERATION,
    RETRY_BASE_DELAY,
    MAX_CONSECUTIVE_ERRORS,
)

logger = logging.getLogger(__name__)


class ExecutionLoop:
    """Manages the main LLM-Tool interaction loop."""

    def __init__(self, agent):
        self.agent = agent
        self.tool_executor = ToolExecutor(agent)
        self.hooks_manager = HooksManager(agent)

    async def run(self, user_message: str) -> Dict[str, Any]:
        """Process a user message and return response."""
        
        # 1. Prepare session and check budget
        budget_block = self._prepare_session(user_message)
        if budget_block:
            return budget_block

        # 2. Add user message to session
        self.agent.session.add_message("user", user_message)

        # 3. Prepare messages (retrieve, inject context, manage window)
        messages = await self._prepare_messages(user_message)

        # 4. Get tool schemas
        tool_schemas = self._get_tool_schemas()

        # 5. Load project hooks
        hooks_data = self.hooks_manager.load_hooks()

        self.agent._assistant_reply_parts.clear()
        tools_were_used = False

        # Process with LLM (potentially multiple rounds for tool calls)
        max_iterations = self.agent.config.max_iterations
        iteration = 0
        consecutive_errors = 0

        while iteration < max_iterations:
            iteration += 1

            if self.agent.tracker_info and self.agent.tracker_info.is_cancelled:
                return self._handle_cancellation()

            try:
                if self.agent.tracker_info:
                    self.agent.tracker_info.status = AgentStatus.THINKING
                    self.agent._sync_tracker()

                response_data = await self._call_llm_with_retry(messages, tool_schemas)
                consecutive_errors = 0

                content = response_data.get("content")
                tool_calls = response_data.get("tool_calls")

                if content and str(content).strip():
                    self.agent._assistant_reply_parts.append(str(content).strip())

                self.agent.session.add_message("assistant", content, tool_calls=tool_calls)

                if not tool_calls:
                    if tools_were_used and not (content or "").strip():
                        summary = await self._post_tool_closing_message(user_message)
                        if summary:
                            msgs = self.agent.session.messages
                            if (
                                msgs
                                and msgs[-1].role == "assistant"
                                and not (msgs[-1].content or "").strip()
                                and not msgs[-1].tool_calls
                            ):
                                msgs.pop()
                            self.agent.session.add_message("assistant", summary)
                            self.agent._assistant_reply_parts.append(summary.strip())

                    self.agent._finish_tracker()
                    self.agent.save_session()
                    joined = "\n\n".join(self.agent._assistant_reply_parts)
                    reply_text = joined if joined else (content or "")
                    return {
                        "content": reply_text,
                        "messages": self.agent.session.messages,
                        "model_info": self.agent.provider.get_model_info(),
                    }

                # Orchestrate tool execution via the dedicated executor
                # Returns (did_error, messages, fatal_res)
                did_error, messages, fatal_res = await self.tool_executor.orchestrate_tool_calls(
                    tool_calls, user_message, hooks_data, self.hooks_manager, MAX_CONSECUTIVE_ERRORS, consecutive_errors
                )
                
                if did_error:
                    consecutive_errors += 1
                    if fatal_res:
                        return fatal_res
                    continue

                tools_were_used = True

            except Exception as e:
                logger.error(f"Error during processing: {e}", exc_info=True)
                consecutive_errors += 1

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    return self._handle_fatal_error(e, consecutive_errors)

                messages = await self._handle_recoverable_error(e, consecutive_errors, user_message)
                continue

        return self._handle_max_iterations()

    def _prepare_session(self, user_message: str) -> Optional[Dict[str, Any]]:
        """Initialize session and tracker, check budget limits."""
        if self.agent.session is None:
            self.agent.create_session()

        if not self.agent.tracker_info or self.agent.tracker_info.status in (
            AgentStatus.DONE,
            AgentStatus.ERROR,
            AgentStatus.CANCELLED,
        ):
            self.agent._register_tracker(task=user_message[:120])
        else:
            self.agent.tracker_info.current_task = user_message[:120]
            self.agent.tracker_info.status = AgentStatus.THINKING

        if (
            self.agent.config.budget_limit > 0
            and self.agent.cost_tracker.get_total_cost() > self.agent.config.budget_limit
        ):
            msg = f"Budget limit of {CostTracker.format_cost(self.agent.config.budget_limit)} exceeded."
            event_emitter.emit("agent_error", message=msg)
            self.agent._finish_tracker(error=True)
            return {
                "content": f"Blocked: {msg}",
                "messages": self.agent.session.messages if self.agent.session else [],
                "model_info": self.agent.provider.get_model_info(),
            }
        return None

    async def _prepare_messages(self, user_message: str) -> List[Dict[str, Any]]:
        """Retrieve messages from session, inject context, and manage window."""
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(
            messages, self.agent.context_manager, query=user_message
        )
        return await self.agent.context_controller.manage_context_window(messages)

    def _get_tool_schemas(self) -> Optional[List[Dict[str, Any]]]:
        """Collect tool schemas from built-in registry and MCP."""
        tool_schemas = self.agent.tools.get_schemas() if self.agent.provider.supports_tools() else None
        if tool_schemas is not None:
            try:
                from .tools.mcp import mcp_client
                mcp_schemas = mcp_client.get_tools_as_openai_format()
                if mcp_schemas:
                    tool_schemas = tool_schemas + mcp_schemas
            except Exception as e:
                logger.debug(f"MCP tool discovery skipped: {e}")
        return tool_schemas

    async def _post_tool_closing_message(self, user_message: str) -> Optional[str]:
        """Ask once for a short user-visible wrap-up when tools ran but the model returned no final text."""
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(
            messages, self.agent.context_manager, query=user_message
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Tools have finished. If you have not already told the user clearly "
                    "what was accomplished, write 1–3 short sentences confirming success "
                    "(what changed and where). If you already explained everything above, "
                    "reply with one brief line such as: All set — everything above is complete. "
                    "Do not call tools."
                ),
            }
        )
        messages = await self.agent.context_controller.manage_context_window(messages)
        event_emitter.emit(
            "agent_status",
            message="\n[dim]Writing a short completion summary…[/dim]",
        )
        try:
            response = await self._call_llm_with_retry(messages, None)
        except Exception as e:
            logger.warning("Post-tool closing message failed: %s", e)
            return "Tools finished — check the tool results above."

        text = (response.get("content") or "").strip()
        if response.get("tool_calls") and not text:
            return "Tools finished — check the tool results above."
        return text or None

    def _handle_cancellation(self) -> Dict[str, Any]:
        self.agent._finish_tracker()
        self.agent.save_session()
        joined = "\n\n".join(self.agent._assistant_reply_parts)
        tail = "Agent stopped by user."
        body = f"{joined}\n\n{tail}" if joined else tail
        return {
            "content": body,
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    def _handle_fatal_error(self, e: Exception, count: int) -> Dict[str, Any]:
        event_emitter.emit("agent_error", message=f"Too many consecutive errors ({count}). Last: {e}")
        self.agent._finish_tracker(error=True)
        self.agent.save_session()
        return {
            "content": f"I encountered {count} consecutive errors. Last error: {e}. Please try again.",
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _handle_recoverable_error(self, e: Exception, count: int, user_message: str) -> List[Dict[str, Any]]:
        event_emitter.emit("agent_error", message=f"Error (attempt {count}/{MAX_CONSECUTIVE_ERRORS}): {e}")
        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(messages, self.agent.context_manager, query=user_message)
        messages.append({"role": "system", "content": f"[Error in previous step: {e}. Acknowledge and try again.]"})
        return await self.agent.context_controller.manage_context_window(messages)

    def _handle_max_iterations(self) -> Dict[str, Any]:
        event_emitter.emit("agent_warning", message="Maximum iteration limit reached")
        self.agent._finish_tracker(error=True)
        self.agent.save_session()
        return {
            "content": "I've reached the maximum number of iterations. Please try again.",
            "messages": self.agent.session.messages,
            "model_info": self.agent.provider.get_model_info(),
        }

    async def _call_llm_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Call the LLM with retry logic for transient errors."""
        for attempt in range(1, MAX_RETRIES_PER_ITERATION + 1):
            try:
                if self.agent.streaming:
                    result = await self._stream_response(messages, tool_schemas)
                else:
                    raw = await self.agent.provider.chat(messages, tools=tool_schemas)
                    result = self._extract_response_data(raw)

                # Update tokens and cost
                model_info = self.agent.provider.get_model_info()
                new_in = model_info.get("total_input_tokens", 0) - self.agent.total_prompt_tokens
                new_out = model_info.get("total_output_tokens", 0) - self.agent.total_completion_tokens

                if new_in > 0 or new_out > 0:
                    self.agent.total_prompt_tokens = model_info.get("total_input_tokens", 0)
                    self.agent.total_completion_tokens = model_info.get("total_output_tokens", 0)
                    self.agent.total_tokens = model_info.get("total_tokens", 0)
                    self.agent.cost_tracker.add_cost(self.agent.model, new_in, new_out)

                    if (
                        self.agent.config.budget_limit > 0
                        and self.agent.cost_tracker.get_total_cost() > self.agent.config.budget_limit
                    ):
                        msg = (
                            f"Budget limit of {CostTracker.format_cost(self.agent.config.budget_limit)} exceeded "
                            f"(current: {CostTracker.format_cost(self.agent.cost_tracker.get_total_cost())}). Stopping."
                        )
                        event_emitter.emit("agent_warning", message=f"[bold red]BUDGET LIMIT EXCEEDED![/bold red] {msg}")
                        raise RuntimeError(msg)

                return result
            except Exception as e:
                if not is_transient_error(e) or attempt == MAX_RETRIES_PER_ITERATION:
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

        raise RuntimeError("Retry loop exited without raising — this should never happen.")

    async def _stream_response(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Stream response from LLM."""
        if self.agent.streaming_handler is None:
            raw = await self.agent.provider.chat(messages, tools=tools)
            return self._extract_response_data(raw)
        stream = self.agent.provider.stream(messages, tools=tools)
        result = await self.agent.streaming_handler.handle_stream(stream)
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

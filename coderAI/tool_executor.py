"""Tool execution and orchestration for the CoderAI agent.

Handles batch parallelism, read-only vs mutating limits, hook execution,
and UI confirmation.
"""

import asyncio
import json
import logging
import time as _time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .agent_tracker import AgentStatus
from .events import event_emitter
from .tool_routing import call_mcp_tool_by_function_name, is_mcp_function_name, coerce_tool_arguments

logger = logging.getLogger(__name__)

# Cap concurrent read-only tools to avoid OS resource exhaustion
MAX_CONCURRENT_READ_ONLY = 20


class ToolExecutor:
    """Coordinates tool batched runs, context updates, and UI confirmations."""

    def __init__(self, agent):
        self.agent = agent
        self._ro_semaphore: Optional[asyncio.Semaphore] = None

    @property
    def _read_only_semaphore(self) -> asyncio.Semaphore:
        """Lazily create the semaphore inside a running event loop."""
        if self._ro_semaphore is None:
            self._ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY)
        return self._ro_semaphore

    async def _confirmation_callback(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_id: Optional[str] = None,
    ) -> bool:
        """Ask the user to confirm a tool execution."""
        ipc_server = getattr(self.agent, "ipc_server", None)

        if ipc_server is None:
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

        if ipc_server is not None:
            return await ipc_server.request_tool_approval(
                tool_id=tool_id or str(uuid.uuid4()),
                tool_name=tool_name,
                arguments=arguments,
            )

        try:
            from prompt_toolkit import PromptSession
            prompt_session = PromptSession()
            answer = await prompt_session.prompt_async("Allow this tool? (y/n) > ")
        except (ImportError, EOFError, KeyboardInterrupt):
            try:
                loop = asyncio.get_running_loop()
                answer = await loop.run_in_executor(None, lambda: input("Allow this tool? (y/n) > "))
            except (EOFError, KeyboardInterrupt):
                answer = "n"

        return answer.strip().lower() in ("y", "yes")

    async def execute_single_tool(self, pc: Dict[str, Any], hooks_data: Optional[Dict[str, Any]], hooks_manager) -> Dict[str, Any]:
        """Execute a single tool call, including hooks and confirmation."""
        if pc.get("parse_error"):
            return {"success": False, "error": pc["parse_error"]}
        try:
            tool_name = pc["tool_name"]
            arguments = pc["arguments"]

            pre_hooks = await hooks_manager.run_hooks(tool_name, "PreToolUse", arguments, hooks_data)

            async def _confirm(name, args):
                return await self._confirmation_callback(name, args, tool_id=pc["tool_id"])

            if is_mcp_function_name(tool_name) and self.agent.tools.get(tool_name) is None:
                result = await call_mcp_tool_by_function_name(tool_name, arguments)
            else:
                result = await self.agent.tools.execute(
                    tool_name,
                    confirmation_callback=_confirm if not self.agent.auto_approve else None,
                    **arguments,
                )

            post_hooks = await hooks_manager.run_hooks(tool_name, "PostToolUse", arguments, hooks_data)

            if isinstance(result, dict) and (pre_hooks or post_hooks):
                result["_hooks"] = {"pre": pre_hooks, "post": post_hooks}
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def orchestrate_tool_calls(
        self, tool_calls: list, user_message: str, hooks_data: Optional[Dict[str, Any]], hooks_manager, max_consecutive_errors: int, current_errors: int
    ) -> Tuple[bool, Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
        """Parse and execute a batch of tool calls.
        Returns: Tuple of (did_error, messages_for_next_round, dict_with_fatal_session)
        """
        parsed_calls = []
        parse_failures = 0
        for tc in tool_calls:
            tool_id = tc.get("id", "")
            func = tc.get("function", {}) or {}
            name = func.get("name", "") or ""
            raw_args = func.get("arguments")
            args, arg_err = coerce_tool_arguments(raw_args)
            if arg_err is not None:
                parse_failures += 1
                parsed_calls.append({"tool_id": tool_id, "tool_name": name, "arguments": None, "parse_error": arg_err})
            else:
                parsed_calls.append({"tool_id": tool_id, "tool_name": name, "arguments": args, "parse_error": None})

        if parse_failures == len(parsed_calls):
            # All tools failed to parse
            check_count = current_errors + 1
            for pc in parsed_calls:
                self.agent.session.add_message("tool", json.dumps({"success": False, "error": pc["parse_error"]}), tool_call_id=pc["tool_id"], name=pc["tool_name"])
            
            if check_count >= max_consecutive_errors:
                self.agent._finish_tracker(error=True)
                self.agent.save_session()
                return True, [], {"content": f"I encountered {check_count} consecutive parse errors. Rephrase please.", "session_id": self.agent.session.session_id}
            
            messages = self.agent.session.get_messages_for_api()
            messages = self.agent.context_controller.inject_context(messages, self.agent.context_manager, query=user_message)
            messages = await self.agent.context_controller.manage_context_window(messages)
            return True, messages, {"retry": True}

        if self.agent.tracker_info:
            self.agent.tracker_info.status = AgentStatus.TOOL_CALL
            self.agent.tracker_info.current_tool = ", ".join(pc["tool_name"] for pc in parsed_calls if pc["arguments"])

        for pc in parsed_calls:
            if pc["parse_error"] is not None:
                event_emitter.emit("tool_error", tool_name=pc["tool_name"], error=pc["parse_error"])
            elif pc["arguments"] is not None:
                event_emitter.emit("tool_call", tool_name=pc["tool_name"], arguments=pc["arguments"], tool_id=pc["tool_id"])

        results = await self.run_tool_batch(parsed_calls, hooks_data, hooks_manager)

        for pc, res in zip(parsed_calls, results):
            res = self.agent.context_controller.summarize_tool_result(res)
            event_emitter.emit("tool_result", tool_name=pc["tool_name"], result=res, tool_id=pc["tool_id"])
            self.agent.session.add_message("tool", json.dumps(res), tool_call_id=pc["tool_id"], name=pc["tool_name"])

        if self.agent.tracker_info:
            self.agent.tracker_info.current_tool = None
            self.agent._sync_tracker()

        messages = self.agent.session.get_messages_for_api()
        messages = self.agent.context_controller.inject_context(messages, self.agent.context_manager, query=user_message)
        messages = await self.agent.context_controller.manage_context_window(messages)
        
        event_emitter.emit("agent_status", message="\n[dim]Processing results...[/dim]")
        return False, messages, None

    async def run_tool_batch(self, parsed_calls: list, hooks_data: Optional[Dict[str, Any]], hooks_manager) -> list:
        """Handle parallel/sequential execution of parsed tool calls."""
        ro_indices: list = []
        capped_groups: Dict[str, list] = {}
        mut_indices: list = []
        for i, pc in enumerate(parsed_calls):
            tool_name = pc["tool_name"]
            tool = self.agent.tools.get(tool_name)
            if not tool:
                mut_indices.append(i)
                continue
            max_par = getattr(tool, "max_parallel_invocations", 0)
            if max_par > 0:
                capped_groups.setdefault(tool_name, []).append(i)
            elif getattr(tool, "is_read_only", False):
                ro_indices.append(i)
            else:
                mut_indices.append(i)

        results = [None] * len(parsed_calls)
        total, done = len(parsed_calls), 0
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None

        async def _run(pc):
            coro = self.execute_single_tool(pc, hooks_data, hooks_manager)
            if not cancel_event:
                return await coro
            t = asyncio.ensure_future(coro)
            w = asyncio.ensure_future(cancel_event.wait())
            done_set, _pending = await asyncio.wait({t, w}, return_when=asyncio.FIRST_COMPLETED)
            if t in done_set:
                w.cancel()
                return t.result()
            t.cancel()
            return {"success": False, "error": "Cancelled by user.", "error_code": "cancelled"}

        def _emit_progress(i: int, elapsed: Optional[float] = None) -> None:
            nonlocal done
            done += 1
            payload = {"step": done, "total": total, "tool_name": parsed_calls[i]["tool_name"]}
            if elapsed is not None:
                payload["elapsed"] = elapsed
            event_emitter.emit("tool_progress", **payload)

        event_emitter.emit("status_start", message="[bold cyan]Executing tools...[/bold cyan]")

        if ro_indices:
            async def _run_ro(idx):
                async with self._read_only_semaphore:
                    return await _run(parsed_calls[idx])
            res = await asyncio.gather(*(_run_ro(i) for i in ro_indices))
            for i, r in zip(ro_indices, res):
                results[i] = r
                _emit_progress(i)

        for tool_name, indices in capped_groups.items():
            cap_tool = self.agent.tools.get(tool_name)
            size = max(1, int(getattr(cap_tool, "max_parallel_invocations", 1)))
            for start in range(0, len(indices), size):
                chunk = indices[start : start + size]
                res = await asyncio.gather(*(_run(parsed_calls[i]) for i in chunk))
                for i, r in zip(chunk, res):
                    results[i] = r
                    _emit_progress(i)

        for i in mut_indices:
            t0 = _time.time()
            results[i] = await _run(parsed_calls[i])
            _emit_progress(i, elapsed=round(_time.time() - t0, 2))

        event_emitter.emit("status_stop")
        return results
